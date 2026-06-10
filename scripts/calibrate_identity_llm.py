"""Backtest the LLM identity adjudicator against already-decided cases.

Read-only: replays the exact production path (dossier fetch + two-pass
adjudication + verdict validation) over identity_candidates groups whose
outcome a human or rule already decided, then reports accept precision,
coverage, and the self-consistency disagreement rate per confidence
threshold. Run this BEFORE enabling live accepts; the chosen threshold goes
into IDENTITY_LLM_ACCEPT_CONFIDENCE and the scheduled sweep stays behind
ENABLE_IDENTITY_LLM_SWEEP until accept precision clears ~0.99.

Ground truth:
  positives — groups with exactly one accepted row that were genuinely
              ambiguous at decision time (>=2 candidates or top score <0.9);
              stratified into manual vs rule accepts via enrichment_log.
  negatives — faculty marked 'rejected' (admin said "not findable") whose
              candidates were all rejected; any accept here is a false
              positive.

Usage:
  python scripts/calibrate_identity_llm.py [--limit N] [--dept SLUG]
      [--thresholds 0.7,0.8,0.9,0.95] [--no-self-consistency]
"""

import argparse
import json
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import db
from enrichment import identity_llm
from enrichment.sources.openalex import OpenAlexSource


def _load_groups(conn, dept):
    """All decided identity_candidates rows joined with faculty, grouped."""
    sql = (
        "SELECT c.*, f.first_name AS f_first, f.last_name AS f_last,"
        " f.title AS f_title, f.department AS f_department,"
        " f.division_school AS f_division_school, f.email AS f_email,"
        " f.research_interests AS f_research_interests,"
        " f.identity_status AS f_identity_status,"
        " f.stable_key AS f_stable_key"
        " FROM identity_candidates c JOIN faculty f ON f.id = c.faculty_id"
        " WHERE c.status IN ('accepted', 'rejected')")
    params = []
    if dept:
        sql += " AND f.department = ?"
        params.append(dept)
    sql += " ORDER BY c.faculty_id, c.score DESC"
    groups = defaultdict(list)
    for row in conn.execute(sql, params):
        groups[row["faculty_id"]].append(dict(row))
    return groups


def _rule_accepted_faculty(conn):
    rows = conn.execute(
        "SELECT DISTINCT faculty_id FROM enrichment_log"
        " WHERE field_updated = 'identity'"
        " AND method IN ('identity_auto', 'identity_auto_rule')")
    return {r[0] for r in rows}


def _build_cases(groups, rule_accepted):
    """Split decided groups into positive / negative calibration cases."""
    positives, negatives = [], []
    for faculty_id, rows in groups.items():
        accepted = [r for r in rows if r["status"] == "accepted"]
        statuses = {r["f_identity_status"] for r in rows}
        ambiguous_at_decision = (len(rows) >= 2
                                 or max(r["score"] or 0 for r in rows) < 0.9)
        if len(accepted) == 1 and ambiguous_at_decision:
            positives.append({
                "faculty_id": faculty_id,
                "rows": rows,
                "truth": accepted[0]["external_id"],
                "stratum": ("rule" if faculty_id in rule_accepted
                            else "manual"),
            })
        elif not accepted and "rejected" in statuses:
            negatives.append({"faculty_id": faculty_id, "rows": rows,
                              "truth": None, "stratum": "negative"})
    return positives, negatives


def _to_candidates(rows):
    candidates = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence"] or "{}")
        except ValueError:
            evidence = {}
        candidates.append({
            "_row_id": row["id"],
            "source": row["source"],
            "external_id": row["external_id"],
            "display_name": row["display_name"],
            "score": row["score"],
            "evidence": evidence,
        })
    candidates.sort(key=lambda c: -(c["score"] or 0))
    return candidates


