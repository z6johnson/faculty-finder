"""LLM adjudication of the pending identity review queue.

Third resolution tier, after deterministic scoring (enrichment/identity.py)
and the conservative rules (enrichment/identity_rules.py): faculty whose
identity_candidates the rules left pending are judged by an LLM that sees
richer evidence than the score formula ever did — the candidates' recent
publication titles, affiliation history with years, and name alternatives,
compared against the faculty's HR title, division, and research interests.

Precision-first design (a wrong match poisons downstream enrichment):
  - The LLM may only ACCEPT or ABSTAIN; reject_all verdicts merely annotate
    and downrank the manual queue. Whole groups are never auto-rejected;
    only an accepted verdict retires its losing co-candidates (duplicate
    profiles of the accepted person are merged as alternate ids, not lost).
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
from .sources.orcid import ORCIDSource

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
# ORCID-only groups are a single uncorroborated profile (no second source to
# cross-check), so accepts are gated harder than the OpenAlex path: a higher
# confidence floor and a near-exact record-name match on top of a
# network-verified UCSD *employment* fact (acceptance_guardrails_orcid).
ORCID_NAME_SIMILARITY_FLOOR = 0.9

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

Output the raw JSON object only — no markdown fences, no text before or
after it. Keep reasoning to at most 4 sentences.

Use "accept" only when the evidence is decisive that one candidate is this
person. Use "reject_all" only when every candidate is clearly someone else.
You may only pick external ids that appear in the candidate list."""


ORCID_SYSTEM_PROMPT = """\
You are an author-disambiguation adjudicator for UC San Diego faculty
records. You will receive one faculty member (HR data) and ONE ORCID profile
that a name+affiliation search proposed as them. Decide whether this ORCID
profile certainly belongs to the same person as the faculty member.

This decision feeds an automated pipeline: a WRONG match attributes another
person's publications and grants to this faculty member. A missed match
merely waits for human review. There is no second candidate to fall back on,
so the bar is high — when in doubt, abstain.

Confirm only when the profile's employment history, research works, and name
all line up with the faculty member's department, title, and interests.
Watch out for:
- common-name homonyms (the name + "University of California San Diego"
  search can return a different UCSD person who shares the name);
- profiles whose UCSD affiliation is education-only or historical, not the
  current employment of an active faculty member;
- research topics inconsistent with the faculty member's department/title.

Respond with ONLY a JSON object:
{"decision": "accept" | "reject_all" | "abstain",
 "candidate_external_id": "<the ORCID iD, or null>",
 "confidence": <0.0-1.0>,
 "reasoning": "<2-4 sentences citing the specific evidence>"}

Output the raw JSON object only — no markdown fences, no text before or
after it. Use "accept" only when it is decisive that this ORCID profile is
this faculty member; otherwise "abstain" (or "reject_all" if the profile is
clearly a different person)."""


# Paraphrased second pass for single-candidate self-consistency: order-shuffle
# (the OpenAlex trick) is a no-op for one candidate, so we re-ask the question
# from an adversarial angle. A brittle accept that survives only one phrasing
# disagrees here and collapses to abstain via merge_verdicts.
ORCID_SYSTEM_PROMPT_RECHECK = """\
You are verifying a proposed identity match for UC San Diego faculty. You
will receive one faculty member (HR data) and ONE ORCID profile. Your job is
to actively look for reasons this match could be WRONG — a same-name UCSD
homonym, an education-only or outdated affiliation, or research that does not
fit the faculty member's department and title.

Only confirm the match if, after trying to disprove it, the employment,
works, and name still decisively point to the same person. A wrong match
poisons downstream enrichment; when any doubt remains, abstain.

Respond with ONLY a JSON object:
{"decision": "accept" | "reject_all" | "abstain",
 "candidate_external_id": "<the ORCID iD, or null>",
 "confidence": <0.0-1.0>,
 "reasoning": "<2-4 sentences citing the specific evidence>"}

Output the raw JSON object only — no markdown fences, no text around it."""


