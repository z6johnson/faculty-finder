#!/usr/bin/env python3
"""Triage the identity-resolution backlog: why is each faculty still pending?

Read-only. Buckets every 'ambiguous' (pending identity_candidates group) and
'not_found' faculty by the reason it hasn't resolved, so each fix can be
sized and a resolution campaign's progress measured between passes:

  ambiguous groups
    accept_annotated      stored LLM accept awaiting promotion (next
                          non-dry sweep commits it without new LLM calls)
    cooldown_same_model   fresh llm_evaluated_at from the *configured*
                          model — the sweep will skip these (re-billed as
                          skipped_recent) until the stamp ages out
    cooldown_other_model  fresh stamp from a different/unknown model — the
                          model-aware recheck re-opens these on the next
                          sweep with the configured model
    self_consistency      abstained because the two passes disagreed
    abstained             evidence-based abstain (or stale stamp)
    never_evaluated       no LLM verdict stored yet
  guardrail ceilings (within ambiguous, independent overlay)
    below_name_floor      no candidate clears the LLM accept name floor —
                          only a human can accept these
    no_ucsd_evidence      no candidate carries UCSD affiliation evidence
    no_topical_context    faculty row has no research interests (original
                          or enriched) — the adjudicator has nothing to
                          corroborate topics against; run the ucsd_profile
                          context backfill first
  not_found
    likely_terminal       no research-bearing role (identity_rules.
                          no_research_footprint) — the resweep's terminal
                          disposition will retire these
    needs_recall          active researchers the searches simply missed —
                          re-run identity_resolve (include_not_found) to
                          retry with the name-variant/no-filter fallbacks

Usage:
  python scripts/identity_triage.py [--dept SLUG] [--csv OUT.csv]
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from enrichment import identity_llm, identity_rules

SELF_CONSISTENCY_PREFIX = "Self-consistency disagreement"


def _group_bucket(group, model):
    """Primary bucket for one pending candidate group."""
    if any(row.get("llm_verdict") == "accept"
           and identity_llm._stamp_is_fresh(row.get("llm_evaluated_at"))
           for row in group):
        return "accept_annotated"
    if all(identity_llm._stamp_is_fresh(row.get("llm_evaluated_at"))
           for row in group):
        if all(row.get("llm_model") == model for row in group):
            return "cooldown_same_model"
        return "cooldown_other_model"
    if any((row.get("llm_reasoning") or "").startswith(SELF_CONSISTENCY_PREFIX)
           for row in group):
        return "self_consistency"
    if any(row.get("llm_evaluated_at") for row in group):
        return "abstained"
    return "never_evaluated"


def _evidence(row):
    try:
        return json.loads(row.get("evidence") or "{}")
    except ValueError:
        return {}


def _group_ceilings(group):
    """Deterministic accept ceilings: reasons no sweep can ever auto-accept
    this group regardless of what the LLM says."""
    ceilings = []
    openalex = [r for r in group if r.get("source") == "openalex"]
    orcid = [r for r in group if r.get("source") == "orcid"]
    if openalex:
        sims = [(_evidence(r).get("name_similarity") or 0.0) for r in openalex]
        if max(sims) < identity_llm.NAME_SIMILARITY_FLOOR:
            ceilings.append("below_name_floor")
        if not any(_evidence(r).get("ucsd_current")
                   or _evidence(r).get("ucsd_listed") for r in openalex):
            ceilings.append("no_ucsd_evidence")
    elif orcid:
        ev = _evidence(orcid[0])
        if (ev.get("record_name_similarity") or 0.0) \
                < identity_llm.ORCID_NAME_SIMILARITY_FLOOR:
            ceilings.append("below_name_floor")
        if not ev.get("employment_verified"):
            ceilings.append("employment_not_verified")
    first = group[0]
    if not (first.get("f_research_interests")
            or first.get("f_research_interests_enriched")):
        ceilings.append("no_topical_context")
    return ceilings


def triage(conn, department=None):
    model = identity_llm._model_name()
    rows = db.list_identity_candidates(conn, department=department, limit=None)
    groups = defaultdict(list)
    for row in rows:
        groups[row["faculty_id"]].append(row)

    records = []
    for faculty_id, group in groups.items():
        first = group[0]
        bucket = _group_bucket(group, model)
        ceilings = _group_ceilings(group)
        records.append({
            "faculty_id": faculty_id,
            "name": f"{first.get('f_first', '')} {first.get('f_last', '')}".strip(),
            "department": first.get("f_department"),
            "status": "ambiguous",
            "bucket": bucket,
            "ceilings": ";".join(ceilings),
            "candidates": len(group),
            "llm_model": first.get("llm_model") or "",
            "llm_evaluated_at": first.get("llm_evaluated_at") or "",
        })

    not_found = db.fetch_identity_queue(
        conn, department=department, statuses=("not_found", "no_footprint"))
    for faculty in not_found:
        status = faculty.get("identity_status")
        if status == "no_footprint":
            bucket = "no_footprint"
        elif identity_rules.no_research_footprint(faculty):
            bucket = "likely_terminal"
        else:
            bucket = "needs_recall"
        records.append({
            "faculty_id": faculty["_db_id"],
            "name": f"{faculty.get('first_name', '')} "
                    f"{faculty.get('last_name', '')}".strip(),
            "department": faculty.get("department"),
            "status": status,
            "bucket": bucket,
            "ceilings": "",
            "candidates": 0,
            "llm_model": "",
            "llm_evaluated_at": "",
        })
    return records, model


def report(records, model):
    print(f"# Identity triage ({len(records)} unresolved faculty; "
          f"configured model: {model})\n")
    by_bucket = Counter(r["bucket"] for r in records)
    print("## By cause\n")
    for bucket, n in by_bucket.most_common():
        print(f"- {bucket}: {n}")

    ceilings = Counter()
    for r in records:
        for c in filter(None, r["ceilings"].split(";")):
            ceilings[c] += 1
    if ceilings:
        print("\n## Accept ceilings (ambiguous groups; overlapping)\n")
        for ceiling, n in ceilings.most_common():
            print(f"- {ceiling}: {n}")

    print("\n## By division\n")
    by_dept = defaultdict(Counter)
    for r in records:
        by_dept[r["department"] or "?"][r["bucket"]] += 1
    for dept in sorted(by_dept):
        counts = by_dept[dept]
        detail = ", ".join(f"{b}={n}" for b, n in counts.most_common())
        print(f"- {dept}: {sum(counts.values())} ({detail})")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dept", help="Restrict to one division slug")
    parser.add_argument("--csv", help="Write per-faculty rows to this path")
    args = parser.parse_args()

    conn = db.connect(readonly=True)
    records, model = triage(conn, department=args.dept)
    report(records, model)

    if args.csv:
        fields = ["faculty_id", "name", "department", "status", "bucket",
                  "ceilings", "candidates", "llm_model", "llm_evaluated_at"]
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(records)
        print(f"\nWrote {len(records)} rows to {args.csv}")


if __name__ == "__main__":
    main()
