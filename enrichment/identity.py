"""Identity resolution: link EAH-seeded faculty to external researcher IDs.

EAH-seeded faculty start with only HR data (name, title, division, maybe
email). Before deep enrichment we resolve each person to an OpenAlex author
(and, when known, an ORCID iD) so the source adapters fetch the right
person's work. Run as the 'identity_resolve' job kind, before 'backfill'.

Decisions per faculty (faculty.identity_status):
  score >= 0.9, unambiguous  -> write openalex_id (+ orcid if empty), 'auto'
  0.6 - 0.9, or tied         -> store identity_candidates rows, 'ambiguous'
                                (admin review queue accepts/rejects)
  no candidates              -> 'not_found' (re-swept periodically)

Conservative auto-accept rules (enrichment/identity_rules.py) decide some
would-be-ambiguous cases without review: duplicate OpenAlex profiles of the
same person collapse into one (method 'identity_auto_rule'), exact-name
candidates corroborated by an employment-verified ORCID record accept below
0.9, and ORCID-fallback hits accept when uniquely and strictly verified.
resweep_pending() applies the same rules to the already-queued backlog.
"""

import json
import logging

from data import db
from utils.names import name_similarity, search_name_variants

from . import identity_rules
from .sources.openalex import OpenAlexSource, UCSD_INSTITUTION_ID, AUTHORS_URL
from .sources.orcid import ORCIDSource

logger = logging.getLogger(__name__)

AUTO_ACCEPT_SCORE = 0.9
CANDIDATE_SCORE = 0.6
# A rival within this margin of the best score makes the match ambiguous.
TIE_MARGIN = 0.05
# Cap on the extra author-search requests the recall fallbacks may spend
# per faculty when the primary "first last" query returns nothing.
MAX_VARIANT_SEARCHES = 4

# OpenAlex topic domains -> division slugs they are consistent with.
# Used as a weak positive signal; absence is only a mild penalty because
# plenty of faculty publish across domains.
_DOMAIN_DIVISIONS = {
    "health sciences": {"som", "hwsph", "skaggs", "bio-sci"},
    "life sciences": {"bio-sci", "som", "sio", "skaggs", "hwsph"},
    "physical sciences": {"phys-sci", "sio", "jacobs"},
    "social sciences": {"soc-sci", "rady", "gps", "arts-hum", "hwsph"},
}


def _domain_consistent(domains, division):
    if not domains or not division:
        return None  # unknown
    for d in domains:
        if division in _DOMAIN_DIVISIONS.get(d.lower(), set()):
            return True
    return False


def _score_openalex_author(author, faculty):
    """Score one OpenAlex author hit against a faculty record. Returns
    (score, evidence dict)."""
    first = faculty.get("first_name", "")
    last = faculty.get("last_name", "")

    best_name_sim = 0.0
    names = [author.get("display_name") or ""]
    names.extend(author.get("display_name_alternatives") or [])
    for name in names:
        parts = name.split()
        if len(parts) < 2:
            continue
        best_name_sim = max(best_name_sim,
                            name_similarity(first, last, parts[0], parts[-1]))
    if best_name_sim == 0.0:
        return 0.0, {}

    # Affiliation currency: is UCSD a *current* institution or just historical?
    current = False
    listed = False
    for inst in (author.get("last_known_institutions") or []):
        if (inst.get("id") or "").endswith(UCSD_INSTITUTION_ID):
            current = True
    for aff in (author.get("affiliations") or []):
        inst = aff.get("institution") or {}
        if (inst.get("id") or "").endswith(UCSD_INSTITUTION_ID):
            listed = True
    affiliation_score = 1.0 if current else (0.7 if listed else 0.4)

    domains = []
    for topic in (author.get("topics") or [])[:10]:
        domain = ((topic.get("domain") or {}).get("display_name") or "")
        if domain and domain not in domains:
            domains.append(domain)
    consistent = _domain_consistent(domains, faculty.get("department"))
    if consistent is True:
        domain_score = 1.0
    elif consistent is None:
        domain_score = 0.7   # no topic data — neutral-ish
    else:
        domain_score = 0.4

    score = round(0.55 * best_name_sim + 0.3 * affiliation_score
                  + 0.15 * domain_score, 3)

    evidence = {
        "name_similarity": best_name_sim,
        "ucsd_current": current,
        "ucsd_listed": listed,
        "topic_domains": domains,
        "domain_consistent": consistent,
        "works_count": author.get("works_count"),
        "cited_by_count": author.get("cited_by_count"),
        "display_name": author.get("display_name"),
    }
    orcid_url = author.get("orcid") or ""
    if orcid_url:
        evidence["orcid"] = orcid_url.rsplit("/", 1)[-1]
    return score, evidence


