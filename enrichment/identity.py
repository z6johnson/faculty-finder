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
"""

import logging

from data import db
from utils.names import name_similarity

from .sources.openalex import OpenAlexSource, UCSD_INSTITUTION_ID, AUTHORS_URL
from .sources.orcid import ORCIDSource

logger = logging.getLogger(__name__)

AUTO_ACCEPT_SCORE = 0.9
CANDIDATE_SCORE = 0.6
# A rival within this margin of the best score makes the match ambiguous.
TIE_MARGIN = 0.05

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


class IdentityResolver:
    def __init__(self):
        self._openalex = OpenAlexSource()
        self._orcid = ORCIDSource()

    def _openalex_candidates(self, faculty):
        """Search OpenAlex authors at UCSD by name; return scored candidates."""
        first = faculty.get("first_name", "")
        last = faculty.get("last_name", "")
        if not first or not last:
            return []
        resp = self._openalex._get(AUTHORS_URL, params=self._openalex._params({
            "search": f"{first} {last}",
            "filter": f"affiliations.institution.id:{UCSD_INSTITUTION_ID}",
            "per-page": 10,
        }))
        if not resp:
            return []
        try:
            results = resp.json().get("results") or []
        except ValueError:
            return []

        candidates = []
        for author in results:
            score, evidence = _score_openalex_author(author, faculty)
            if score < CANDIDATE_SCORE:
                continue
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

    def _orcid_fallback(self, faculty):
        """ORCID name+affiliation search when OpenAlex finds nothing."""
        orcid_id = self._orcid._search_by_name(
            faculty.get("first_name", ""), faculty.get("last_name", ""))
        if not orcid_id:
            return None
        record = self._orcid._fetch_full_record(orcid_id)
        if not record or not self._orcid._has_ucsd_affiliation(record):
            return None
        return {
            "source": "orcid",
            "external_id": orcid_id,
            "display_name": f"{faculty.get('first_name')} {faculty.get('last_name')}",
            "affiliation": "UCSD (per ORCID employment/education)",
            # Name match came from the ORCID query itself; affiliation was
            # verified against the full record — solid but reviewed-tier.
            "score": 0.8,
            "evidence": {"orcid": orcid_id, "via": "orcid_affiliation_search"},
        }

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
            db.set_identity_status(conn, faculty_id, "not_found")
            return "not_found"

        best = candidates[0]
        runner_up = candidates[1]["score"] if len(candidates) > 1 else 0.0
        unambiguous = (best["score"] - runner_up) > TIE_MARGIN

        if best["score"] >= AUTO_ACCEPT_SCORE and unambiguous:
            if best["source"] == "openalex":
                conn.execute(
                    "UPDATE faculty SET openalex_id = COALESCE(openalex_id, ?)"
                    " WHERE id = ?", (best["external_id"], faculty_id))
                if best["evidence"].get("orcid"):
                    conn.execute(
                        "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
                        (best["evidence"]["orcid"], faculty_id))
            else:
                conn.execute(
                    "UPDATE faculty SET orcid = COALESCE(orcid, ?) WHERE id = ?",
                    (best["external_id"], faculty_id))
            db.set_identity_status(conn, faculty_id, "auto")
            db.append_log(conn, [{
                "faculty_id": faculty_id,
                "stable_key": faculty.get("_stable_key"),
                "source_name": best["source"],
                "source_url": (f"https://openalex.org/{best['external_id']}"
                               if best["source"] == "openalex"
                               else f"https://orcid.org/{best['external_id']}"),
                "field_updated": "identity",
                "old_value": "unresolved",
                "new_value": best["external_id"],
                "confidence": best["score"],
                "method": "identity_auto",
                "raw_response": None,
                "retrieved_at": db._now_iso(),
            }])
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
    statuses = ("unresolved", "not_found") if include_not_found else ("unresolved",)
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