class AdjudicationError(Exception):
    """Infrastructure failure during one adjudication (LLM call or response
    parsing) — distinct from a genuine abstain so the sweep never stamps
    llm_evaluated_at for it. kind: 'budget' | 'llm_call' | 'parse'."""

    def __init__(self, kind, message=""):
        super().__init__(message or kind)
        self.kind = kind


def _accept_confidence():
    return float(os.environ.get("IDENTITY_LLM_ACCEPT_CONFIDENCE", "0.9"))


def _orcid_accept_confidence():
    return float(os.environ.get("IDENTITY_LLM_ORCID_ACCEPT_CONFIDENCE", "0.95"))


def _identity_model():
    """Scoped model override for the identity sweep, so it can run a cheaper,
    higher-throughput model (e.g. deepseek-v4-flash) than grant matching or
    the normalizer without changing the global LITELLM_MODEL. Returns None
    when unset (the sweep then uses the global default)."""
    model = os.environ.get("IDENTITY_LLM_MODEL")
    if not model:
        return None
    if "/" not in model:
        model = f"openai/{model}"
    return model


def _self_consistency():
    return (os.environ.get("IDENTITY_LLM_SELF_CONSISTENCY", "true").lower()
            not in ("false", "0", "no"))


# ---------------------------------------------------------------------------
# Pure layer (no network, no LLM) — unit-testable like identity_rules.py
# ---------------------------------------------------------------------------

def eligible_candidates(candidates):
    """OpenAlex-source rows, adjudicated with the OpenAlex dossier path."""
    return [c for c in candidates if c.get("source") == "openalex"]


def orcid_eligible_candidates(candidates):
    """ORCID-source rows. Adjudicated by the ORCID path (build_orcid_dossier
    + ORCID_SYSTEM_PROMPT) only when no OpenAlex row is eligible — the
    OpenAlex path has richer evidence, so mixed groups stay OpenAlex-only.
    These are usually a single _orcid_fallback row."""
    return [c for c in candidates if c.get("source") == "orcid"]


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


def _format_orcid_candidate_block(candidate, dossier):
    evidence = candidate.get("evidence") or {}
    d = dossier or {}

    def _fact(key):
        return d.get(key, evidence.get(key))

    lines = ["ORCID PROFILE (proposed match — the only candidate):"]
    lines.append(f"ORCID iD: {candidate['external_id']}")
    name = (d.get("record_name") or candidate.get("display_name")
            or evidence.get("display_name") or "?")
    lines.append(f"Profile name: {name}")
    facts = []
    sim = _fact("record_name_similarity")
    if sim is not None:
        facts.append(f"record_name_similarity={sim}")
    facts.append(f"ucsd_employment_verified={bool(_fact('employment_verified'))}")
    facts.append(f"unique_name_hit={bool(_fact('unique_hit'))}")
    facts.append(f"faculty_email_on_record={bool(_fact('record_email_match'))}")
    lines.append("Verification facts: " + ", ".join(facts))
    affiliations = d.get("affiliations") or []
    if affiliations:
        lines.append("Employment history: " + "; ".join(affiliations[:8]))
    works = d.get("recent_works") or []
    if works:
        lines.append("Recent works:")
        for w in works[:WORKS_PER_CANDIDATE]:
            entry = f"- {w.get('title', '?')}"
            if w.get("year"):
                entry += f" ({w['year']}"
                entry += f", {w['journal']})" if w.get("journal") else ")"
            lines.append(entry)
    fundings = d.get("fundings") or []
    if fundings:
        lines.append("Funding/grants:")
        for g in fundings[:5]:
            entry = f"- {g.get('title', '?')}"
            if g.get("agency"):
                entry += f" — {g['agency']}"
            lines.append(entry)
    return "\n".join(lines)


def build_orcid_user_prompt(faculty, candidate, dossier):
    """Render the single-profile ORCID verification prompt."""
    blocks = [_format_faculty_block(faculty),
              _format_orcid_candidate_block(candidate, dossier),
              "Is this ORCID profile certainly the same person as the faculty "
              "member? Respond with the JSON verdict only — use the ORCID iD as "
              "candidate_external_id when accepting."]
    return "\n\n".join(blocks)