def _identity_log_entry(faculty_id, stable_key, candidate, *, method,
                        confidence, rule_info=None):
    return {
        "faculty_id": faculty_id,
        "stable_key": stable_key,
        "source_name": candidate["source"],
        "source_url": (f"https://openalex.org/{candidate['external_id']}"
                       if candidate["source"] == "openalex"
                       else f"https://orcid.org/{candidate['external_id']}"),
        "field_updated": "identity",
        "old_value": "unresolved",
        "new_value": candidate["external_id"],
        "confidence": confidence,
        "method": method,
        "raw_response": (json.dumps(rule_info, ensure_ascii=False)
                         if rule_info else None),
        "retrieved_at": db._now_iso(),
    }


def _adopt_cluster_orcid(canonical, candidates, cluster_ids):
    """When duplicate profiles collapse, the canonical one may lack the
    ORCID a sibling carries — same person, so adopt it."""
    if canonical["evidence"].get("orcid"):
        return
    for c in candidates:
        if c.get("external_id") in cluster_ids:
            sibling_orcid = (c.get("evidence") or {}).get("orcid")
            if sibling_orcid:
                canonical["evidence"]["orcid"] = sibling_orcid
                return


class IdentityResolver:
    def __init__(self):
        self._openalex = OpenAlexSource()
        self._orcid = ORCIDSource()

    def _openalex_author_search(self, search, ucsd_filter=True):
        """One OpenAlex author search; returns raw author results."""
        params = {"search": search, "per-page": 10}
        if ucsd_filter:
            params["filter"] = f"affiliations.institution.id:{UCSD_INSTITUTION_ID}"
        resp = self._openalex._get(AUTHORS_URL,
                                   params=self._openalex._params(params))
        if not resp:
            return []
        try:
            return resp.json().get("results") or []
        except ValueError:
            return []

    def _score_authors(self, results, faculty, via=None,
                       require_ucsd_evidence=False):
        """Score raw author hits against the faculty record; keep qualifiers."""
        candidates = []
        for author in results:
            score, evidence = _score_openalex_author(author, faculty)
            if score < CANDIDATE_SCORE:
                continue
            if require_ucsd_evidence and not (evidence.get("ucsd_current")
                                              or evidence.get("ucsd_listed")):
                continue
            if via:
                evidence["via_search"] = via
            openalex_id = (author.get("id") or "").rsplit("/", 1)[-1]
            inst_names = [i.get("display_name") for i in
                          (author.get("last_known_institutions") or [])
                          if i.get("display_name")]
            candidates.append({
                "source": "openalex",
                "external_id": openalex_id,
                "display_name": author.get("display_name"),
                "affiliation": "; ".join(inst_names) or None,
                "score": score,
                "evidence": evidence,
            })
        candidates.sort(key=lambda c: -c["score"])
        return candidates

    def _openalex_candidates(self, faculty):
        """Search OpenAlex authors at UCSD by name; return scored candidates.

        When the exact "first last" query at UCSD misses, two recall
        fallbacks run (scoring against the true name is unchanged, so
        precision still comes from _score_openalex_author + downstream
        guardrails):
          1. name-variant queries (initials, dropped middles, compound last
             names) still filtered to UCSD;
          2. the exact name *without* the institution filter — for new hires
             whose last_known_institution isn't UCSD yet — keeping only hits
             whose own affiliation history shows UCSD.
        """
        first = faculty.get("first_name", "")
        last = faculty.get("last_name", "")
        if not first or not last:
            return []
        candidates = self._score_authors(
            self._openalex_author_search(f"{first} {last}"), faculty)
        if candidates:
            return candidates

        seen_ids = set()
        for vf, vl in search_name_variants(first, last)[:MAX_VARIANT_SEARCHES]:
            results = self._openalex_author_search(f"{vf} {vl}")
            for c in self._score_authors(results, faculty,
                                         via=f"name_variant:{vf} {vl}"):
                if c["external_id"] not in seen_ids:
                    seen_ids.add(c["external_id"])
                    candidates.append(c)
        if candidates:
            candidates.sort(key=lambda c: -c["score"])
            return candidates

        results = self._openalex_author_search(f"{first} {last}",
                                               ucsd_filter=False)
        candidates = self._score_authors(results, faculty,
                                         via="no_institution_filter",
                                         require_ucsd_evidence=True)
        return candidates

    def _orcid_fallback(self, faculty):
        """ORCID name+affiliation search when OpenAlex finds nothing."""
        first = faculty.get("first_name", "")
        last = faculty.get("last_name", "")
        orcid_id, hit_count = self._orcid.search_by_name_counted(first, last)
        if not orcid_id:
            return None
        record = self._orcid._fetch_full_record(orcid_id)
        if not record or not self._orcid._has_ucsd_affiliation(record):
            return None
        # Employment-grade facts let resolve() auto-accept the airtight
        # subset (unique hit, employment-verified, exact record name or
        # matching email); everything else stays reviewed-tier.
        verification = self._orcid._verify_record(
            record, orcid_id, first, last, email=faculty.get("email"))
        return {
            "source": "orcid",
            "external_id": orcid_id,
            "display_name": f"{first} {last}",
            "affiliation": "UCSD (per ORCID employment/education)",
            # Name match came from the ORCID query itself; affiliation was
            # verified against the full record — solid but reviewed-tier.
            "score": 0.8,
            "evidence": {
                "orcid": orcid_id,
                "via": "orcid_affiliation_search",
                "unique_hit": hit_count == 1,
                "employment_verified": verification["employment_verified"],
                "record_name_similarity": verification["record_name_similarity"],
                "record_email_match": verification["record_email_match"],
            },
        }

    def _accept(self, conn, faculty, candidate, *, method, confidence,
                rule_info=None):
        """Write the candidate's external ids onto the faculty row, mark it
        'auto', and log the decision. A duplicate-tie cluster's sibling
        profile ids are kept as alternates so enrichment reads their works
        too."""
        faculty_id = faculty["_db_id"]
        if candidate["source"] == "openalex":
            conn.execute(
                "UPDATE faculty SET openalex_id = COALESCE(openalex_id, ?)"
                " WHERE id = ?", (candidate["external_id"], faculty_id))
            if candidate["evidence"].get("orcid"):
                conn.execute(
                    "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
                    (candidate["evidence"]["orcid"], faculty_id))
            for alt_id in (rule_info or {}).get("cluster") or []:
                db.add_openalex_alt(conn, faculty_id, alt_id)
        else:
            conn.execute(
                "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
                (candidate["external_id"], faculty_id))
        db.set_identity_status(conn, faculty_id, "auto")
        db.append_log(conn, [_identity_log_entry(
            faculty_id, faculty.get("_stable_key"), candidate,
            method=method, confidence=confidence, rule_info=rule_info)])

    def resolve(self, conn, faculty):
        """Resolve one faculty record; writes status/candidates. Returns the
        resulting identity_status."""
        faculty_id = faculty["_db_id"]
        candidates = self._openalex_candidates(faculty)

        if not candidates:
            fallback = self._orcid_fallback(faculty)
            if fallback:
                candidates = [fallback]

        if not candidates:
            # A still-empty re-search must not demote a terminal
            # no_footprint back to the retried-weekly pool.
            if faculty.get("identity_status") != "no_footprint":
                db.set_identity_status(conn, faculty_id, "not_found")
            return "not_found"

        collapse = identity_rules.collapse_duplicate_ties(candidates, TIE_MARGIN)
        best = collapse["canonical"]
        unambiguous = (collapse["effective_score"] - collapse["runner_up"]) > TIE_MARGIN

        if collapse["effective_score"] >= AUTO_ACCEPT_SCORE and unambiguous:
            rule_info = None
            method = "identity_auto"
            if collapse["collapsed"]:
                method = "identity_auto_rule"
                rule_info = {"rule": "duplicate_tie_collapse",
                             "cluster": collapse["cluster_ids"]}
                _adopt_cluster_orcid(best, candidates, collapse["cluster_ids"])
            self._accept(conn, faculty, best, method=method,
                         confidence=collapse["effective_score"],
                         rule_info=rule_info)
            return "auto"

        # ORCID employment corroboration: an exact-name UCSD candidate whose
        # attached ORCID record independently verifies UCSD employment.
        if (best["source"] == "openalex" and unambiguous
                and identity_rules.orcid_corroboration_precheck(faculty, best)):
            verification = self._orcid.verify_ucsd_employment(
                identity_rules.evidence_orcid(best),
                faculty.get("first_name", ""), faculty.get("last_name", ""),
                email=faculty.get("email"))
            if identity_rules.orcid_corroboration_confirms(verification):
                self._accept(conn, faculty, best, method="identity_auto_rule",
                             confidence=best["score"],
                             rule_info={"rule": "orcid_employment_corroboration",
                                        **verification})
                return "auto"

        # Strictly verified ORCID fallback (unique hit + employment +
        # exact record name, or exact email match).
        if (best["source"] == "orcid"
                and identity_rules.orcid_fallback_qualifies(best.get("evidence"))):
            self._accept(conn, faculty, best, method="identity_auto_rule",
                         confidence=best["score"],
                         rule_info={"rule": "orcid_fallback_verified",
                                    **{k: best["evidence"].get(k) for k in
                                       ("unique_hit", "employment_verified",
                                        "record_name_similarity",
                                        "record_email_match")}})
            return "auto"

        db.clear_identity_candidates(conn, faculty_id)
        db.insert_identity_candidates(conn, faculty_id, candidates[:5])
        db.set_identity_status(conn, faculty_id, "ambiguous")
        return "ambiguous"


