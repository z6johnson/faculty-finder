"""Enrichment runner for GitHub Actions.

Called directly: python enrichment/run.py

Configuration via environment variables:
    ENRICH_SOURCES: Comma-separated source names (default: all)
    ENRICH_FACULTY_IDS: Comma-separated faculty indices (default: all)
    ENRICH_DRY_RUN: Set to "true" for dry run
    ENRICH_DEPARTMENT: Department key ("sio" for Scripps, default: HWSPH)
"""

import logging
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from enrichment.pipeline import enrich_all, get_enrichment_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    # Parse config from environment
    sources = None
    raw_sources = os.environ.get("ENRICH_SOURCES", "").strip()
    if raw_sources:
        sources = [s.strip() for s in raw_sources.split(",") if s.strip()]

    faculty_ids = None
    raw_ids = os.environ.get("ENRICH_FACULTY_IDS", "").strip()
    if raw_ids:
        faculty_ids = [int(i.strip()) for i in raw_ids.split(",") if i.strip()]

    dry_run = os.environ.get("ENRICH_DRY_RUN", "").lower() == "true"

    department = os.environ.get("ENRICH_DEPARTMENT", "").strip().lower() or None
    dept_label = department or "hwsph"

    # Show pre-enrichment status
    status = get_enrichment_status(department)
    print(f"\n=== Pre-enrichment status ({dept_label}) ===")
    print(f"Total faculty: {status['total_faculty']}")
    print(f"With research interests: {status['with_original_interests']} ({status['coverage_original']}%)")
    print(f"With enriched interests: {status['with_enriched_interests']} ({status['coverage_enriched']}%)")
    print(f"With funded grants: {status['with_funded_grants']}")
    print(f"With publications: {status['with_publications']}")

    # Run enrichment
    config_desc = [f"dept={dept_label}"]
    if sources:
        config_desc.append(f"sources={sources}")
    if faculty_ids:
        config_desc.append(f"faculty_ids={faculty_ids}")
    if dry_run:
        config_desc.append("DRY RUN")
    print(f"\n=== Running enrichment ({', '.join(config_desc)}) ===\n")

    def on_progress(completed, total):
        print(f"Progress: {completed}/{total}")

    # Time budget: leave 5 minutes of headroom for setup/commit steps.
    # Default to 115 minutes (matching the 120-minute workflow timeout).
    time_budget = int(os.environ.get("ENRICH_TIME_BUDGET", 115 * 60))

    results = enrich_all(
        sources=sources,
        faculty_ids=faculty_ids,
        dry_run=dry_run,
        progress_callback=on_progress,
        department=department,
        time_budget_seconds=time_budget,
    )

    # Summary
    enriched = sum(
        1 for r in results
        if any(s.get("status") == "data_found" for s in r.get("sources", {}).values())
    )
    errored = [r for r in results if r.get("error")]
    print(f"\n=== Results ===")
    print(f"Processed: {len(results)}")
    print(f"Data found: {enriched}")
    print(f"Errors: {len(errored)}")
    for r in errored:
        print(f"  - {r.get('name', r.get('faculty_index', '?'))}: {r.get('error')}")

    # Post-enrichment status
    if not dry_run:
        status = get_enrichment_status(department)
        print(f"\n=== Post-enrichment status ({dept_label}) ===")
        print(f"With enriched interests: {status['with_enriched_interests']} ({status['coverage_enriched']}%)")
        print(f"With funded grants: {status['with_funded_grants']}")
        print(f"With publications: {status['with_publications']}")

    # Only fail if more than half of faculty had errors (indicates systemic issue)
    # Transient source failures are normal and should not break the workflow
    if len(errored) > len(results) // 2:
        print(f"\n!!! Too many errors ({len(errored)}/{len(results)}), failing the run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