def acceptance_guardrails_orcid(verdict, candidate, dossier=None,
                                accept_confidence=None):
    """Deterministic post-checks on an ORCID accept. Stricter than the
    OpenAlex path: a single uncorroborated profile must carry a
    network-verified UCSD *employment* fact, a near-exact record-name match,
    and clear a higher confidence floor. Reads fresh dossier facts when
    present, else the stored evidence. Returns (ok, reason)."""
    if accept_confidence is None:
        accept_confidence = _orcid_accept_confidence()
    if verdict["decision"] != "accept":
        return False, "not_an_accept"
    if verdict["candidate_external_id"] != candidate["external_id"]:
        return False, "candidate_not_listed"
    d = dossier or {}
    evidence = candidate.get("evidence") or {}

    def _fact(key):
        return d.get(key, evidence.get(key))

    if not _fact("employment_verified"):
        return False, "employment_not_verified"
    if (_fact("record_name_similarity") or 0.0) < ORCID_NAME_SIMILARITY_FLOOR:
        return False, "name_similarity_below_floor"
    if verdict["confidence"] < accept_confidence:
        return False, "confidence_below_threshold"
    return True, "ok"


def _stamp_is_fresh(stamp, now=None):
    """True when an llm_evaluated_at ISO timestamp is newer than RECHECK_DAYS."""
    if not stamp:
        return False
    try:
        seen = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return seen >= now - timedelta(days=RECHECK_DAYS)


def _recently_evaluated(group, now=None):
    """True when every pending row in the group carries an llm_evaluated_at
    newer than RECHECK_DAYS — the LLM already abstained; don't re-bill."""
    return all(_stamp_is_fresh(row.get("llm_evaluated_at"), now)
               for row in group)


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
    return _call_llm(system_prompt, user_prompt, max_tokens=1500,
                     temperature=0, json_mode=True, model=_identity_model())


def _model_name():
    override = _identity_model()
    if override:
        return override
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


def build_orcid_dossier(candidate, faculty, orcid_source, stats, max_fetches):
    """Fetch the ORCID record and distill the evidence the LLM judges:
    recent works, employment history, and fresh network-verified facts
    (employment_verified, record_name_similarity). Degrades to the stored
    evidence on fetch failure or budget exhaustion so the prompt is always
    renderable. Mirrors build_dossiers' graceful degradation."""
    evidence = candidate.get("evidence") or {}
    dossier = {
        "employment_verified": bool(evidence.get("employment_verified")),
        "record_name_similarity": evidence.get("record_name_similarity"),
        "record_email_match": bool(evidence.get("record_email_match")),
        "unique_hit": bool(evidence.get("unique_hit")),
    }
    if max_fetches is not None and stats["orcid_fetches"] + 1 > max_fetches:
        return dossier
    try:
        stats["orcid_fetches"] += 1
        record = orcid_source._fetch_full_record(candidate["external_id"])
    except Exception:
        logger.exception("ORCID dossier fetch failed for %s",
                         candidate["external_id"])
        return dossier
    if not record:
        return dossier
    # Fresh, network-verified facts override the stored evidence the guardrail
    # would otherwise trust.
    try:
        verification = orcid_source._verify_record(
            record, candidate["external_id"],
            faculty.get("first_name", ""), faculty.get("last_name", ""),
            email=faculty.get("email"))
    except Exception:
        logger.exception("ORCID re-verification failed for %s",
                         candidate["external_id"])
        verification = None
    if verification:
        dossier["employment_verified"] = verification["employment_verified"]
        dossier["record_name_similarity"] = \
            verification["record_name_similarity"]
        dossier["record_email_match"] = verification["record_email_match"]
    works = orcid_source._extract_works(record)
    if works:
        dossier["recent_works"] = works[:WORKS_PER_CANDIDATE]
    affiliations = orcid_source.employment_affiliations(record)
    if affiliations:
        dossier["affiliations"] = affiliations
    fundings = orcid_source._extract_fundings(record)
    if fundings:
        dossier["fundings"] = fundings[:5]
    return dossier


