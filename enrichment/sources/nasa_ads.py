"""NASA ADS (Astrophysics Data System) API client.

Queries the NASA ADS search API for publications by a faculty member,
constrained to UCSD-affiliated records.

Docs: https://ui.adsabs.harvard.edu/help/api/
Requires a free API key via the ADS_API_KEY environment variable
(register at https://ui.adsabs.harvard.edu/user/settings/token).
Used primarily by physics and astronomy divisions, but safe to run for
any division.
"""

import logging
import os

from .base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "https://api.adsabs.harvard.edu/v1/search/query"

MAX_PUBLICATIONS = 15


class NASAADSSource(BaseSource):
    source_name = "nasa_ads"
    min_request_interval = 1.0
    confidence = 0.8  # affiliation-filtered search

    def __init__(self):
        super().__init__()
        self._api_key = os.getenv("ADS_API_KEY", "")
        if self._api_key:
            self._session.headers.update({
                "Authorization": f"Bearer {self._api_key}",
            })
        self._warned_no_key = False

    def fields_provided(self):
        return ["recent_publications"]

    def fetch(self, faculty_dict):
        """Search NASA ADS for UCSD-affiliated publications by this person."""
        if not self._api_key:
            if not self._warned_no_key:
                logger.debug("ADS_API_KEY not set; skipping NASA ADS source")
                self._warned_no_key = True
            return None

        first = (faculty_dict.get("first_name") or "").strip()
        last = (faculty_dict.get("last_name") or "").strip()
        if not first or not last:
            return None

        query = (
            f'author:"{last}, {first}" '
            'aff:("ucsd" OR "uc san diego" OR '
            '"university of california, san diego")'
        )
        params = {
            "q": query,
            "fl": "title,year,pub,doi,citation_count",
            "rows": 20,
            "sort": "date desc",
        }

        resp = self._get(API_URL, params=params)
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON from NASA ADS for %s %s", first, last)
            return None

        docs = ((data.get("response") or {}).get("docs")) or []
        if not docs:
            return None

        publications = []
        for doc in docs:
            pub = self._parse_doc(doc)
            if pub:
                publications.append(pub)
            if len(publications) >= MAX_PUBLICATIONS:
                break

        if not publications:
            return None

        return {
            "recent_publications": publications,
            "_source_url": f"{API_URL}?q={query}",
        }

    def _parse_doc(self, doc):
        """Convert an ADS doc into a publication dict."""
        pub = {}

        # ADS returns title and doi as lists
        title = doc.get("title")
        if isinstance(title, list):
            title = title[0] if title else ""
        if title:
            pub["title"] = title.strip()

        year = doc.get("year")
        if year:
            try:
                pub["year"] = int(year)
            except (TypeError, ValueError):
                pass

        journal = doc.get("pub")
        if journal:
            pub["journal"] = journal

        doi = doc.get("doi")
        if isinstance(doi, list):
            doi = doi[0] if doi else None
        if doi:
            pub["doi"] = doi

        citations = doc.get("citation_count")
        if citations is not None:
            pub["citations"] = citations

        return pub if pub.get("title") else None
