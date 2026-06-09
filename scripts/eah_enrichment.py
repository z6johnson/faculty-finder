#!/usr/bin/env python3
"""Enrich faculty JSON files with Employee Activity Hub (EAH) data.

Reads the EAH Active Academics CSV and reconciles it against our three
tracked schools (HWSPH, Jacobs, SIO). Updates contact details, adds EAH
fields, flags inactive faculty, and adds new faculty from EAH.
"""

import csv
import json
import os
import re
import sys
import tempfile
from collections import defaultdict

# Writable state dir for the faculty JSON snapshots (see enrichment/pipeline.py).
DATA_DIR = (os.environ.get("DATA_STATE_DIR", "").strip()
            or os.path.join(os.path.dirname(__file__), "..", "data"))

# The raw EAH extract is sensitive UCSD HR PII and must NEVER live inside the
# repo tree. In production EAH_CSV_PATH points at the private runtime volume
# (e.g. /data/private/...). The default below is for local/dev use only and is
# git-ignored.
EAH_PATH = (os.environ.get("EAH_CSV_PATH", "").strip()
            or os.path.join(DATA_DIR, "EAH Active Academics.csv"))


class EAHFileMissing(Exception):
    """Raised when no EAH extract is present at EAH_PATH."""

SCHOOL_CONFIG = {
    "hwsph": {
        "json_path": os.path.join(DATA_DIR, "faculty.json"),
        "filter": lambda r: r.get("Division / School", "").strip() == "School of Public Health",
        "has_subdepartment": False,
    },
    "jacobs": {
        "json_path": os.path.join(DATA_DIR, "jacobs_faculty.json"),
        "filter": lambda r: r.get("Division / School", "").strip() == "Jacobs School of Engineering",
        "has_subdepartment": True,
    },
    "sio": {
        "json_path": os.path.join(DATA_DIR, "sio_faculty.json"),
        "filter": lambda r: "SIO" in r.get("Division / School", "") or r.get("Division / School", "").strip() == "VC-SIO Other",
        "has_subdepartment": True,
    },
}

# EAH CSV column -> faculty record field mapping
EAH_FIELD_MAP = {
    "Employee Class": "employee_class",
    "Job Code": "job_code",
    "Job Code Description": "job_code_description",
    "PI Eligibility Flag Current": "pi_eligible",
    "VC Area": "vc_area",
    "Division / School": "division_school",
    "Dept / Unit": "department_unit",
    "Department L2": "department_l2",
    "Department L3": "department_l3",
    "Department L4": "department_l4",
    "Department L5": "department_l5",
    "Department": "department_eah",
    "Department Code": "department_code",
}

TITLE_PATTERNS = [
    (r"^PROF[-\s]", "Professor"),
    (r"^ASSOC PROF[-\s]", "Associate Professor"),
    (r"^ASSOC ADJ PROF[-\s]", "Associate Adjunct Professor"),
    (r"^ASST PROF[-\s]", "Assistant Professor"),
    (r"^ASST ADJ PROF[-\s]", "Assistant Adjunct Professor"),
    (r"^ASST RES[-\s]", "Assistant Researcher"),
    (r"^ASSOC RES[-\s]", "Associate Researcher"),
    (r"^RES SCNTST[-\s]", "Research Scientist"),
    (r"^HS CLIN PROF[-\s]", "Health Sciences Clinical Professor"),
    (r"^HS ASSOC CLIN PROF[-\s]", "Health Sciences Associate Clinical Professor"),
    (r"^HS ASST CLIN PROF[-\s]", "Health Sciences Assistant Clinical Professor"),
    (r"^PROF OF CLIN[-\s]", "Professor of Clinical Medicine"),
    (r"^ASSOC PROF OF CLIN[-\s]", "Associate Professor of Clinical Medicine"),
    (r"^ASST PROF OF CLIN[-\s]", "Assistant Professor of Clinical Medicine"),
    (r"^PROF EMERITUS", "Professor Emeritus"),
    (r"^NON-SENATE ACAD EMERITUS", "Professor Emeritus"),
    (r"^HHMI INVESTIGATOR", "HHMI Investigator"),
    (r"^LECTURER", "Lecturer"),
    (r"^SR LECTURER", "Senior Lecturer"),
    (r"^ADJUNCT PROF[-\s]", "Adjunct Professor"),
    (r"^ACT PROF[-\s]", "Acting Professor"),
    (r"^PROF IN RES[-\s]", "Professor in Residence"),
    (r"^ASSOC PROF IN RES[-\s]", "Associate Professor in Residence"),
    (r"^ASST PROF IN RES[-\s]", "Assistant Professor in Residence"),
    (r"^VISITING", "Visiting Professor"),
    (r"^COLLEGE PROVOST", "College Provost"),
    (r"^DEAN", "Dean"),
    (r"^ASSOC DEAN", "Associate Dean"),
    (r"^ASST DEAN", "Assistant Dean"),
]


