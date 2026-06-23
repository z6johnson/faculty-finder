#!/usr/bin/env python3
"""Seed script for Jacobs School of Engineering faculty.

Discovers Jacobs faculty from the school's public directory and creates
the initial jacobs_faculty.json seed file for enrichment.

Usage:
    python enrichment/seed_jacobs.py                    # Run all strategies
    python enrichment/seed_jacobs.py --strategy catalog  # Catalog page only
    python enrichment/seed_jacobs.py --strategy profiles # profiles.ucsd.edu only
    python enrichment/seed_jacobs.py --dry-run           # Show what would be added
"""

import argparse
import json
import logging
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "jacobs_faculty.json")

HEADERS = {
    "User-Agent": "UCSD-GrantMatch/1.0 (academic research tool; "
                   "contact: hwsph-grants@ucsd.edu)",
}

# Known Jacobs departments for extraction
JACOBS_DEPARTMENTS = [
    "Bioengineering",
    "Chemical and Nano Engineering",
    "Computer Science & Engineering",
    "Computer Science and Engineering",
    "Electrical and Computer Engineering",
    "Mechanical & Aerospace Engineering",
    "Mechanical and Aerospace Engineering",
    "Structural Engineering",
    "NanoEngineering",
]

# Academic title patterns
TITLE_PATTERNS = [
    "Distinguished Professor",
    "Research Professor",
    "Professor Emerita",
    "Professor Emeritus",
    "Professor Practice",
    "Professor",
    "Associate Professor",
    "Assistant Professor",
    "Associate Teaching Professor",
    "Assistant Teaching Professor",
    "Teaching Professor",
    "Research Scientist",
    "Senior Lecturer",
    "Lecturer",
    "Faculty-Affiliate",
]


def _extract_department(title_text):
    """Extract subdepartment from a title string."""
    for dept in JACOBS_DEPARTMENTS:
        if dept.lower() in title_text.lower():
            # Normalize variants
            return dept.replace("and Aerospace", "& Aerospace").replace(
                "and Nano", "and Nano"
            ).replace("and Computer", "and Computer")
    return ""


def _extract_title(title_text):
    """Extract academic title from a combined title string."""
    for t in TITLE_PATTERNS:
        if t.lower() in title_text.lower():
            return t
    return ""


def _parse_name(name_str):
    """Parse 'Last, First' or 'Last, First Middle' into components."""
    name_str = name_str.strip()
    # Remove quoted nicknames like "Raj"
    name_str = re.sub(r'"[^"]*"', "", name_str).strip()

    if "," not in name_str:
        parts = name_str.split()
        if len(parts) >= 2:
            return parts[0], parts[-1]
        return name_str, ""

    parts = name_str.split(",", 1)
    last_name = parts[0].strip()
    first_name = parts[1].strip().split()[0] if parts[1].strip() else ""
    return first_name, last_name


def discover_jacobs_faculty_from_directory():
    """Scrape the Jacobs School faculty directory page.

    The directory at jacobsschool.ucsd.edu/faculty/profiles uses a
    Drupal Views infinite scroll. We paginate through it to collect
    all entries.
    """
    base_url = "https://jacobsschool.ucsd.edu/faculty/profiles"
    faculty = []
    page = 0

    while True:
        url = base_url if page == 0 else f"{base_url}?page={page}"
        logger.info("Fetching directory page %d: %s", page, url)

        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Failed to fetch page %d: %s", page, e)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        view_content = soup.select_one(".view-content")
        if not view_content:
            break

        cols = view_content.select("[class*='col-']")
        if not cols:
            break

        page_count = 0
        for i in range(0, len(cols), 3):
            if i + 1 >= len(cols):
                break

            name_col = cols[i + 1]
            kw_col = cols[i + 2] if i + 2 < len(cols) else None

            lines = [l.strip() for l in name_col.get_text().split("\n") if l.strip()]
            if not lines:
                continue

            name_str = lines[0]
            title_str = ", ".join(lines[1:]).strip()
            keywords = kw_col.get_text(strip=True) if kw_col else ""

            first_name, last_name = _parse_name(name_str)
            if not first_name or not last_name:
                continue

            # Filter obvious non-person entries
            if any(c.isdigit() for c in last_name):
                continue

            faculty.append({
                "first_name": first_name,
                "last_name": last_name,
                "title": _extract_title(title_str),
                "subdepartment": _extract_department(title_str),
                "research_interests": keywords,
            })
            page_count += 1

        logger.info("  Found %d entries on page %d", page_count, page)

        if page_count == 0:
            break

        page += 1

        # Safety limit
        if page > 60:
            break

    return faculty


