"""LLM adjudication of the pending identity review queue.

Third resolution tier, after deterministic scoring (enrichment/identity.py)
and the conservative rules (enrichment/identity_rules.py): faculty whose
identity_candidates the rules left pending are judged by an LLM that sees
richer evidence than the score formula ever did — the candidates' recent
publication titles, affiliation history with years, and name alternatives,
compared against the faculty's HR title, division, and research interests.

Precision-first design (a wrong match poisons downstream enrichment):
  - The LLM may only ACCEPT or ABSTAIN; reject_all verdicts merely annotate
    and downrank the manual queue. Nothing is ever auto-rejected.
  - Accepts require two independent calls agreeing on the same candidate
    (the second sees the candidates in shuffled order — order invariance
    catches position bias at temperature 0), pass deterministic guardrails
    (UCSD affiliation, name-similarity floor, confidence threshold), and go
    through db.decide_identity_candidate — the same path as a manual accept.
  - Everything is logged to enrichment_log with method 'identity_llm_rule'
    and the full verdicts in raw_response.

Pure decision logic (prompt building, verdict validation, guardrails) is
network-free; the LLM caller and OpenAlex source are injected so tests run
without I/O, mirroring how resweep_pending() injects orcid_source.
"""

import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from data import db

from . import identity_rules
from .identity import TIE_MARGIN, _identity_log_entry
from .sources.openalex import OpenAlexSource, AUTHORS_URL, WORKS_URL

logger = logging.getLogger(__name__)

PROMPT_VERSION = "identity-llm-v1"
# Floor on the stored evidence's name similarity for any LLM accept — the
# LLM judges identity, not spelling; a weak name match needs a human.
NAME_SIMILARITY_FLOOR = 0.75
# Don't re-bill the LLM for groups it already abstained on until this many
# days pass (new evidence accrues slowly; the weekly resolve adds candidates).
RECHECK_DAYS = 30
# Dossiers are fetched for the top N candidates by deterministic score;
# trailing low scorers are shown with stored evidence only.
DOSSIER_CANDIDATES = 3
WORKS_PER_CANDIDATE = 10

SYSTEM_PROMPT = """\
You are an author-disambiguation adjudicator for UC San Diego faculty
records. You will receive one faculty member (HR data) and a list of
candidate OpenAlex author profiles. Decide whether exactly one candidate is
certainly the same person as the faculty member.

This decision feeds an automated pipeline: a WRONG match attributes another
person's publications and grants to this faculty member. A missed match
merely waits for human review. Therefore: when in doubt, abstain.

Watch out for:
- common-name homonyms at the same institution (two researchers sharing a
  name at UCSD is common — topics and titles must line up, not just names);
- merged/contaminated profiles whose alternative names belong to someone else;
- candidates whose UCSD affiliation is historical only;
- research topics inconsistent with the faculty member's department and title.

Respond with ONLY a JSON object:
{"decision": "accept" | "reject_all" | "abstain",
 "candidate_external_id": "<id from the candidate list, or null>",
 "confidence": <0.0-1.0>,
 "reasoning": "<2-4 sentences citing the specific evidence>"}

Use "accept" only when the evidence is decisive that one candidate is this
person. Use "reject_all" only when every candidate is clearly someone else.
You may only pick external ids that appear in the candidate list."""


def _accept_confidence():
    return float(os.environ.get("IDENTITY_LLM_ACCEPT_CONFIDENCE", "0.9"))


def _self_consistency():
    return (os.environ.get("IDENTITY_LLM_SELF_CONSISTENCY", "true").lower()
            not in ("false", "0", "no"))


# ---------------------------------------------------------------------------
# Pure layer (no network, no LLM) — unit-testable like identity_rules.py
# ---------------------------------------------------------------------------

def eligible_candidates(candidates):
    """Only openalex-source rows are adjudicated: the LLM's edge is reading
    works/affiliations, which orcid-fallback rows don't expose. Those stay
    in the human queue."""
    return [c for c in candidates if c.get("source") == "openalex"]


def _format_faculty_block(faculty):
    lines = ["FACULTY RECORD (UCSD HR):"]
    lines.append(f"Name: {faculty.get('first_name', '')} "
                 f"{faculty.get('last_name', '')}".strip())
    if faculty.get("title"):
        lines.append(f"Title: {faculty['title']}")
    division = faculty.get("division_school") or faculty.get("department")
    if division:
        lines.append(f"Division/school: {division}")
    email = faculty.get("email") or ""
    if "@" in email:
        lines.append(f"Email domain: {email.split('@', 1)[1]}")
    interests = (faculty.get("research_interests") or "").strip()
    if interests:
        lines.append(f"Research interests: {interests[:400]}")
    return "\n".join(lines)