def normalize_name(s):
    """Remove non-alpha chars and lowercase for comparison."""
    return re.sub(r"[^a-z]", "", s.lower())


def email_local(email):
    """Return the local part of an email address (before @)."""
    if not email or "@" not in email:
        return (email or "").lower().strip()
    return email.split("@")[0].lower().strip()


def parse_eah_name(name_str):
    """Parse 'Last, First Middle' into (first_name, last_name)."""
    name_str = name_str.strip()
    if "," not in name_str:
        parts = name_str.split()
        return (parts[0] if parts else "", " ".join(parts[1:]) if len(parts) > 1 else "")
    last, rest = name_str.split(",", 1)
    first_parts = rest.strip().split()
    first = first_parts[0] if first_parts else ""
    return first.strip(), last.strip()


def map_title(job_code_desc):
    """Map EAH Job Code Description to a human-readable title."""
    if not job_code_desc:
        return None
    desc = job_code_desc.strip().upper()
    for pattern, title in TITLE_PATTERNS:
        if re.search(pattern, desc):
            return title
    # DIRECTOR and other administrative codes — don't override existing title
    return None


def load_eah():
    """Load and parse the EAH CSV, returning all rows.

    Raises EAHFileMissing if no extract has been uploaded yet, so callers
    (cron/admin) can skip gracefully instead of crashing.
    """
    if not os.path.exists(EAH_PATH):
        raise EAHFileMissing(f"No EAH extract found at {EAH_PATH}")
    rows = []
    with open(EAH_PATH, encoding="latin-1") as f:
        # Skip 3 header/info rows
        for _ in range(3):
            next(f)
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def filter_and_deduplicate(eah_rows, school_filter):
    """Filter EAH rows for a school and deduplicate by person.

    For people with multiple rows, prefer the row with a PROF-like title
    (most representative academic appointment).
    """
    school_rows = [r for r in eah_rows if school_filter(r)]

    # Group by (email, name) to handle duplicates
    by_person = {}
    for row in school_rows:
        email = row.get("Email", "").strip().lower()
        name = row.get("Employee Name", "").strip()
        key = email or name
        if key not in by_person:
            by_person[key] = []
        by_person[key].append(row)

    # Pick best row per person (prefer PROF in job code description)
    deduped = {}
    for key, rows in by_person.items():
        best = rows[0]
        for r in rows:
            desc = (r.get("Job Code Description") or "").upper()
            if "PROF" in desc and "PROF" not in (best.get("Job Code Description") or "").upper():
                best = r
        deduped[key] = best

    return deduped