def discover_jacobs_faculty_from_catalog():
    """Scrape the UCSD catalog Jacobs/engineering faculty listing.

    Catalog pages cover multiple engineering departments.
    """
    # Jacobs departments in the catalog
    catalog_urls = {
        "BE": "https://catalog.ucsd.edu/faculty/BE.html",
        "CENG": "https://catalog.ucsd.edu/faculty/CENG.html",
        "CSE": "https://catalog.ucsd.edu/faculty/CSE.html",
        "ECE": "https://catalog.ucsd.edu/faculty/ECE.html",
        "MAE": "https://catalog.ucsd.edu/faculty/MAE.html",
        "SE": "https://catalog.ucsd.edu/faculty/SE.html",
        "NANO": "https://catalog.ucsd.edu/faculty/NANO.html",
    }

    dept_map = {
        "BE": "Bioengineering",
        "CENG": "Chemical and Nano Engineering",
        "CSE": "Computer Science & Engineering",
        "ECE": "Electrical and Computer Engineering",
        "MAE": "Mechanical & Aerospace Engineering",
        "SE": "Structural Engineering",
        "NANO": "NanoEngineering",
    }

    faculty = []

    for code, url in catalog_urls.items():
        logger.info("Fetching catalog page for %s: %s", code, url)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning("Failed to fetch catalog for %s: %s", code, e)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.find_all(["p", "li", "div"]):
            text = el.get_text(strip=True)
            if not text or len(text) < 5 or len(text) > 500:
                continue
            if "," not in text:
                continue

            title = _extract_title(text)
            if not title:
                continue

            parts = text.split(",")
            last_name = parts[0].strip()
            first_part = parts[1].strip() if len(parts) > 1 else ""
            first_name = first_part.split()[0] if first_part else ""

            if not first_name or not last_name:
                continue
            if any(c.isdigit() for c in last_name):
                continue

            faculty.append({
                "first_name": first_name,
                "last_name": last_name,
                "title": title,
                "subdepartment": dept_map.get(code, ""),
            })

    # Deduplicate
    seen = set()
    unique = []
    for f in faculty:
        key = (f["first_name"].lower(), f["last_name"].lower())
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return unique


def merge_faculty_lists(*lists):
    """Merge multiple faculty lists, deduplicating by (first_name, last_name).

    When duplicates are found, prefer the entry with more fields populated.
    """
    by_key = {}
    for faculty_list in lists:
        for entry in faculty_list:
            key = (entry["first_name"].lower(), entry["last_name"].lower())
            existing = by_key.get(key)
            if existing is None:
                by_key[key] = entry
            else:
                for field, value in entry.items():
                    if value and not existing.get(field):
                        existing[field] = value

    return sorted(by_key.values(), key=lambda f: (f["last_name"].lower(), f["first_name"].lower()))


