"""Crossref API client — funder metadata for already-identified publications.

Crossref (https://api.crossref.org) holds DOI-level metadata including funder
names and award numbers (via the Crossref Funder Registry). Name search is
too noisy for identity, so this adapter is keyed off DOIs already attached to
the faculty record (typically by OpenAlex): it looks up each DOI and emits
funder entries as ``funded_grants`` supplements.

No auth required; a ``mailto`` (CROSSREF_MAILTO or OPENALEX_MAILTO env var)
joins the polite pool. Used by divisions without a federal-grants source
(arts-hum, phys-sci, soc-sci, rady, gps).
"""

import logging
import os

from .base import BaseSource

logger = logging.getLogger(__name__)

WORKS_URL = "https://api.crossref.org/works/"

MAX_DOI_LOOKUPS = 8


class CrossrefSource(BaseSource):
    source_name = "crossref"
    min_request_interval = 1.0
    confidence = 0.7

    def __init__(self):
        super().__init__()
        mailto = (os.environ.get("CROSSREF_MAILTO", "").strip()
                  or os.environ.get("OPENALEX_MAILTO", "").strip())
        if mailto:
            self._session.headers["User-Agent"] += f" mailto:{mailto}"

    def fields_provided(self):
        return ["funded_grants"]

    def fetch(self, faculty_dict):
        dois = []
        for pub in (faculty_dict.get("recent_publications") or []):
            doi = (pub.get("doi") or "").strip()
            if doi:
                dois.append(doi)
        if not dois:
            return None

        grants = []
        seen = set()
        for doi in dois[:MAX_DOI_LOOKUPS]:
            for funder in self._funders_for_doi(doi):
                key = (funder.get("agency", "").lower(),
                       tuple(funder.get("award_numbers") or []))
                if key in seen:
                    continue
                seen.add(key)
                grants.append(funder)

        if not grants:
            return None
        return {
            "funded_grants": grants[:15],
            "_source_url": f"https://api.crossref.org/works/{dois[0]}",
        }

    def _funders_for_doi(self, doi):
        resp = self._get(WORKS_URL + doi)
        if not resp:
            return []
        try:
            message = resp.json().get("message") or {}
        except ValueError:
            return []

        title_list = message.get("title") or []
        title = title_list[0] if title_list else ""
        funders = []
        for f in message.get("funder") or []:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            entry = {
                "title": f"Funding acknowledged on: {title}" if title else "Funder acknowledgement",
                "agency": name,
                "type": "funder_acknowledgement",
            }
            awards = [a for a in (f.get("award") or []) if a]
            if awards:
                entry["award_numbers"] = awards[:5]
            funders.append(entry)
        return funders