def _is_budget_error(exc):
    try:
        import litellm
        if isinstance(exc, (litellm.RateLimitError,
                            litellm.BudgetExceededError)):
            return True
    except (ImportError, AttributeError):
        pass
    return "budget" in str(exc).lower()


def adjudicate(faculty, candidates, dossiers, llm_call, shuffle_seed=None,
               stats=None):
    """One LLM pass; returns a validated verdict.

    Infrastructure failures raise AdjudicationError instead of masquerading
    as abstains: a budget/transport error or an unparseable response says
    nothing about the candidates, so the group must stay un-stamped and be
    retried on a later sweep. An unparseable response is retried once
    (counted in stats['llm_calls']). A parseable-but-malformed verdict is
    still the model's answer and collapses to abstain via validate_verdict.
    """
    from utils.grant_matcher import _parse_json_response

    prompt = build_user_prompt(faculty, candidates, dossiers,
                               shuffle_seed=shuffle_seed)

    def _call():
        try:
            return llm_call(SYSTEM_PROMPT, prompt)
        except Exception as e:
            kind = "budget" if _is_budget_error(e) else "llm_call"
            raise AdjudicationError(
                kind, f"LLM call failed for faculty id "
                      f"{faculty.get('_db_id')}: {e}") from e

    text = _call()
    try:
        raw = _parse_json_response(text)
    except ValueError:
        logger.warning("Unparseable LLM response for faculty id %s — "
                       "retrying once", faculty.get("_db_id"))
        if stats is not None:
            stats["llm_calls"] += 1
        text = _call()
        try:
            raw = _parse_json_response(text)
        except ValueError as e:
            raise AdjudicationError(
                "parse", f"Unparseable LLM response for faculty id "
                         f"{faculty.get('_db_id')} after retry") from e
    return validate_verdict(raw, candidates)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def llm_sweep_pending(department=None, max_llm_calls=None,
                      max_dossier_fetches=None, max_orcid_fetches=None,
                      progress_callback=None, time_budget_seconds=None,
                      dry_run=False, force=False, openalex_source=None,
                      orcid_source=None, llm_call=None):
    """Adjudicate the pending identity review queue with an LLM.

    Accepts go through db.decide_identity_candidate — the same path as a
    manual accept — and are logged with method 'identity_llm_rule'. Abstain
    and reject_all verdicts only annotate the rows (llm_* columns) so the
    admin queue can be triaged; nothing is ever auto-rejected.

    Dry runs annotate (including llm_evaluated_at) but never commit: abstain
    annotations cache the verdict for RECHECK_DAYS so the next sweep doesn't
    re-bill, and accept annotations are *promoted* — committed without new
    LLM calls — by the next non-dry run, after a fresh guardrail re-check.

    Infrastructure failures (LLM budget/transport errors, unparseable
    responses) never stamp llm_evaluated_at; the sweep aborts early on
    budget exhaustion or repeated consecutive errors. Returns a stats dict.
    """
    import time as _time

    if max_llm_calls is None:
        max_llm_calls = int(os.environ.get("IDENTITY_LLM_MAX_CALLS", "2400"))
    if max_dossier_fetches is None:
        max_dossier_fetches = int(os.environ.get("IDENTITY_LLM_MAX_FETCHES",
                                                 "6000"))
    if max_orcid_fetches is None:
        max_orcid_fetches = int(os.environ.get("IDENTITY_LLM_MAX_ORCID_FETCHES",
                                               "3000"))
    openalex_source = openalex_source or OpenAlexSource()
    orcid_source = orcid_source or ORCIDSource()
    llm_call = llm_call or _default_llm_call
    self_consistency = _self_consistency()
    accept_confidence = _accept_confidence()
    orcid_accept_confidence = _orcid_accept_confidence()
    model = _model_name()

    conn = db.get_write_conn()
    rows = db.list_identity_candidates(conn, department=department, limit=None)
    groups = {}
    for row in rows:
        groups.setdefault(row["faculty_id"], []).append(row)
    logger.info("Identity LLM sweep: %d faculty with pending candidates "
                "(dept=%s, dry_run=%s)", len(groups), department or "all",
                dry_run)

    stats = {"faculty_seen": len(groups), "eligible": 0, "orcid_eligible": 0,
             "skipped_recent": 0, "llm_calls": 0, "dossier_fetches": 0,
             "orcid_fetches": 0, "accepted_llm": 0, "orcid_accepted": 0,
             "orcid_annotated": 0, "orcid_guardrail_blocked": 0,
             "promoted": 0, "abstained": 0, "reject_all_flagged": 0,
             "guardrail_blocked": 0, "errors": 0, "budget_errors": 0,
             "left_pending": 0}
    start = _time.monotonic()
    consecutive_errors = 0

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
        # Mixed groups stay OpenAlex-only (richer evidence); the ORCID path
        # runs only when no OpenAlex row is eligible.
        orcid_eligible = (orcid_eligible_candidates(candidates)
                          if not eligible else [])

        # Promote a stored accept annotation (e.g. from a dry run) before
        # spending anything: the verdict was already paid for and vetted.
        if not dry_run and (eligible or orcid_eligible):
            try:
                if _promote_annotated_accept(conn, faculty, candidates,
                                             eligible or orcid_eligible, group,
                                             stats, accept_confidence, model):
                    if progress_callback:
                        progress_callback(i + 1, len(groups))
                    continue
            except Exception:
                logger.exception("Annotation promotion failed for faculty "
                                 "id %s", faculty_id)

        if not force and _recently_evaluated(group):
            stats["skipped_recent"] += 1
            stats["left_pending"] += 1
            continue

        if not eligible and not orcid_eligible:
            stats["left_pending"] += 1
            continue

        try:
            if eligible:
                stats["eligible"] += 1
                accepted = _adjudicate_group(
                    conn, faculty, candidates, eligible, openalex_source,
                    llm_call, stats, max_dossier_fetches, self_consistency,
                    accept_confidence, model, dry_run)
            else:
                stats["orcid_eligible"] += 1
                accepted = _adjudicate_orcid_group(
                    conn, faculty, candidates, orcid_eligible, orcid_source,
                    llm_call, stats, max_orcid_fetches, self_consistency,
                    orcid_accept_confidence, model, dry_run)
            consecutive_errors = 0
        except AdjudicationError as e:
            logger.warning("Identity LLM sweep: %s failure for faculty id "
                           "%s: %s", e.kind, faculty_id, e)
            stats["errors"] += 1
            stats["left_pending"] += 1
            consecutive_errors += 1
            if e.kind == "budget":
                stats["budget_errors"] += 1
                stats["aborted"] = "llm_budget_exhausted"
                logger.warning("Identity LLM sweep: LLM budget exhausted "
                               "after %d/%d — aborting", i, len(groups))
                break
            if consecutive_errors >= 3:
                stats["aborted"] = "consecutive_errors"
                logger.warning("Identity LLM sweep: %d consecutive errors "
                               "after %d/%d — aborting", consecutive_errors,
                               i, len(groups))
                break
            continue
        except Exception:
            logger.exception("Identity LLM sweep failed for faculty id %s",
                             faculty_id)
            stats["errors"] += 1
            stats["left_pending"] += 1
            consecutive_errors += 1
            if consecutive_errors >= 3:
                stats["aborted"] = "consecutive_errors"
                logger.warning("Identity LLM sweep: %d consecutive errors "
                               "after %d/%d — aborting", consecutive_errors,
                               i, len(groups))
                break
            continue

        if not accepted:
            stats["left_pending"] += 1
        if progress_callback:
            progress_callback(i + 1, len(groups))

    conn.commit()
    logger.info("Identity LLM sweep done: %s", stats)
    return stats