def resolve_batch(department=None, pi_only=False, limit=None,
                  include_not_found=False, progress_callback=None,
                  time_budget_seconds=None):
    """Resolve identities for unresolved faculty. Returns a stats dict."""
    import time as _time

    conn = db.get_write_conn()
    # no_footprint is terminal only until a search hits: include_not_found
    # re-searches those rows too, and any candidate promotes them back.
    statuses = (("unresolved", "not_found", "no_footprint")
                if include_not_found else ("unresolved",))
    queue = db.fetch_identity_queue(conn, department=department,
                                    pi_only=pi_only, statuses=statuses,
                                    limit=limit)
    logger.info("Identity resolution: %d faculty queued (dept=%s, pi_only=%s)",
                len(queue), department or "all", pi_only)

    resolver = IdentityResolver()
    stats = {"processed": 0, "auto": 0, "ambiguous": 0, "not_found": 0}
    start = _time.monotonic()

    for i, faculty in enumerate(queue):
        if time_budget_seconds is not None:
            if _time.monotonic() - start > time_budget_seconds - 30:
                logger.warning("Identity resolution: time budget reached "
                               "after %d/%d", i, len(queue))
                break
        try:
            outcome = resolver.resolve(conn, faculty)
        except Exception:
            logger.exception("Identity resolution failed for faculty id %s",
                             faculty.get("_db_id"))
            continue
        stats["processed"] += 1
        stats[outcome] = stats.get(outcome, 0) + 1
        conn.commit()
        if progress_callback:
            progress_callback(i + 1, len(queue))

    conn.commit()
    logger.info("Identity resolution done: %s", stats)
    return stats


