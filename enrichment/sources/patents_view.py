"""USPTO PatentsView API client — granted patents by UCSD inventors.

PatentsView (https://search.patentsview.org/docs/) indexes US patent grants
with disambiguated inventors and assignees. We search by inventor name and
require the assignee to be the University of California system ("Regents of
the University of California"), which keeps bare name matches safe.

Requires a free API key (PATENTSVIEW_API_KEY env var); fetch() is a no-op
without one. Used by som/skaggs/jacobs/bio-sci/phys-sci bundles.
"""

import json
import logging
import os

from .base import BaseSource

logger = logging.getLogger(__name__)

SEARCH_URL = "https://search.patentsview.org/api/v1/patent/"

UC_ASSIGNEE_STRINGS = [
    "regents of the university of california",
    "university of california",
]


class PatentsViewSource(BaseSource):
    source_name = "patents_view"
    min_request_interval = 1.0
    confidence = 0.8

    def __init__(self):
        super().__init__()
        self._api_key = os.environ.get("PATENTSVIEW_API_KEY", "").strip()
        if self._api_key:
            self._session.headers["X-Api-Key"] = self._api_key

    def fields_provided(self):
        return ["patents"]

    def fetch(self, faculty_dict):
        if not self._api_key:
            logger.debug("PatentsView: PATENTSVIEW_API_KEY unset — skipping")
            return None
        first = (faculty_dict.get("first_name") or "").strip()
        last = (faculty_dict.get("last_name") or "").strip()
        if not first or not last:
            return None

        query = {
            "_and": [
                {"_begins": {"inventors.inventor_name_first": first}},
                {"inventors.inventor_name_last": last},
            ]
        }
        fields = ["patent_id", "patent_title", "patent_date",
                  "assignees.assignee_organization",
                  "inventors.inventor_name_first", "inventors.inventor_name_last"]
        resp = self._get(SEARCH_URL, params={
            "q": json.dumps(query),
            "f": json.dumps(fields),
            "o": json.dumps({"size": 25}),
        })
        if not resp:
            return None
        try:
            patents = resp.json().get("patents") or []
        except ValueError:
            return None

        results = []
        for p in patents:
            # Identity guard: only keep UC-assigned patents.
            orgs = [(a.get("assignee_organization") or "").lower()
                    for a in (p.get("assignees") or [])]
            if not any(uc in org for org in orgs for uc in UC_ASSIGNEE_STRINGS):
                continue
            entry = {"title": p.get("patent_title") or "Untitled",
                     "patent_number": p.get("patent_id"),
                     "assignee": "Regents of the University of California"}
            date = p.get("patent_date") or ""
            if len(date) >= 4 and date[:4].isdigit():
                entry["year"] = int(date[:4])
            results.append(entry)
            if len(results) >= 20:
                break

        if not results:
            return None
        return {
            "patents": results,
            "_source_url": ("https://search.patentsview.org/api/v1/patent/"
                            f"?inventor={first}+{last}"),
        }
