#!/usr/bin/env python3
"""Reconcile the Employee Activity Hub (EAH) extract against the faculty DB.

Reads the EAH Active Academics CSV and reconciles it against SQLite — the
source of truth — for EVERY UCSD division (not just the three originally
tracked schools). For each division:

  * matched faculty get refreshed contact/HR fields,
  * faculty matched in a DIFFERENT division are moved (EAH wins),
  * faculty absent from EAH are soft-flagged eah_status='Inactive'
    (never deleted — purges are admin-confirmed via
    data.db.purge_flagged_faculty),
  * EAH people with no existing row are inserted with
    identity_status='unresolved' so identity resolution picks them up.
"""

import csv
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from data.divisions import division_for
from utils.names import (email_local, names_compatible, normalize_name,
                         parse_eah_name)

# Writable state dir (kept for the local/dev EAH default path below).
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


def deduplicate_people(rows):
    """Deduplicate EAH rows by person (email, else name).

    For people with multiple rows, prefer the row with a PROF-like title
    (most representative academic appointment).
    """
    by_person = defaultdict(list)
    for row in rows:
        email = row.get("Email", "").strip().lower()
        name = row.get("Employee Name", "").strip()
        by_person[email or name].append(row)

    deduped = {}
    for key, person_rows in by_person.items():
        best = person_rows[0]
        for r in person_rows:
            desc = (r.get("Job Code Description") or "").upper()
            if "PROF" in desc and "PROF" not in (best.get("Job Code Description") or "").upper():
                best = r
        deduped[key] = best
    return deduped


def eah_person_key(row):
    return (row.get("Email", "").strip().lower()
            or row.get("Employee Name", "").strip())


def build_eah_indices(deduped):
    """Build lookup indices for matching."""
    by_email = {}          # full email -> row
    by_email_local = {}    # local part of email -> row
    by_name = {}           # (norm_first, norm_last) -> row

    for row in deduped.values():
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


def _names_compatible_with_row(our_first, our_last, eah_row):
    eah_first_raw, eah_last_raw = parse_eah_name(eah_row.get("Employee Name", ""))
    return names_compatible(our_first, our_last,
                            normalize_name(eah_first_raw),
                            normalize_name(eah_last_raw))


def match_faculty_to_eah(faculty, by_email, by_email_local, by_name):
    """Try to match a faculty record to an EAH record. Returns the EAH row or None."""
    our_email = (faculty.get("email") or "").strip().lower()
    our_local = email_local(our_email)
    our_first = normalize_name(faculty.get("first_name") or "")
    our_last = normalize_name(faculty.get("last_name") or "")

    # Tier 1: exact email + name cross-validation
    if our_email and our_email in by_email:
        row = by_email[our_email]
        if _names_compatible_with_row(our_first, our_last, row):
            return row
        # Email matches but name doesn't — likely a wrong email in our data

    # Tier 2: email local part + name cross-validation
    if our_local and our_local in by_email_local:
        row = by_email_local[our_local]
        if _names_compatible_with_row(our_first, our_last, row):
            return row

    # Tier 3: name matching
    if our_first and our_last:
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
    elif not faculty.get("eah_status") or faculty.get("eah_status") in ("Inactive", "Duplicate"):
        faculty["eah_status"] = "Active"

    return faculty


def create_new_faculty(eah_row):
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
        "identity_status": "unresolved",
    }

    dept_unit = (eah_row.get("Dept / Unit") or "").strip()
    record["subdepartment"] = dept_unit.title() if dept_unit else ""

    # Add all EAH fields
    for csv_col, json_field in EAH_FIELD_MAP.items():
        eah_value = (eah_row.get(csv_col) or "").strip()
        if json_field == "pi_eligible":
            record[json_field] = eah_value.upper() == "Y" if eah_value else None
        else:
            record[json_field] = eah_value

    record["eah_status"] = (eah_row.get("Column1") or "").strip() or "Active"
    return record