def mark_no_footprint(conn, department=None):
    """Terminal disposition for the not-findable: move 'not_found' faculty
    whose HR data shows no research-bearing role (identity_rules.
    no_research_footprint) to 'no_footprint' so they stop counting as
    pending. Logged per faculty; reversible via include_not_found resolves.
    Returns the number of rows marked."""
    queue = db.fetch_identity_queue(conn, department=department,
                                    statuses=("not_found",))
    marked = 0
    for faculty in queue:
        if not identity_rules.no_research_footprint(faculty):
            continue
        db.set_identity_status(conn, faculty["_db_id"], "no_footprint")
        db.append_log(conn, [{
            "faculty_id": faculty["_db_id"],
            "stable_key": faculty.get("_stable_key"),
            "source_name": "identity",
            "field_updated": "identity_status",
            "old_value": "not_found",
            "new_value": "no_footprint",
            "method": "identity_no_footprint_rule",
            "raw_response": json.dumps(
                {"pi_eligible": faculty.get("pi_eligible"),
                 "title": faculty.get("title"),
                 "job_code_description": faculty.get("job_code_description")},
                ensure_ascii=False),
            "retrieved_at": db._now_iso(),
        }])
        marked += 1
    conn.commit()
    return marked


def resweep_pending(department=None, max_orcid_lookups=None,
                    progress_callback=None, time_budget_seconds=None,
                    orcid_source=None, mark_terminal=True):
    """Apply the conservative auto-accept rules to the pending review queue.

    Works from the stored identity_candidates rows (tie collapse needs no
    network; ORCID verification is budgeted by max_orcid_lookups). Accepts
    go through db.decide_identity_candidate — the same path as a manual
    accept — and are logged with method 'identity_auto_rule'. Whole groups
    are never auto-rejected; non-qualifying faculty stay queued for humans.
    An accepted verdict merges its duplicate-profile cluster as alternate
    ids and retires the remaining candidates. When mark_terminal is set,
    'not_found' faculty with no research-bearing role move to the terminal
    'no_footprint' status (mark_no_footprint). Returns a stats dict.
    """
    import time as _time

    conn = db.get_write_conn()
    rows = db.list_identity_candidates(conn, department=department, limit=None)
    groups = {}
    for row in rows:
        groups.setdefault(row["faculty_id"], []).append(row)
    logger.info("Identity re-sweep: %d faculty with pending candidates "
                "(dept=%s)", len(groups), department or "all")

    orcid_source = orcid_source or ORCIDSource()
    stats = {"faculty_seen": len(groups), "accepted_tie_collapse": 0,
             "accepted_orcid_corroboration": 0, "accepted_orcid_fallback": 0,
             "orcid_lookups": 0, "left_pending": 0, "no_footprint_marked": 0}
    start = _time.monotonic()

    def _lookup_budget_left():
        return max_orcid_lookups is None or stats["orcid_lookups"] < max_orcid_lookups

    for i, (faculty_id, group) in enumerate(groups.items()):
        if time_budget_seconds is not None:
            if _time.monotonic() - start > time_budget_seconds - 30:
                logger.warning("Identity re-sweep: time budget reached "
                               "after %d/%d", i, len(groups))
                break
        faculty = {
            "_db_id": faculty_id,
            "_stable_key": group[0].get("f_stable_key"),
            "first_name": group[0]["f_first"],
            "last_name": group[0]["f_last"],
            "email": group[0].get("f_email"),
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
        candidates.sort(key=lambda c: -c["score"])

        try:
            accepted = _resweep_decide(faculty, candidates, orcid_source,
                                       stats, _lookup_budget_left)
        except Exception:
            logger.exception("Identity re-sweep failed for faculty id %s",
                             faculty_id)
            stats["left_pending"] += 1
            continue

        if accepted:
            candidate, rule_info, stat_key, sibling_orcid = accepted
            decided = db.decide_identity_candidate(conn, candidate["_row_id"],
                                                   "accept")
            if decided:
                # Cluster siblings are duplicate profiles of the accepted
                # person — merge them as alternate ids; the remaining
                # candidates lost an airtight verdict and are rejected.
                cluster = set((rule_info or {}).get("cluster") or [])
                for c in candidates:
                    if c["_row_id"] == candidate["_row_id"]:
                        continue
                    if c["external_id"] in cluster:
                        db.decide_identity_candidate(conn, c["_row_id"],
                                                     "merge")
                    else:
                        db.decide_identity_candidate(conn, c["_row_id"],
                                                     "reject")
                if sibling_orcid:
                    conn.execute(
                        "UPDATE faculty SET orcid = COALESCE(orcid, ?)"
                        " WHERE id = ?", (sibling_orcid, faculty_id))
                db.append_log(conn, [_identity_log_entry(
                    faculty_id, faculty["_stable_key"], candidate,
                    method="identity_auto_rule",
                    confidence=candidate["score"], rule_info=rule_info)])
                stats[stat_key] += 1
            conn.commit()
        else:
            stats["left_pending"] += 1
        if progress_callback:
            progress_callback(i + 1, len(groups))

    if mark_terminal:
        stats["no_footprint_marked"] = mark_no_footprint(conn,
                                                         department=department)

    conn.commit()
    logger.info("Identity re-sweep done: %s", stats)
    return stats


def _resweep_decide(faculty, candidates, orcid_source, stats, budget_left):
    """Pick at most one candidate to auto-accept for one faculty. Returns
    (candidate, rule_info, stat_key, sibling_orcid) or None."""
    collapse = identity_rules.collapse_duplicate_ties(candidates, TIE_MARGIN)
    best = collapse["canonical"]
    if best is None:
        return None
    unambiguous = (collapse["effective_score"] - collapse["runner_up"]) > TIE_MARGIN

    if (collapse["collapsed"] and unambiguous
            and collapse["effective_score"] >= AUTO_ACCEPT_SCORE):
        sibling_orcid = None
        if not best["evidence"].get("orcid"):
            _adopt_cluster_orcid(best, candidates, collapse["cluster_ids"])
            sibling_orcid = best["evidence"].get("orcid")
        return (best, {"rule": "duplicate_tie_collapse",
                       "cluster": collapse["cluster_ids"]},
                "accepted_tie_collapse", sibling_orcid)

    if (best["source"] == "openalex" and unambiguous
            and identity_rules.orcid_corroboration_precheck(faculty, best)
            and budget_left()):
        stats["orcid_lookups"] += 1
        verification = orcid_source.verify_ucsd_employment(
            identity_rules.evidence_orcid(best),
            faculty["first_name"], faculty["last_name"],
            email=faculty.get("email"))
        if identity_rules.orcid_corroboration_confirms(verification):
            return (best, {"rule": "orcid_employment_corroboration",
                           **verification},
                    "accepted_orcid_corroboration", None)
        return None

    if best["source"] == "orcid":
        evidence = best.get("evidence") or {}
        if "employment_verified" not in evidence:
            # Legacy fallback row ({orcid, via} only): re-verify fresh, but
            # only trust it if the same ORCID still comes back from search.
            if not budget_left():
                return None
            stats["orcid_lookups"] += 1
            found_id, hit_count = orcid_source.search_by_name_counted(
                faculty["first_name"], faculty["last_name"])
            if found_id != best["external_id"]:
                return None
            verification = orcid_source.verify_ucsd_employment(
                best["external_id"], faculty["first_name"],
                faculty["last_name"], email=faculty.get("email"))
            if not verification:
                return None
            evidence = dict(evidence, unique_hit=(hit_count == 1),
                            **{k: verification[k] for k in
                               ("employment_verified",
                                "record_name_similarity",
                                "record_email_match")})
        if identity_rules.orcid_fallback_qualifies(evidence):
            return (best, {"rule": "orcid_fallback_verified",
                           **{k: evidence.get(k) for k in
                              ("unique_hit", "employment_verified",
                               "record_name_similarity",
                               "record_email_match")}},
                    "accepted_orcid_fallback", None)
    return None