def _format_candidate_block(idx, candidate, dossier):
    evidence = candidate.get("evidence") or {}
    lines = [f"CANDIDATE {idx}:"]
    lines.append(f"OpenAlex ID: {candidate['external_id']}")
    name = candidate.get("display_name") or evidence.get("display_name") or "?"
    alternatives = (dossier or {}).get("alternatives") or []
    if alternatives:
        name += " (also published as: " + "; ".join(alternatives[:6]) + ")"
    lines.append(f"Display name: {name}")
    if candidate.get("cluster_size", 1) > 1:
        lines.append(f"Note: collapses {candidate['cluster_size']} duplicate "
                     "OpenAlex profiles of the same person.")
    parts = [f"score={candidate.get('score')}"]
    if evidence.get("name_similarity") is not None:
        parts.append(f"name_similarity={evidence['name_similarity']}")
    parts.append(f"ucsd_current={bool(evidence.get('ucsd_current'))}")
    parts.append(f"ucsd_listed={bool(evidence.get('ucsd_listed'))}")
    lines.append("Deterministic match evidence: " + ", ".join(parts))
    works_count = (dossier or {}).get("works_count",
                                      evidence.get("works_count"))
    cited = (dossier or {}).get("cited_by_count",
                                evidence.get("cited_by_count"))
    if works_count is not None or cited is not None:
        lines.append(f"Output: {works_count or 0} works, "
                     f"{cited or 0} citations")
    topics = (dossier or {}).get("topics") or evidence.get("topic_domains") or []
    if topics:
        lines.append("Topics: " + "; ".join(topics[:10]))
    affiliations = (dossier or {}).get("affiliations") or []
    if affiliations:
        lines.append("Affiliation history: " + "; ".join(affiliations[:6]))
    lines.append("Has ORCID on profile: "
                 + ("yes" if evidence.get("orcid") else "no"))
    works = (dossier or {}).get("recent_works") or []
    if works:
        lines.append("Recent works:")
        for w in works[:WORKS_PER_CANDIDATE]:
            entry = f"- {w.get('title', '?')}"
            if w.get("year"):
                entry += f" ({w['year']}"
                entry += f", {w['journal']})" if w.get("journal") else ")"
            lines.append(entry)
    return "\n".join(lines)


def build_user_prompt(faculty, candidates, dossiers, shuffle_seed=None):
    """Render the adjudication prompt. shuffle_seed reorders the candidates
    deterministically for the second self-consistency pass."""
    ordered = list(candidates)
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(ordered)
    blocks = [_format_faculty_block(faculty)]
    for i, cand in enumerate(ordered, 1):
        blocks.append(_format_candidate_block(
            i, cand, dossiers.get(cand["external_id"])))
    blocks.append("Is exactly one of these candidates certainly this faculty "
                  "member? Respond with the JSON verdict only.")
    return "\n\n".join(blocks)


def validate_verdict(raw, candidates):
    """Normalize a parsed LLM response. Anything malformed, out of range, or
    naming an unlisted external id collapses to an abstain — fail closed."""
    invalid = {"decision": "abstain", "candidate_external_id": None,
               "confidence": 0.0, "reasoning": "", "guardrail": "invalid_verdict"}
    if not isinstance(raw, dict):
        return invalid
    decision = raw.get("decision")
    if decision not in ("accept", "reject_all", "abstain"):
        return invalid
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return invalid
    if not 0.0 <= confidence <= 1.0:
        return invalid
    verdict = {
        "decision": decision,
        "candidate_external_id": raw.get("candidate_external_id"),
        "confidence": confidence,
        "reasoning": str(raw.get("reasoning") or "")[:2000],
    }
    if decision == "accept":
        listed = {c["external_id"] for c in candidates}
        if verdict["candidate_external_id"] not in listed:
            return invalid
    else:
        verdict["candidate_external_id"] = None
    return verdict


