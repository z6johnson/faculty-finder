"""arXiv API client.

Queries the public arXiv API for recent preprints by a faculty member.
No API key required; arXiv asks for at most one request every 3 seconds.

Docs: https://info.arxiv.org/help/api/index.html
Used primarily by physical-sciences and engineering divisions
(physics, math, CS, ECE), but safe to run for any division.

Because the arXiv author search is name-only (no affiliation field), this
source requires an exact full-name match in an entry's author list before
returning anything, and is given lower confidence than affiliation-verified
sources.
"""

import logging
import xml.etree.ElementTree as ET

from .base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "http://export.arxiv.org/api/query"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

MAX_PUBLICATIONS = 15


class ArxivSource(BaseSource):
    source_name = "arxiv"
    min_request_interval = 3.0  # arXiv asks for 3s between requests
    confidence = 0.65  # name-only matching; no affiliation signal

    def fields_provided(self):
        return ["recent_publications"]

    def fetch(self, faculty_dict):
        """Search arXiv for recent preprints by this faculty member."""
        first = (faculty_dict.get("first_name") or "").strip()
        last = (faculty_dict.get("last_name") or "").strip()
        if not first or not last:
            return None

        search_query = f'au:"{last}_{first[0]}"'
        params = {
            "search_query": search_query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": 20,
        }

        resp = self._get(API_URL, params=params)
        if not resp:
            return None

        publications = self._parse_feed(resp.text, first, last)
        if not publications:
            return None

        return {
            "recent_publications": publications,
            "_source_url": f"{API_URL}?search_query={search_query}",
        }

    def _parse_feed(self, xml_text, first, last):
        """Parse the Atom feed, keeping only entries with an exact name match.

        arXiv author search matches on surname + initial, which is far too
        loose on its own. We require the entry's author list to contain the
        exact "First Last" name before trusting it.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.warning("Failed to parse arXiv Atom feed for %s %s",
                           first, last)
            return []

        full_name = f"{first} {last}".lower()
        publications = []

        for entry in root.findall("atom:entry", ATOM_NS):
            if not self._has_exact_author(entry, full_name):
                continue

            pub = {}

            title_el = entry.find("atom:title", ATOM_NS)
            if title_el is not None and title_el.text:
                pub["title"] = " ".join(title_el.text.split())

            published_el = entry.find("atom:published", ATOM_NS)
            if published_el is not None and published_el.text:
                try:
                    pub["year"] = int(published_el.text[:4])
                except ValueError:
                    pass

            pub["journal"] = "arXiv preprint"

            # arXiv-issued DOI, if present
            doi_el = entry.find("{http://arxiv.org/schemas/atom}doi")
            if doi_el is not None and doi_el.text:
                pub["doi"] = doi_el.text.strip()

            if pub.get("title"):
                publications.append(pub)
            if len(publications) >= MAX_PUBLICATIONS:
                break

        return publications

    def _has_exact_author(self, entry, full_name):
        """Check whether the entry's author list contains the exact name."""
        for author in entry.findall("atom:author", ATOM_NS):
            name_el = author.find("atom:name", ATOM_NS)
            if name_el is not None and name_el.text:
                if name_el.text.strip().lower() == full_name:
                    return True
        return False