def _commit_accept(conn, faculty, candidates, verdict, cluster_ids,
                   rule_info):
    """Commit an accept verdict through the same db path as a manual accept:
    accept the chosen candidate as primary, merge its duplicate-profile
    cluster siblings as alternate ids (adopting their ORCID), and reject the
    remaining candidates — the two-pass verdict was exclusive. Returns True
    when the decision landed."""
    faculty_id = faculty["_db_id"]
    chosen = next(c for c in candidates
                  if c["external_id"] == verdict["candidate_external_id"])
    decided = db.decide_identity_candidate(conn, chosen["_row_id"], "accept")
    if not decided:
        return False
    cluster_ids = set(cluster_ids or [])
    for c in candidates:
        if c["_row_id"] == chosen["_row_id"]:
            continue
        if c["external_id"] in cluster_ids:
            # Duplicate OpenAlex profile of the accepted person: keep its
            # works reachable by enrichment instead of dropping them.
            db.decide_identity_candidate(conn, c["_row_id"], "merge")
        else:
            db.decide_identity_candidate(conn, c["_row_id"], "reject")
    db.append_log(conn, [_identity_log_entry(
        faculty_id, faculty["_stable_key"], chosen,
        method="identity_llm_rule",
        confidence=verdict["confidence"], rule_info=rule_info)])
    conn.commit()
    return True