def merge_verdicts(first, second):
    """Self-consistency: both passes must accept the same candidate; the
    merged confidence is the weaker of the two. Any disagreement abstains."""
    if second is None:
        return first
    if (first["decision"] == "accept" and second["decision"] == "accept"
            and first["candidate_external_id"] == second["candidate_external_id"]):
        return dict(first, confidence=min(first["confidence"],
                                          second["confidence"]))
    return {"decision": "abstain",
            "candidate_external_id": None,
            "confidence": 0.0,
            "reasoning": (f"Self-consistency disagreement: pass 1 said "
                          f"{first['decision']}"
                          f" ({first['candidate_external_id']}), pass 2 said "
                          f"{second['decision']}"
                          f" ({second['candidate_external_id']})."),
            "guardrail": "self_consistency_disagreement"}


def acceptance_guardrails(verdict, candidates, accept_confidence=None):
    """Deterministic post-checks on an accept verdict. Returns (ok, reason);
    every failure leaves the group in the manual queue."""
    if accept_confidence is None:
        accept_confidence = _accept_confidence()
    if verdict["decision"] != "accept":
        return False, "not_an_accept"
    chosen = next((c for c in candidates
                   if c["external_id"] == verdict["candidate_external_id"]),
                  None)
    if chosen is None:
        return False, "candidate_not_listed"
    evidence = chosen.get("evidence") or {}
    if not (evidence.get("ucsd_current") or evidence.get("ucsd_listed")):
        return False, "no_ucsd_affiliation"
    if (evidence.get("name_similarity") or 0.0) < NAME_SIMILARITY_FLOOR:
        return False, "name_similarity_below_floor"
    if verdict["confidence"] < accept_confidence:
        return False, "confidence_below_threshold"
    return True, "ok"


