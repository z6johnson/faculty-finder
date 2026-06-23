#!/usr/bin/env python3
"""Report enrichment coverage for the PI-eligible faculty roster.

Reads the live SQLite DB (the source of truth) and renders the coverage ledger
across ALL divisions, measured against the EAH PI-eligible population — not the
three legacy JSON snapshots. Each faculty member lands in exactly one funnel
stage (data/db.py::LEDGER_STAGES), so "X% missing" breaks into actionable
buckets instead of one opaque number.
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db

SEP = "=" * 72


def _bar(pct, width=40):
    filled = int(pct / 100 * width)
    return "#" * filled + "." * (width - filled)


def main():
    conn = db.connect(readonly=True)

    by_div = db.load_ledger_by_division(conn, pi_only=True)
    overall = db.load_ledger(conn, pi_only=True)
    stages = db.LEDGER_STAGES

    print(SEP)
    print("COVERAGE LEDGER — PI-eligible faculty (EAH roster)")
    print(SEP)

    for d in by_div:
        total = d["total"]
        if not total:
            continue
        pct = d["coverage_enriched"]
        print(f"\n{d['department_label'] or d['department']} — {total} PI-eligible")
        print(f"  Enriched: {d['buckets']['enriched']:4d}/{total:<4d} "
              f"({pct:5.1f}%) |{_bar(pct)}|")
        for s in stages:
            if s == "enriched":
                continue
            n = d["buckets"][s]
            if n:
                print(f"    {s:28s} {n:4d}")

    # Overall summary
    print(f"\n{SEP}")
    print("OVERALL")
    print(SEP)
    total = overall["total"]
    print(f"  PI-eligible faculty (all divisions): {total}")
    print(f"  Enriched with research interests:    {overall['buckets']['enriched']} "
          f"({overall['coverage_enriched']}%)")
    if overall["unknown_eligibility"]:
        print(f"  Unknown PI-eligibility (excluded):   {overall['unknown_eligibility']}")
    print("\n  Funnel buckets:")
    for s in stages:
        print(f"    {s:28s} {overall['buckets'][s]:5d}")

    # Audit issues, computed from DB columns (not JSON keys).
    print(f"\n{SEP}")
    print("AUDIT ISSUES")
    print(SEP)
    issues = []

    # Resolved + enrichment ran but no usable interests — split by cause.
    sources_dry = overall["buckets"]["sources_dry"]
    no_input = overall["buckets"]["normalizer_no_input"]
    if sources_dry:
        issues.append(f"{sources_dry} resolved+enriched faculty have material but no "
                      f"enriched interests — likely wrong identity match (recheck identity).")
    if no_input:
        issues.append(f"{no_input} resolved+enriched faculty had no input to synthesize "
                      f"from — needs sources/identity work, not re-running the LLM.")

    # Duplicate / missing names.
    dupes = conn.execute(
        "SELECT lower(first_name || '|' || last_name) AS k, COUNT(*) AS n"
        " FROM faculty WHERE pi_eligible = 1 GROUP BY k HAVING n > 1"
    ).fetchall()
    if dupes:
        issues.append(f"{len(dupes)} duplicate name(s) among PI-eligible faculty.")
    nameless = conn.execute(
        "SELECT COUNT(*) FROM faculty WHERE pi_eligible = 1 AND"
        " (first_name IS NULL OR first_name = '' OR last_name IS NULL OR last_name = '')"
    ).fetchone()[0]
    if nameless:
        issues.append(f"{nameless} PI-eligible faculty missing a first or last name.")

    if issues:
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
    else:
        print("  None.")

    # Enrichment log analysis (from the enrichment_log table).
    print(f"\n{SEP}")
    print("ENRICHMENT LOG (by method / source)")
    print(SEP)
    methods = conn.execute(
        "SELECT method, COUNT(*) AS n FROM enrichment_log GROUP BY method"
        " ORDER BY n DESC"
    ).fetchall()
    if methods:
        for r in methods:
            print(f"  {r['method'] or 'unknown':20s} {r['n']}")
    else:
        print("  No enrichment log entries yet.")

    # Completion evaluation.
    print(f"\n{SEP}")
    pct = overall["coverage_enriched"]
    if pct >= 80:
        print(f"  STATUS: TARGET MET — {pct}% of PI-eligible faculty enriched (>= 80%)")
    elif pct >= 60:
        print(f"  STATUS: APPROACHING — {pct}% enriched (target 80%)")
    else:
        print(f"  STATUS: BELOW TARGET — {pct}% enriched (target 80%)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