def _run_case(case, openalex, self_consistency):
    rows = case["rows"]
    faculty = {
        "_db_id": case["faculty_id"],
        "_stable_key": rows[0]["f_stable_key"],
        "first_name": rows[0]["f_first"],
        "last_name": rows[0]["f_last"],
        "title": rows[0].get("f_title"),
        "department": rows[0].get("f_department"),
        "division_school": rows[0].get("f_division_school"),
        "email": rows[0].get("f_email"),
        "research_interests": rows[0].get("f_research_interests"),
    }
    candidates = _to_candidates(rows)
    eligible = identity_llm.eligible_candidates(candidates)
    if not eligible:
        return None
    presented, _ = identity_llm._collapse_for_prompt(eligible)
    stats = {"dossier_fetches": 0}
    dossiers = identity_llm.build_dossiers(presented, openalex, stats,
                                           max_fetches=None)
    first = identity_llm.adjudicate(faculty, presented, dossiers,
                                    identity_llm._default_llm_call)
    second = None
    if self_consistency and first["decision"] == "accept":
        second = identity_llm.adjudicate(
            faculty, presented, dossiers, identity_llm._default_llm_call,
            shuffle_seed=case["faculty_id"])
    verdict = identity_llm.merge_verdicts(first, second)
    # Threshold is swept in the report, so guard everything except confidence.
    ok, reason = identity_llm.acceptance_guardrails(verdict, presented,
                                                    accept_confidence=0.0)
    return {
        "faculty_id": case["faculty_id"],
        "stratum": case["stratum"],
        "department": rows[0].get("f_department"),
        "truth": case["truth"],
        "verdict": verdict,
        "guardrails_ok": ok,
        "guardrail_reason": reason,
        "disagreed": (second is not None
                      and verdict.get("guardrail") == "self_consistency_disagreement"),
    }


def _report(results, thresholds):
    eligible = [r for r in results if r is not None]
    print(f"\n=== Calibration over {len(eligible)} adjudicated groups ===")
    by_stratum = defaultdict(int)
    for r in eligible:
        by_stratum[r["stratum"]] += 1
    print("Strata:", dict(by_stratum))
    accepts_attempted = [r for r in eligible
                         if r["verdict"]["decision"] == "accept"]
    disagreements = sum(1 for r in eligible if r["disagreed"])
    print(f"Self-consistency disagreements: {disagreements}")

    for threshold in thresholds:
        accepts = [r for r in accepts_attempted
                   if r["guardrails_ok"]
                   and r["verdict"]["confidence"] >= threshold]
        correct = [r for r in accepts
                   if r["truth"]
                   and r["verdict"]["candidate_external_id"] == r["truth"]]
        false_on_negatives = [r for r in accepts if r["stratum"] == "negative"]
        precision = len(correct) / len(accepts) if accepts else float("nan")
        positives = [r for r in eligible if r["truth"]]
        coverage = (len(correct) / len(positives)) if positives else float("nan")
        print(f"\n--- threshold >= {threshold} ---")
        print(f"accepts: {len(accepts)}  correct: {len(correct)}  "
              f"precision: {precision:.3f}  coverage of positives: "
              f"{coverage:.3f}")
        print(f"false accepts on negative set: {len(false_on_negatives)}")
        wrong = [r for r in accepts if r not in correct]
        if wrong:
            print("WRONG ACCEPTS (inspect every one):")
            for r in wrong:
                print(f"  faculty {r['faculty_id']} ({r['department']}): "
                      f"picked {r['verdict']['candidate_external_id']}, "
                      f"truth {r['truth']} — {r['verdict']['reasoning'][:160]}")
        per_division = defaultdict(lambda: [0, 0])
        for r in accepts:
            per_division[r["department"]][0] += 1
            if r in correct:
                per_division[r["department"]][1] += 1
        if per_division:
            print("per-division accepts (total/correct):",
                  {d: tuple(v) for d, v in sorted(per_division.items())})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None,
                        help="max calibration cases (sampled)")
    parser.add_argument("--dept", default=None, help="division slug filter")
    parser.add_argument("--thresholds", default="0.7,0.8,0.9,0.95")
    parser.add_argument("--no-self-consistency", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    thresholds = [float(t) for t in args.thresholds.split(",") if t.strip()]
    conn = db.connect(readonly=True)
    groups = _load_groups(conn, args.dept)
    rule_accepted = _rule_accepted_faculty(conn)
    positives, negatives = _build_cases(groups, rule_accepted)
    print(f"Ground truth: {len(positives)} positive groups "
          f"({sum(1 for p in positives if p['stratum'] == 'manual')} manual), "
          f"{len(negatives)} negative groups")

    cases = positives + negatives
    if args.limit and len(cases) > args.limit:
        random.Random(args.seed).shuffle(cases)
        cases = cases[:args.limit]

    openalex = OpenAlexSource()
    results = []
    for i, case in enumerate(cases, 1):
        try:
            results.append(_run_case(case, openalex,
                                     not args.no_self_consistency))
        except Exception as e:
            print(f"  case {case['faculty_id']} errored: {e}")
            results.append(None)
        if i % 10 == 0:
            print(f"  ...{i}/{len(cases)}")

    _report(results, thresholds)


if __name__ == "__main__":
    main()