def _recently_evaluated(group, now=None):
    """True when every pending row in the group carries an llm_evaluated_at
    newer than RECHECK_DAYS — the LLM already abstained; don't re-bill."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RECHECK_DAYS)
    for row in group:
        stamp = row.get("llm_evaluated_at")
        if not stamp:
            return False
        try:
            seen = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen < cutoff:
            return False
    return True


def _collapse_for_prompt(candidates):
    """Pre-collapse duplicate-profile ties (identity_rules) so the LLM sees
    one candidate per person instead of re-discovering OpenAlex splits.
    Returns (presented_candidates, cluster_ids) where the canonical carries
    cluster_size and cluster siblings are dropped from the prompt."""
    collapse = identity_rules.collapse_duplicate_ties(candidates, TIE_MARGIN)
    if not collapse["collapsed"]:
        return list(candidates), []
    cluster_ids = set(collapse["cluster_ids"])
    canonical = dict(collapse["canonical"], cluster_size=len(cluster_ids))
    presented = [canonical] + [c for c in candidates
                               if c["external_id"] not in cluster_ids]
    return presented, collapse["cluster_ids"]


# ---------------------------------------------------------------------------
# I/O layer
# ---------------------------------------------------------------------------

def _default_llm_call(system_prompt, user_prompt):
    from utils.grant_matcher import _call_llm
    return _call_llm(system_prompt, user_prompt, max_tokens=500,
                     temperature=0, json_mode=True)


def _model_name():
    try:
        from utils.grant_matcher import _get_model
        return _get_model()
    except Exception:
        return os.environ.get("LITELLM_MODEL", "unknown")


def _fetch_author_detail(openalex, external_id):
    resp = openalex._get(f"{AUTHORS_URL}/{external_id}",
                         params=openalex._params())
    if not resp:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _fetch_recent_works(openalex, external_id):
    resp = openalex._get(WORKS_URL, params=openalex._params({
        "filter": f"authorships.author.id:{external_id}",
        "sort": "publication_date:desc",
        "per-page": WORKS_PER_CANDIDATE,
    }))
    if not resp:
        return None
    try:
        results = resp.json().get("results") or []
    except ValueError:
        return None
    works = []
    for w in results:
        if not w.get("display_name"):
            continue
        entry = {"title": w["display_name"]}
        if w.get("publication_year"):
            entry["year"] = w["publication_year"]
        source = ((w.get("primary_location") or {}).get("source") or {})
        if source.get("display_name"):
            entry["journal"] = source["display_name"]
        works.append(entry)
    return works or None


def _dossier_from_author(author, works):
    dossier = {}
    alternatives = author.get("display_name_alternatives") or []
    if alternatives:
        dossier["alternatives"] = alternatives[:8]
    affiliations = []
    for aff in (author.get("affiliations") or [])[:8]:
        inst = (aff.get("institution") or {}).get("display_name")
        if not inst:
            continue
        years = [y for y in (aff.get("years") or []) if y]
        if years:
            inst += f" ({min(years)}-{max(years)})"
        affiliations.append(inst)
    if affiliations:
        dossier["affiliations"] = affiliations
    topics = []
    for topic in (author.get("topics") or [])[:10]:
        name = topic.get("display_name")
        if name and name not in topics:
            topics.append(name)
    if topics:
        dossier["topics"] = topics
    if author.get("works_count") is not None:
        dossier["works_count"] = author["works_count"]
    if author.get("cited_by_count") is not None:
        dossier["cited_by_count"] = author["cited_by_count"]
    if works:
        dossier["recent_works"] = works
    return dossier


def build_dossiers(candidates, openalex, stats, max_fetches):
    """Fetch fresh OpenAlex evidence for the top candidates. Failures (and
    budget exhaustion) degrade gracefully to the stored evidence."""
    dossiers = {}
    for cand in candidates[:DOSSIER_CANDIDATES]:
        if max_fetches is not None and stats["dossier_fetches"] + 2 > max_fetches:
            break
        try:
            stats["dossier_fetches"] += 2
            author = _fetch_author_detail(openalex, cand["external_id"])
            works = _fetch_recent_works(openalex, cand["external_id"])
        except Exception:
            logger.exception("Dossier fetch failed for %s", cand["external_id"])
            continue
        if author or works:
            dossiers[cand["external_id"]] = _dossier_from_author(author or {},
                                                                 works)
    return dossiers


def adjudicate(faculty, candidates, dossiers, llm_call, shuffle_seed=None):
    """One LLM pass; returns a validated verdict (abstain on parse failure)."""
    from utils.grant_matcher import _parse_json_response

    prompt = build_user_prompt(faculty, candidates, dossiers,
                               shuffle_seed=shuffle_seed)
    try:
        raw = _parse_json_response(llm_call(SYSTEM_PROMPT, prompt))
    except Exception:
        logger.exception("LLM adjudication failed for faculty id %s",
                         faculty.get("_db_id"))
        raw = None
    return validate_verdict(raw, candidates)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def llm_sweep_pending(department=None, max_llm_calls=None,
                      max_dossier_fetches=None, progress_callback=None,
                      time_budget_seconds=None, dry_run=False, force=False,
                      openalex_source=None, llm_call=None):
    """Adjudicate the pending identity review queue with an LLM.

    Accepts go through db.decide_identity_candidate — the same path as a
    manual accept — and are logged with method 'identity_llm_rule'. Abstain
    and reject_all verdicts only annotate the rows (llm_* columns) so the
    admin queue can be triaged; nothing is ever auto-rejected. Returns a
    stats dict.
    """
    import time as _time

    if max_llm_calls is None:
        max_llm_calls = int(os.environ.get("IDENTITY_LLM_MAX_CALLS", "2400"))
    if max_dossier_fetches is None:
        max_dossier_fetches = int(os.environ.get("IDENTITY_LLM_MAX_FETCHES",
                                                 "6000"))
    openalex_source = openalex_source or OpenAlexSource()
    llm_call = llm_call or _default_llm_call
    self_consistency = _self_consistency()
    accept_confidence = _accept_confidence()
    model = _model_name()

    conn = db.get_write_conn()
    rows = db.list_identity_candidates(conn, department=department, limit=None)
    groups = {}
    for row in rows:
        groups.setdefault(row["faculty_id"], []).append(row)
    logger.info("Identity LLM sweep: %d faculty with pending candidates "
                "(dept=%s, dry_run=%s)", len(groups), department or "all",
                dry_run)

    stats = {"faculty_seen": len(groups), "eligible": 0, "skipped_recent": 0,
             "llm_calls": 0, "dossier_fetches": 0, "accepted_llm": 0,
             "abstained": 0, "reject_all_flagged": 0, "guardrail_blocked": 0,
             "errors": 0, "left_pending": 0}
    start = _time.monotonic()

    for i, (faculty_id, group) in enumerate(groups.items()):
        if time_budget_seconds is not None:
            if _time.monotonic() - start > time_budget_seconds - 30:
                logger.warning("Identity LLM sweep: time budget reached "
                               "after %d/%d", i, len(groups))
                break
        if stats["llm_calls"] >= max_llm_calls:
            logger.warning("Identity LLM sweep: call budget reached "
                           "after %d/%d", i, len(groups))
            break

        if not force and _recently_evaluated(group):
            stats["skipped_recent"] += 1
            stats["left_pending"] += 1
            continue

        faculty = {
            "_db_id": faculty_id,
            "_stable_key": group[0].get("f_stable_key"),
            "first_name": group[0]["f_first"],
            "last_name": group[0]["f_last"],
            "title": group[0].get("f_title"),
            "department": group[0].get("f_department"),
            "division_school": group[0].get("f_division_school"),
            "email": group[0].get("f_email"),
            "research_interests": group[0].get("f_research_interests"),
        }
        candidates = []
        for row in group:
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

        eligible = eligible_candidates(candidates)
        if not eligible:
            stats["left_pending"] += 1
            continue
        stats["eligible"] += 1

        try:
            accepted = _adjudicate_group(
                conn, faculty, candidates, eligible, openalex_source,
                llm_call, stats, max_dossier_fetches, self_consistency,
                accept_confidence, model, dry_run)
        except Exception:
            logger.exception("Identity LLM sweep failed for faculty id %s",
                             faculty_id)
            stats["errors"] += 1
            stats["left_pending"] += 1
            continue

        if not accepted:
            stats["left_pending"] += 1
        if progress_callback:
            progress_callback(i + 1, len(groups))

    conn.commit()
    logger.info("Identity LLM sweep done: %s", stats)
    return stats


def _adjudicate_group(conn, faculty, candidates, eligible, openalex_source,
                      llm_call, stats, max_dossier_fetches, self_consistency,
                      accept_confidence, model, dry_run):
    """Run the two-pass adjudication for one faculty group. Returns True
    when a candidate was accepted (and committed)."""
    faculty_id = faculty["_db_id"]
    presented, cluster_ids = _collapse_for_prompt(eligible)
    dossiers = build_dossiers(presented, openalex_source, stats,
                              max_dossier_fetches)

    stats["llm_calls"] += 1
    first = adjudicate(faculty, presented, dossiers, llm_call)
    second = None
    if first["decision"] == "accept" and self_consistency:
        stats["llm_calls"] += 1
        second = adjudicate(faculty, presented, dossiers, llm_call,
                            shuffle_seed=faculty_id)
    verdict = merge_verdicts(first, second)

    ok = blocked = False
    if verdict["decision"] == "accept":
        ok, reason = acceptance_guardrails(verdict, presented,
                                           accept_confidence)
        if not ok:
            stats["guardrail_blocked"] += 1
            blocked = True
            verdict = dict(verdict, decision="abstain", guardrail=reason)

    if ok and not dry_run:
        chosen = next(c for c in candidates
                      if c["external_id"] == verdict["candidate_external_id"])
        decided = db.decide_identity_candidate(conn, chosen["_row_id"],
                                               accept=True)
        if decided:
            sibling_orcid = None
            if not (chosen.get("evidence") or {}).get("orcid"):
                for c in candidates:
                    if (c["external_id"] in cluster_ids
                            and (c.get("evidence") or {}).get("orcid")):
                        sibling_orcid = c["evidence"]["orcid"]
                        break
            if sibling_orcid:
                conn.execute(
                    "UPDATE faculty SET orcid = COALESCE(orcid, ?)"
                    " WHERE id = ?", (sibling_orcid, faculty_id))
            rule_info = {"rule": "llm_adjudication",
                         "prompt_version": PROMPT_VERSION,
                         "model": model,
                         "verdict_1": first,
                         "verdict_2": second,
                         "dossier_ids": sorted(dossiers),
                         "cluster": cluster_ids or None}
            db.append_log(conn, [_identity_log_entry(
                faculty_id, faculty["_stable_key"], chosen,
                method="identity_llm_rule",
                confidence=verdict["confidence"], rule_info=rule_info)])
            stats["accepted_llm"] += 1
            conn.commit()
            return True
        return False

    # Abstain / reject_all / dry-run accept: annotate only.
    if ok and dry_run:
        stats["accepted_llm"] += 1   # would have accepted
    elif verdict["decision"] == "reject_all":
        stats["reject_all_flagged"] += 1
    elif not blocked:
        stats["abstained"] += 1   # guardrail-blocked accepts counted above
    annotations = {}
    for c in candidates:
        if verdict["decision"] == "accept":   # dry-run accept
            row_verdict = ("accept" if c["external_id"]
                           == verdict["candidate_external_id"] else "abstain")
        elif verdict["decision"] == "reject_all":
            row_verdict = "reject"
        else:
            row_verdict = "abstain"
        annotations[c["_row_id"]] = (row_verdict, verdict["confidence"],
                                     verdict["reasoning"])
    db.annotate_identity_candidates_llm(conn, annotations)
    conn.commit()
    return False
