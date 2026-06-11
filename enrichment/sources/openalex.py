"""OpenAlex API client — the discipline-agnostic enrichment backbone.

OpenAlex (https://docs.openalex.org/) indexes publications, citations, and
topic classifications across every discipline, including the humanities and
social sciences where PubMed/Semantic Scholar coverage is thin. One author
record provides h-index, citation/works counts, topics, and (when known) the
author's ORCID iD.

Lookup strategy (in priority order):
  1. Stored openalex_id (written by identity resolution or a prior run)
  2. Stored ORCID iD (filter=orcid:...)
  3. Name search filtered to the UCSD institution (ROR 0168r3w48 / I36258959)
     — only accepted when the result is unambiguous.

No auth required. Adding a ``mailto`` param (OPENALEX_MAILTO env var) joins
the polite pool with higher rate limits. Used by every division bundle.
"""

import logging
import os

from .base import BaseSource

logger = logging.getLogger(__name__)

AUTHORS_URL = "https://api.openalex.org/authors"
WORKS_URL = "https://api.openalex.org/works"

UCSD_INSTITUTION_ID = "I36258959"


class OpenAlexSource(BaseSource):
    source_name = "openalex"
    min_request_interval = 0.15  # polite pool allows ~10 req/s
    confidence = 0.85

    def __init__(self):
        super().__init__()
        self._mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
        if not self._mailto:
            logger.warning(
                "OPENALEX_MAILTO is not set — requests are outside the "
                "OpenAlex polite pool and will be rate-limited (429s). "
                "Set it to a contact email address.")

    def fields_provided(self):
        return ["openalex_id", "orcid", "h_index", "citation_count",
                "works_count", "recent_publications", "expertise_keywords"]

    def _params(self, extra=None):
        params = dict(extra or {})
        if self._mailto:
            params["mailto"] = self._mailto
        return params

    def fetch(self, faculty_dict):
        author = self._find_author(faculty_dict)
        if not author:
            return None

        openalex_id = (author.get("id") or "").rsplit("/", 1)[-1]
        result = {
            "openalex_id": openalex_id,
            "_source_url": f"https://openalex.org/{openalex_id}",
        }

        orcid_url = author.get("orcid") or ""
        if orcid_url:
            result["orcid"] = orcid_url.rsplit("/", 1)[-1]

        stats = author.get("summary_stats") or {}
        if stats.get("h_index") is not None:
            result["h_index"] = stats["h_index"]
        if author.get("cited_by_count") is not None:
            result["citation_count"] = author["cited_by_count"]
        if author.get("works_count") is not None:
            result["works_count"] = author["works_count"]

        keywords = [t.get("display_name") for t in (author.get("topics") or [])[:15]
                    if t.get("display_name")]
        if keywords:
            result["expertise_keywords"] = keywords

        # Alternate profiles (OpenAlex splits of the same person, merged in
        # identity review) contribute their works and counts too.
        alt_ids = [a for a in (faculty_dict.get("openalex_id_alt") or [])
                   if a and a != openalex_id]
        self._fold_in_alternates(result, alt_ids)

        pubs = self._fetch_works(openalex_id, alt_ids=alt_ids)
        if pubs:
            result["recent_publications"] = pubs

        return result

    def _fold_in_alternates(self, result, alt_ids):
        """Sum works/citation counts (max h_index) across alternate author
        profiles. Failures degrade gracefully to the primary's numbers."""
        for alt_id in alt_ids:
            resp = self._get(f"{AUTHORS_URL}/{alt_id}", params=self._params())
            if not resp:
                continue
            try:
                alt = resp.json()
            except ValueError:
                continue
            if alt.get("works_count") is not None:
                result["works_count"] = ((result.get("works_count") or 0)
                                         + alt["works_count"])
            if alt.get("cited_by_count") is not None:
                result["citation_count"] = ((result.get("citation_count") or 0)
                                            + alt["cited_by_count"])
            alt_h = (alt.get("summary_stats") or {}).get("h_index")
            if alt_h is not None:
                result["h_index"] = max(result.get("h_index") or 0, alt_h)

    def _find_author(self, faculty_dict):
        """Resolve the OpenAlex author record using the safest available key."""
        openalex_id = (faculty_dict.get("openalex_id") or "").strip()
        if openalex_id:
            resp = self._get(f"{AUTHORS_URL}/{openalex_id}", params=self._params())
            if resp:
                try:
                    return resp.json()
                except ValueError:
                    pass

        orcid = (faculty_dict.get("orcid") or "").strip()
        if orcid:
            resp = self._get(f"{AUTHORS_URL}/orcid:{orcid}", params=self._params())
            if resp:
                try:
                    return resp.json()
                except ValueError:
                    pass

        # Name search constrained to UCSD. Only trust an unambiguous hit:
        # a single result, or a clear leader whose name matches.
        first = faculty_dict.get("first_name", "")
        last = faculty_dict.get("last_name", "")
        if not first or not last:
            return None
        resp = self._get(AUTHORS_URL, params=self._params({
            "search": f"{first} {last}",
            "filter": f"affiliations.institution.id:{UCSD_INSTITUTION_ID}",
            "per-page": 5,
        }))
        if not resp:
            return None
        try:
            results = resp.json().get("results") or []
        except ValueError:
            return None

        matches = [a for a in results if self._name_matches(a, first, last)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.info("OpenAlex: %d UCSD authors match %s %s — skipping "
                        "(needs identity resolution)", len(matches), first, last)
        return None

    @staticmethod
    def _name_matches(author, first, last):
        from utils.names import name_similarity
        names = [author.get("display_name") or ""]
        names.extend(author.get("display_name_alternatives") or [])
        for name in names:
            parts = name.split()
            if len(parts) < 2:
                continue
            if name_similarity(first, last, parts[0], parts[-1]) >= 0.85:
                return True
        return False

    def _fetch_works(self, openalex_id, alt_ids=None):
        """Most recent works for an author id (plus merged alternate
        profiles — OpenAlex filter values OR together with '|')."""
        author_filter = "|".join([openalex_id] + list(alt_ids or []))
        resp = self._get(WORKS_URL, params=self._params({
            "filter": f"authorships.author.id:{author_filter}",
            "sort": "publication_date:desc",
            "per-page": 20,
        }))
        if not resp:
            return None
        try:
            works = resp.json().get("results") or []
        except ValueError:
            return None

        pubs = []
        for w in works:
            pub = {}
            if w.get("display_name"):
                pub["title"] = w["display_name"]
            if w.get("publication_year"):
                pub["year"] = w["publication_year"]
            source = ((w.get("primary_location") or {}).get("source") or {})
            if source.get("display_name"):
                pub["journal"] = source["display_name"]
            doi = w.get("doi") or ""
            if doi:
                pub["doi"] = doi.replace("https://doi.org/", "")
            if w.get("cited_by_count") is not None:
                pub["citations"] = w["cited_by_count"]
            if pub.get("title"):
                pubs.append(pub)
        return pubs or None