def build_eah_indices(deduped):
    """Build lookup indices for matching."""
    by_email = {}          # full email -> row
    by_email_local = {}    # local part of email -> row
    by_name = {}           # (norm_first, norm_last) -> row

    for key, row in deduped.items():
        email = row.get("Email", "").strip().lower()
        if email:
            by_email[email] = row
            local = email_local(email)
            if local:
                by_email_local[local] = row

        first, last = parse_eah_name(row.get("Employee Name", ""))
        nf, nl = normalize_name(first), normalize_name(last)
        if nf and nl:
            by_name[(nf, nl)] = row

    return by_email, by_email_local, by_name


def _names_compatible(our_first, our_last, eah_row):
    """Check if faculty name is compatible with an EAH record's name."""
    eah_first_raw, eah_last_raw = parse_eah_name(eah_row.get("Employee Name", ""))
    eah_first = normalize_name(eah_first_raw)
    eah_last = normalize_name(eah_last_raw)
    if not eah_first or not eah_last or not our_first or not our_last:
        return False
    # Last names must match (or one contains the other for hyphenated names)
    if our_last != eah_last and our_last not in eah_last and eah_last not in our_last:
        return False
    # First names must share a prefix (handles middle names, nicknames)
    if not (eah_first.startswith(our_first[:3]) or our_first.startswith(eah_first[:3])):
        return False
    return True


def match_faculty_to_eah(faculty, by_email, by_email_local, by_name):
    """Try to match a faculty record to an EAH record. Returns the EAH row or None."""
    our_email = (faculty.get("email") or "").strip().lower()
    our_local = email_local(our_email)
    our_first = normalize_name(faculty.get("first_name") or "")
    our_last = normalize_name(faculty.get("last_name") or "")

    # Tier 1: exact email + name cross-validation
    if our_email and our_email in by_email:
        row = by_email[our_email]
        if _names_compatible(our_first, our_last, row):
            return row
        # Email matches but name doesn't — likely a wrong email in our data

    # Tier 2: email local part + name cross-validation
    if our_local and our_local in by_email_local:
        row = by_email_local[our_local]
        if _names_compatible(our_first, our_last, row):
            return row

    # Tier 3: name matching
    if our_first and our_last:
        # Exact first+last
        if (our_first, our_last) in by_name:
            return by_name[(our_first, our_last)]

        # Last name match (exact, or one contains the other for hyphenated)
        # + first name prefix match
        for (nf, nl), row in by_name.items():
            last_ok = (nl == our_last or nl in our_last or our_last in nl)
            first_ok = (nf.startswith(our_first) or our_first.startswith(nf))
            if last_ok and first_ok:
                return row

    return None


def apply_eah_fields(faculty, eah_row, updates_tracker):
    """Apply EAH fields to a faculty record. Returns the updated record."""
    # Update email (EAH is source of truth)
    eah_email = (eah_row.get("Email") or "").strip()
    if eah_email:
        old_email = faculty.get("email", "")
        if old_email != eah_email:
            updates_tracker["email"] += 1
        faculty["email"] = eah_email

    # Update name from EAH
    first, last = parse_eah_name(eah_row.get("Employee Name", ""))
    if first and first != faculty.get("first_name", ""):
        updates_tracker["first_name"] += 1
        faculty["first_name"] = first
    if last and last != faculty.get("last_name", ""):
        updates_tracker["last_name"] += 1
        faculty["last_name"] = last

    # Update title
    mapped_title = map_title(eah_row.get("Job Code Description", ""))
    if mapped_title:
        old_title = faculty.get("title", "")
        if not old_title or old_title != mapped_title:
            updates_tracker["title"] += 1
            faculty["title"] = mapped_title

    # Add all EAH-specific fields
    for csv_col, json_field in EAH_FIELD_MAP.items():
        eah_value = (eah_row.get(csv_col) or "").strip()

        # Special handling for pi_eligible: convert Y/N to boolean
        if json_field == "pi_eligible":
            if eah_value:
                faculty[json_field] = eah_value.upper() == "Y"
            elif json_field not in faculty:
                faculty[json_field] = None
            continue

        # Don't zero out: if EAH value is blank and we have data, keep ours
        if not eah_value:
            if json_field not in faculty:
                faculty[json_field] = ""
            continue

        faculty[json_field] = eah_value

    # EAH status from Column1
    eah_status = (eah_row.get("Column1") or "").strip()
    if eah_status:
        faculty["eah_status"] = eah_status
    elif "eah_status" not in faculty:
        faculty["eah_status"] = ""

    return faculty