def _promote_annotated_accept(conn, faculty, candidates, eligible, group,
                              stats, accept_confidence, model):
    """Commit a stored accept annotation (typically left by a dry run)
    without new LLM calls. Accept annotations are only ever written after
    the two-pass verdict passed guardrails; re-run the deterministic
    guardrails against the stored evidence and commit on pass. Returns True
    when promoted."""
    accept_row = next(
        (row for row in group
         if row.get("llm_verdict") == "accept"
         and _stamp_is_fresh(row.get("llm_evaluated_at"))),
        None)
    if accept_row is None:
        return False
    verdict = {
        "decision": "accept",
        "candidate_external_id": accept_row["external_id"],
        "confidence": accept_row.get("llm_confidence") or 0.0,
        "reasoning": accept_row.get("llm_reasoning") or "",
    }
    if accept_row.get("source") == "orcid":
        # ORCID-only group: re-run the stricter ORCID guardrails against the
        # stored evidence (no fresh fetch); no duplicate-profile cluster.
        chosen = next((c for c in candidates
                       if c["external_id"] == accept_row["external_id"]), None)
        if chosen is None:
            return False
        cluster_ids = None
        ok, reason = acceptance_guardrails_orcid(
            verdict, chosen, None, _orcid_accept_confidence())
        rule = "llm_orcid_annotation_promoted"
    else:
        presented, cluster_ids = _collapse_for_prompt(eligible)
        ok, reason = acceptance_guardrails(verdict, presented,
                                           accept_confidence)
        rule = "llm_annotation_promoted"
    if not ok:
        logger.info("Annotation promotion blocked for faculty id %s: %s",
                    faculty["_db_id"], reason)
        return False
    rule_info = {"rule": rule,
                 "prompt_version": PROMPT_VERSION,
                 "model": model,
                 "annotated_at": accept_row.get("llm_evaluated_at"),
                 "cluster": cluster_ids or None}
    if _commit_accept(conn, faculty, candidates, verdict, cluster_ids,
                      rule_info):
        stats["promoted"] += 1
        return True
    return False


def _adjudicate_group(conn, faculty, candidates, eligible, openalex_source,
                      llm_call, stats, max_dossier_fetches, self_consistency,
                      accept_confidence, model, dry_run):
    """Run the two-pass adjudication for one faculty group. Returns True
    when a candidate was accepted (and committed)."""
    presented, cluster_ids = _collapse_for_prompt(eligible)
    dossiers = build_dossiers(presented, openalex_source, stats,
                              max_dossier_fetches)

    stats["llm_calls"] += 1
    first = adjudicate(faculty, presented, dossiers, llm_call, stats=stats)
    second = None
    if first["decision"] == "accept" and self_consistency:
        stats["llm_calls"] += 1
        second = adjudicate(faculty, presented, dossiers, llm_call,
                            shuffle_seed=faculty["_db_id"], stats=stats)
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
        rule_info = {"rule": "llm_adjudication",
                     "prompt_version": PROMPT_VERSION,
                     "model": model,
                     "verdict_1": first,
                     "verdict_2": second,
                     "dossier_ids": sorted(dossiers),
                     "cluster": cluster_ids or None}
        if _commit_accept(conn, faculty, candidates, verdict, cluster_ids,
                          rule_info):
            stats["accepted_llm"] += 1
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