def main():
    parser = argparse.ArgumentParser(description="Seed Jacobs faculty data")
    parser.add_argument("--strategy", choices=["directory", "catalog", "all"],
                        default="all", help="Discovery strategy to use")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show results without writing to file")
    parser.add_argument("--force", action="store_true",
                        help="Override the EAH-authority deprecation guard.")
    args = parser.parse_args()

    # DEPRECATED as a roster source. EAH (scripts/eah_enrichment.py) now defines
    # who exists; this scraper writes a JSON roster that would compete with that
    # authority. Jacobs profile pages still feed enrichment via the ucsd_profile
    # source — that path is unaffected.
    if not args.force:
        print("This Jacobs seeder is retired: the roster is now EAH-authoritative. "
              "Profile pages still feed enrichment via the ucsd_profile source. "
              "Re-run with --force only for an explicit ad-hoc rebuild.")
        return 1

    directory_faculty = []
    catalog_faculty = []

    if args.strategy in ("directory", "all"):
        print("=== Strategy 1: Jacobs School directory ===")
        try:
            directory_faculty = discover_jacobs_faculty_from_directory()
            for f in directory_faculty:
                f["_from_directory"] = True
            print(f"  Found {len(directory_faculty)} faculty from directory")
        except Exception as e:
            print(f"  Directory scrape failed: {e}")
            logger.exception("Directory discovery failed")

    if args.strategy in ("catalog", "all"):
        print("\n=== Strategy 2: UCSD catalog pages ===")
        try:
            catalog_faculty = discover_jacobs_faculty_from_catalog()
            for f in catalog_faculty:
                f["_from_catalog"] = True
            print(f"  Found {len(catalog_faculty)} faculty from catalog")
        except Exception as e:
            print(f"  Catalog scrape failed: {e}")
            logger.exception("Catalog discovery failed")

    merged = merge_faculty_lists(directory_faculty, catalog_faculty)

    print(f"\n=== Results ===")
    print(f"Total unique faculty discovered: {len(merged)}")

    if args.dry_run:
        print("\n=== Dry run — not writing to file ===")
        for f in merged[:20]:
            name = f"{f['first_name']} {f['last_name']}"
            title = f.get("title", "Unknown")
            dept = f.get("subdepartment", "")
            print(f"  {name} — {title}, {dept}")
        if len(merged) > 20:
            print(f"  ... and {len(merged) - 20} more")
        return 0

    # Clean internal tracking fields before saving
    for f in merged:
        for key in list(f.keys()):
            if key.startswith("_"):
                del f[key]

    # Ensure each entry has the expected schema fields
    for f in merged:
        f.setdefault("degrees", [])
        f.setdefault("title", "")
        f.setdefault("subdepartment", "")
        f.setdefault("email", "")
        f.setdefault("research_interests", "")
        f.setdefault("research_interests_enriched", "")
        f.setdefault("expertise_keywords", [])
        f.setdefault("methodologies", [])
        f.setdefault("disease_areas", [])
        f.setdefault("populations", [])
        f.setdefault("funded_grants", [])
        f.setdefault("recent_publications", [])
        f.setdefault("profile_url", "")
        f.setdefault("orcid", "")
        f.setdefault("h_index", None)
        f.setdefault("committee_service", [])
        f.setdefault("integrity_flags", [])
        f.setdefault("last_enriched", None)

    # Load existing file to preserve metadata, or create fresh
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH) as fp:
            data = json.load(fp)
    else:
        data = {
            "university": "UC San Diego",
            "department": "Jacobs School of Engineering",
            "source_url": "https://jacobsschool.ucsd.edu/faculty/profiles",
            "enrichment_sources": [
                {"name": "ucsd_profile", "description": "UCSD profile pages", "confidence": 1.0},
                {"name": "nsf_awards", "description": "NSF Award Search API", "confidence": 0.8},
                {"name": "nih_reporter", "description": "NIH RePORTER API", "confidence": 0.8},
                {"name": "pubmed", "description": "PubMed/NCBI E-utilities", "confidence": 0.7},
                {"name": "orcid", "description": "ORCID public API", "confidence": 0.9},
                {"name": "semantic_scholar", "description": "Semantic Scholar API", "confidence": 0.75},
            ],
        }

    data["faculty"] = merged
    data["seed_status"] = "seeded"
    data["date_retrieved"] = "2026-03-19"

    with open(DATA_PATH, "w") as fp:
        json.dump(data, fp, indent=2, ensure_ascii=False)
        fp.write("\n")

    print(f"\nWrote {len(merged)} faculty to {DATA_PATH}")
    print("Next step: run enrichment with 'ENRICH_DEPARTMENT=jacobs python enrichment/run.py'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