def create_new_faculty(eah_row, has_subdepartment):
    """Create a new faculty record from an EAH row."""
    first, last = parse_eah_name(eah_row.get("Employee Name", ""))
    mapped_title = map_title(eah_row.get("Job Code Description", "")) or ""

    record = {
        "first_name": first,
        "last_name": last,
        "title": mapped_title,
        "degrees": [],
        "email": (eah_row.get("Email") or "").strip(),
        "research_interests": "",
        "research_interests_enriched": "",
        "expertise_keywords": [],
        "methodologies": [],
        "disease_areas": [],
        "populations": [],
        "funded_grants": [],
        "recent_publications": [],
        "committee_service": [],
        "integrity_flags": [],
    }

    if has_subdepartment:
        dept_unit = (eah_row.get("Dept / Unit") or "").strip()
        record["subdepartment"] = dept_unit.title() if dept_unit else ""

    # Add all EAH fields
    for csv_col, json_field in EAH_FIELD_MAP.items():
        eah_value = (eah_row.get(csv_col) or "").strip()
        if json_field == "pi_eligible":
            record[json_field] = eah_value.upper() == "Y" if eah_value else None
        else:
            record[json_field] = eah_value

    eah_status = (eah_row.get("Column1") or "").strip()
    record["eah_status"] = eah_status

    return record