def run_eah_reconcile():
    """Reconcile every division against the EAH extract, directly in SQLite.

    Returns an aggregate summary dict (per-division results plus totals).
    Raises EAHFileMissing if no extract is present.
    """
    print("=" * 60)
    print("EAH Reconcile (DB-native, all divisions)")
    print("=" * 60)

    eah_rows = load_eah()
    print(f"Loaded {len(eah_rows)} EAH rows")

    # Group EAH rows by division slug.
    rows_by_division = defaultdict(list)
    labels = {}
    for row in eah_rows:
        slug, label, _ = division_for(row.get("Division / School", ""))
        rows_by_division[slug].append(row)
        labels[slug] = label

    conn = db.get_write_conn()
    db.init_schema(conn)

    results = []
    updates_tracker = defaultdict(int)
    moved = 0

    # Global person index (across all divisions) so a division transfer is
    # detected as a move, not an inactive-flag + duplicate-insert.
    global_deduped = deduplicate_people(eah_rows)
    g_email, g_local, g_name = build_eah_indices(global_deduped)

    matched_person_keys = set()

    # Pass 1: reconcile every existing DB row (all divisions at once).
    existing = db.fetch_for_enrichment(conn, department=None)["faculty"]
    print(f"Faculty rows in DB: {len(existing)}")
    matched = flagged = 0
    for faculty in existing:
        eah_row = match_faculty_to_eah(faculty, g_email, g_local, g_name)
        fid = faculty["_db_id"]
        if not eah_row:
            if (faculty.get("eah_status") or "") not in ("Inactive", "Duplicate"):
                db.mark_eah_status(conn, fid, "Inactive")
            flagged += 1
            continue

        person_key = eah_person_key(eah_row)
        if person_key in matched_person_keys:
            # A previous (richer-keyed) row already claimed this person.
            db.mark_eah_status(conn, fid, "Duplicate")
            continue
        matched_person_keys.add(person_key)
        matched += 1

        apply_eah_fields(faculty, eah_row, updates_tracker)
        slug, label, _ = division_for(eah_row.get("Division / School", ""))
        if slug != faculty.get("department"):
            db.update_faculty_division(conn, fid, slug, label)
            moved += 1
        db.save_faculty_record(conn, fid, faculty)
    conn.commit()

    # Pass 2: insert EAH people with no existing row, division by division.
    total_new = 0
    for slug in sorted(rows_by_division):
        deduped = deduplicate_people(rows_by_division[slug])
        new_count = 0
        for person_key, row in deduped.items():
            if person_key in matched_person_keys:
                continue
            record = create_new_faculty(row)
            record["department_label"] = labels[slug]
            db.upsert_faculty(conn, slug, record)
            new_count += 1
        conn.commit()
        total_new += new_count
        results.append({
            "division": slug,
            "label": labels[slug],
            "eah_count": len(deduped),
            "new_added": new_count,
        })
        print(f"  {slug:12s} EAH people: {len(deduped):5d}  new rows: {new_count}")

    # Mark EAH as the roster authority. Once this key is set the EAH PI-eligible
    # set defines who exists, so the JSON bootstrap (migrate_json_to_sqlite)
    # stops re-importing the legacy three-school rosters and can never resurrect
    # stale rows or re-flip EAH-owned fields on a container restart.
    db.set_meta(conn, "eah_reconciled_at", db._now_iso())
    conn.commit()

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  Matched (existing rows refreshed): {matched}")
    print(f"  Moved between divisions: {moved}")
    print(f"  Flagged inactive (kept, excluded from matching): {flagged}")
    print(f"  New faculty added: {total_new}")
    if updates_tracker:
        print(f"  Fields updated: {dict(updates_tracker)}")

    return {
        "eah_rows": len(eah_rows),
        "divisions": results,
        "total_matched": matched,
        "total_moved": moved,
        "total_removed_inactive": flagged,   # key kept for admin UI compat
        "total_new_added": total_new,
        "updates": dict(updates_tracker),
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
