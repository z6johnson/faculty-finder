"""dblp computer science bibliography client.

Queries the public dblp API for publications by a faculty member.
No API key required.

Docs: https://dblp.org/faq/How+to+use+the+dblp+search+API.html
Used primarily by computer science and engineering divisions, but safe
to run for any division.

dblp has no affiliation field in its search API, so this source resolves
the author first and requires exactly one exact-name match. dblp appends
numeric suffixes ("John Smith 0001") when several people share a name;
if more than one distinct person matches, we return None rather than
guess.
"""

import logging

from .base import BaseSource

logger = logging.getLogger(__name__)

AUTHOR_API = "https://dblp.org/search/author/api"
PUBL_API = "https://dblp.org/search/publ/api"

MAX_PUBLICATIONS = 15


class DBLPSource(BaseSource):
    source_name = "dblp"
    min_request_interval = 1.0
    confidence = 0.75

    def fields_provided(self):
        return ["recent_publications"]

    def fetch(self, faculty_dict):
        """Search dblp for publications by this faculty member."""
        first = (faculty_dict.get("first_name") or "").strip()
        last = (faculty_dict.get("last_name") or "").strip()
        if not first or not last:
            return None

        author_name = self._resolve_author(first, last)
        if not author_name:
            return None

        publications = self._fetch_publications(author_name)
        if not publications:
            return None

        query = "author:" + "_".join(author_name.split()) + ":"
        return {
            "recent_publications": publications,
            "_source_url": f"{PUBL_API}?q={query}",
        }

    def _resolve_author(self, first, last):
        """Resolve the dblp author record; None unless exactly one match.

        Returns the dblp author name (possibly with a numeric disambiguation
        suffix) when exactly one person matches "First Last". Multiple
        distinct people with the same base name mean we cannot disambiguate
        without an affiliation signal, so we bail out.
        """
        resp = self._get(
            AUTHOR_API,
            params={"q": f"{first} {last}", "format": "json"},
        )
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON from dblp author API for %s %s",
                           first, last)
            return None

        hits = (((data.get("result") or {}).get("hits") or {}).get("hit")) or []
        full_name = f"{first} {last}".lower()

        matches = []
        for hit in hits:
            info = hit.get("info") or {}
            author = (info.get("author") or "").strip()
            base_name = author
            parts = author.rsplit(" ", 1)
            # Strip dblp's 4-digit homonym suffix, e.g. "Jane Doe 0002"
            if len(parts) == 2 and parts[1].isdigit():
                base_name = parts[0]
            if base_name.lower() == full_name:
                matches.append(author)

        if not matches:
            return None
        if len(matches) > 1:
            logger.info("dblp: %d distinct people named %s %s; skipping",
                        len(matches), first, last)
            return None

        return matches[0]

    def _fetch_publications(self, author_name):
        """Fetch publications for the resolved dblp author."""
        query = "author:" + "_".join(author_name.split()) + ":"
        resp = self._get(
            PUBL_API,
            params={"q": query, "format": "json", "h": 20},
        )
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON from dblp publ API for %s",
                           author_name)
            return None

        hits = (((data.get("result") or {}).get("hits") or {}).get("hit")) or []

        publications = []
        for hit in hits:
            info = hit.get("info") or {}
            pub = {}

            title = (info.get("title") or "").strip().rstrip(".")
            if title:
                pub["title"] = title

            year = info.get("year")
            if year:
                try:
                    pub["year"] = int(year)
                except (TypeError, ValueError):
                    pass

            venue = info.get("venue")
            if isinstance(venue, list):
                venue = ", ".join(str(v) for v in venue)
            if venue:
                pub["journal"] = venue

            doi = info.get("doi")
            if doi:
                pub["doi"] = doi

            if pub.get("title"):
                publications.append(pub)
            if len(publications) >= MAX_PUBLICATIONS:
                break

        return publications or None