def save_json_atomic(data, path):
    """Atomically write JSON data to a file."""
    dir_path = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def process_school(school_name, config, eah_rows):
    """Process a single school: match, update, flag, add new."""
    print(f"\n{'='*60}")
    print(f"Processing: {school_name.upper()}")
    print(f"{'='*60}")

    # Load existing faculty data
    with open(config["json_path"]) as f:
        data = json.load(f)
    faculty_list = data["faculty"]
    original_count = len(faculty_list)

    # Filter and deduplicate EAH records for this school
    deduped = filter_and_deduplicate(eah_rows, config["filter"])
    print(f"  Faculty in our data: {original_count}")
    print(f"  Unique people in EAH: {len(deduped)}")

    # Build lookup indices
    by_email, by_email_local, by_name = build_eah_indices(deduped)

    # Track what was matched
    matched_eah_keys = set()
    matched_count = 0
    removed_inactive = []
    updates_tracker = defaultdict(int)

    # Match each existing faculty member; collect unmatched for removal
    matched_faculty = []
    for faculty in faculty_list:
        eah_row = match_faculty_to_eah(faculty, by_email, by_email_local, by_name)

        if eah_row:
            matched_count += 1
            eah_key = (eah_row.get("Email", "").strip().lower() or
                       eah_row.get("Employee Name", "").strip())
            matched_eah_keys.add(eah_key)

            # Apply EAH fields
            apply_eah_fields(faculty, eah_row, updates_tracker)
            matched_faculty.append(faculty)
        else:
            # Remove: not found in EAH Active Academics
            name = f"{faculty.get('first_name', '')} {faculty.get('last_name', '')}"
            removed_inactive.append(name)

    # Replace faculty list with only matched (active) faculty
    faculty_list[:] = matched_faculty

    # Deduplicate: if multiple existing records matched the same EAH person,
    # keep the one with the most enriched data and remove the others.
    seen_emails = {}
    to_remove = []
    for i, faculty in enumerate(faculty_list):
        email = (faculty.get("email") or "").strip().lower()
        if not email:
            continue
        if email in seen_emails:
            prev_idx = seen_emails[email]
            prev = faculty_list[prev_idx]
            # Score by data richness
            def _richness(f):
                score = 0
                if f.get("research_interests_enriched"): score += 3
                if f.get("funded_grants"): score += len(f["funded_grants"])
                if f.get("recent_publications"): score += len(f["recent_publications"])
                if f.get("expertise_keywords"): score += 1
                if f.get("orcid"): score += 1
                return score
            if _richness(faculty) > _richness(prev):
                to_remove.append(prev_idx)
                seen_emails[email] = i
            else:
                to_remove.append(i)
        else:
            seen_emails[email] = i
    if to_remove:
        for idx in sorted(to_remove, reverse=True):
            removed = faculty_list.pop(idx)
            print(f"  Removed duplicate: {removed.get('first_name')} {removed.get('last_name')} ({removed.get('email')})")

    # Find EAH records not matched to any existing faculty -> add new
    new_faculty = []
    for key, row in deduped.items():
        if key not in matched_eah_keys:
            new_record = create_new_faculty(row, config["has_subdepartment"])
            new_faculty.append(new_record)
            faculty_list.append(new_record)

    # Sort faculty list by last name, first name for consistency
    faculty_list.sort(key=lambda f: (
        (f.get("last_name") or "").lower(),
        (f.get("first_name") or "").lower()
    ))

    # Save
    save_json_atomic(data, config["json_path"])

    # Report
    print(f"  Matched: {matched_count}")
    print(f"  Removed (not in EAH): {len(removed_inactive)}")
    print(f"  New faculty added: {len(new_faculty)}")
    print(f"  Total faculty now: {len(faculty_list)}")

    if updates_tracker:
        print(f"  Fields updated: {dict(updates_tracker)}")

    if removed_inactive:
        print(f"\n  --- Removed ({len(removed_inactive)}) ---")
        for name in sorted(removed_inactive):
            print(f"    {name}")

    if new_faculty:
        print(f"\n  --- New Faculty Added ({len(new_faculty)}) ---")
        for f in sorted(new_faculty, key=lambda x: x.get("last_name", "")):
            print(f"    {f['first_name']} {f['last_name']} ({f.get('email', '')})")

    return {
        "school": school_name,
        "original_count": original_count,
        "eah_count": len(deduped),
        "matched": matched_count,
        "removed_inactive": len(removed_inactive),
        "new_added": len(new_faculty),
        "total_now": len(faculty_list),
        "updates": dict(updates_tracker),
    }


def run_eah_reconcile():
    """Reconcile every tracked school against the EAH extract.

    Returns an aggregate summary dict (per-school results plus totals) so
    callers such as the admin upload handler and the scheduler can surface
    what changed. Raises EAHFileMissing if no extract is present.
    """
    print("=" * 60)
    print("EAH Enrichment Pass")
    print("=" * 60)

    # Load EAH data once
    eah_rows = load_eah()
    print(f"Loaded {len(eah_rows)} EAH rows")

    results = []
    for school_name, config in SCHOOL_CONFIG.items():
        result = process_school(school_name, config, eah_rows)
        results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    total_matched = sum(r["matched"] for r in results)
    total_flagged = sum(r["removed_inactive"] for r in results)
    total_new = sum(r["new_added"] for r in results)
    total_now = sum(r["total_now"] for r in results)
    print(f"  Total matched: {total_matched}")
    print(f"  Total removed (not in EAH): {total_flagged}")
    print(f"  Total new faculty added: {total_new}")
    print(f"  Total faculty across all schools: {total_now}")

    return {
        "eah_rows": len(eah_rows),
        "schools": results,
        "total_matched": total_matched,
        "total_removed_inactive": total_flagged,
        "total_new_added": total_new,
        "total_now": total_now,
    }


def main():
    try:
        run_eah_reconcile()
        return 0
    except EAHFileMissing as e:
        print(f"SKIP: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
