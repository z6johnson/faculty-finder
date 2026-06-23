#!/usr/bin/env python3
"""Seed script for Scripps Institution of Oceanography faculty.

Discovers SIO faculty from multiple public sources and creates the
initial sio_faculty.json seed file for enrichment.

Usage:
    python enrichment/seed_sio.py                    # Run all strategies
    python enrichment/seed_sio.py --strategy catalog  # Catalog page only
    python enrichment/seed_sio.py --strategy profiles # profiles.ucsd.edu only
    python enrichment/seed_sio.py --dry-run           # Show what would be added
"""

import argparse
import json
import logging
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from enrichment.sources.scripps_profile import (
    discover_sio_faculty_from_catalog,
    discover_sio_faculty_from_profiles,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sio_faculty.json")


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
                # Merge: keep whichever has more data, fill in missing fields
                for field, value in entry.items():
                    if value and not existing.get(field):
                        existing[field] = value

    # Sort by last name, first name
    return sorted(by_key.values(), key=lambda f: (f["last_name"].lower(), f["first_name"].lower()))


def cross_validate(faculty_list):
    """Add a validation_sources count to each entry.

    Faculty found in multiple independent sources get higher confidence.
    """
    for entry in faculty_list:
        sources = []
        if entry.get("_from_catalog"):
            sources.append("catalog")
        if entry.get("_from_profiles"):
            sources.append("profiles")
        entry["_validation_sources"] = len(sources)
        entry["_validation_notes"] = (
            f"Found in {len(sources)} source(s): {', '.join(sources)}"
            if sources else "Added manually"
        )
    return faculty_list


def main():
    parser = argparse.ArgumentParser(description="Seed SIO faculty data")
    parser.add_argument("--strategy", choices=["catalog", "profiles", "all"],
                        default="all", help="Discovery strategy to use")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show results without writing to file")
    parser.add_argument("--force", action="store_true",
                        help="Override the EAH-authority deprecation guard.")
    args = parser.parse_args()

    # DEPRECATED as a roster source. EAH (scripts/eah_enrichment.py) now defines
    # who exists; this scraper writes a JSON roster that would compete with that
    # authority. The SIO profile pages still feed enrichment via the
    # scripps_profile / ucsd_profile sources — that path is unaffected.
    if not args.force:
        print("This SIO seeder is retired: the roster is now EAH-authoritative. "
              "Profile pages still feed enrichment via the scripps_profile source. "
              "Re-run with --force only for an explicit ad-hoc rebuild.")
        return 1

    catalog_faculty = []
    profiles_faculty = []

    if args.strategy in ("catalog", "all"):
        print("=== Strategy 1: UCSD Catalog page ===")
        try:
            catalog_faculty = discover_sio_faculty_from_catalog()
            for f in catalog_faculty:
                f["_from_catalog"] = True
            print(f"  Found {len(catalog_faculty)} faculty from catalog")
        except Exception as e:
            print(f"  Catalog scrape failed: {e}")
            logger.exception("Catalog discovery failed")

    if args.strategy in ("profiles", "all"):
        print("\n=== Strategy 2: profiles.ucsd.edu department search ===")
        try:
            profiles_faculty = discover_sio_faculty_from_profiles()
            for f in profiles_faculty:
                f["_from_profiles"] = True
            print(f"  Found {len(profiles_faculty)} faculty from profiles.ucsd.edu")
        except Exception as e:
            print(f"  Profiles search failed: {e}")
            logger.exception("Profiles discovery failed")

    # Merge and cross-validate
    merged = merge_faculty_lists(catalog_faculty, profiles_faculty)
    merged = cross_validate(merged)

    print(f"\n=== Results ===")
    print(f"Total unique faculty discovered: {len(merged)}")

    # Show validation stats
    multi_source = sum(1 for f in merged if f.get("_validation_sources", 0) >= 2)
    single_source = sum(1 for f in merged if f.get("_validation_sources", 0) == 1)
    print(f"  Cross-validated (2+ sources): {multi_source}")
    print(f"  Single source: {single_source}")

    if args.dry_run:
        print("\n=== Dry run — not writing to file ===")
        for f in merged[:20]:
            name = f"{f['first_name']} {f['last_name']}"
            title = f.get("title", "Unknown")
            note = f.get("_validation_notes", "")
            print(f"  {name} — {title} ({note})")
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

    # Load existing file to preserve metadata
    with open(DATA_PATH) as fp:
        data = json.load(fp)

    data["faculty"] = merged
    data["seed_status"] = "seeded"
    data["date_retrieved"] = "2026-03-18"

    with open(DATA_PATH, "w") as fp:
        json.dump(data, fp, indent=2, ensure_ascii=False)
        fp.write("\n")

    print(f"\nWrote {len(merged)} faculty to {DATA_PATH}")
    print("Next step: run enrichment with 'ENRICH_DEPARTMENT=sio python enrichment/run.py'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