def _adjudicate_orcid_once(faculty, user_prompt, system_prompt, candidate,
                           llm_call, stats):
    """One ORCID adjudication pass; returns a validated verdict. Mirrors
    adjudicate()'s failure handling — AdjudicationError on infra failures,
    one parse retry — but uses a prebuilt single-profile prompt and a
    caller-chosen system prompt (the recheck pass paraphrases it)."""
    from utils.grant_matcher import _parse_json_response

    def _call():
        try:
            return llm_call(system_prompt, user_prompt)
        except Exception as e:
            kind = "budget" if _is_budget_error(e) else "llm_call"
            raise AdjudicationError(
                kind, f"LLM call failed for faculty id "
                      f"{faculty.get('_db_id')}: {e}") from e

    text = _call()
    try:
        raw = _parse_json_response(text)
    except ValueError:
        logger.warning("Unparseable ORCID LLM response for faculty id %s — "
                       "retrying once", faculty.get("_db_id"))
        if stats is not None:
            stats["llm_calls"] += 1
        text = _call()
        try:
            raw = _parse_json_response(text)
        except ValueError as e:
            raise AdjudicationError(
                "parse", f"Unparseable ORCID LLM response for faculty id "
                         f"{faculty.get('_db_id')} after retry") from e
    return validate_verdict(raw, [candidate])


def _adjudicate_orcid_group(conn, faculty, candidates, orcid_eligible,
                            orcid_source, llm_call, stats, max_orcid_fetches,
                            self_consistency, accept_confidence, model,
                            dry_run):
    """Adjudicate an ORCID-only group — a binary confirm/abstain of a single
    uncorroborated ORCID profile. Self-consistency re-asks with a paraphrased
    adversarial prompt (order-shuffle is meaningless for one candidate).
    Accepts clear the stricter acceptance_guardrails_orcid. Returns True when
    committed."""
    candidate = orcid_eligible[0]
    dossier = build_orcid_dossier(candidate, faculty, orcid_source, stats,
                                  max_orcid_fetches)
    prompt = build_orcid_user_prompt(faculty, candidate, dossier)

    stats["llm_calls"] += 1
    first = _adjudicate_orcid_once(faculty, prompt, ORCID_SYSTEM_PROMPT,
                                   candidate, llm_call, stats)
    second = None
    if first["decision"] == "accept" and self_consistency:
        stats["llm_calls"] += 1
        second = _adjudicate_orcid_once(faculty, prompt,
                                        ORCID_SYSTEM_PROMPT_RECHECK,
                                        candidate, llm_call, stats)
    verdict = merge_verdicts(first, second)

    ok = blocked = False
    if verdict["decision"] == "accept":
        ok, reason = acceptance_guardrails_orcid(verdict, candidate, dossier,
                                                 accept_confidence)
        if not ok:
            stats["orcid_guardrail_blocked"] += 1
            blocked = True
            verdict = dict(verdict, decision="abstain", guardrail=reason)

    if ok and not dry_run:
        rule_info = {"rule": "llm_orcid_adjudication",
                     "prompt_version": PROMPT_VERSION,
                     "model": model,
                     "verdict_1": first,
                     "verdict_2": second,
                     "employment_verified": dossier.get("employment_verified"),
                     "record_name_similarity":
                         dossier.get("record_name_similarity")}
        # No duplicate-profile cluster for ORCID; any other rows are rejected.
        if _commit_accept(conn, faculty, candidates, verdict, None, rule_info):
            stats["orcid_accepted"] += 1
            return True
        return False

    # Abstain / reject_all / blocked / dry-run accept: annotate only.
    if ok and dry_run:
        stats["orcid_accepted"] += 1   # would have accepted
    else:
        stats["orcid_annotated"] += 1
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
