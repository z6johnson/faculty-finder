"""RePEc / IDEAS API client.

Queries the RePEc API for economics publications by a faculty member.

Docs: https://ideas.repec.org/api.html
Requires a registered access code via the REPEC_API_CODE environment
variable (request one at https://ideas.repec.org/api.html).
Used primarily by economics and management divisions, but safe to run
for any division.

The RePEc API is loosely documented and keyed on author "short-ids"
rather than names, so this adapter is best-effort: it looks up a
short-id by name and only proceeds when exactly one is returned. All
parsing is wrapped defensively; anything unexpected results in None.
"""

import logging
import os

from .base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "https://api.repec.org/call.cgi"

MAX_PUBLICATIONS = 15


class RePEcSource(BaseSource):
    source_name = "repec"
    min_request_interval = 1.0
    confidence = 0.7

    def __init__(self):
        super().__init__()
        self._api_code = os.getenv("REPEC_API_CODE", "")
        self._warned_no_code = False

    def fields_provided(self):
        return ["recent_publications"]

    def fetch(self, faculty_dict):
        """Search RePEc for publications by this faculty member."""
        if not self._api_code:
            if not self._warned_no_code:
                logger.debug("REPEC_API_CODE not set; skipping RePEc source")
                self._warned_no_code = True
            return None

        first = (faculty_dict.get("first_name") or "").strip()
        last = (faculty_dict.get("last_name") or "").strip()
        if not first or not last:
            return None

        try:
            shortid = self._lookup_shortid(first, last)
            if not shortid:
                return None

            publications = self._fetch_publications(shortid)
        except Exception:
            # The API shape is not well specified; never let a surprise
            # payload crash the pipeline.
            logger.warning("Unexpected RePEc response for %s %s",
                           first, last, exc_info=True)
            return None

        if not publications:
            return None

        return {
            "recent_publications": publications,
            "_source_url": f"https://ideas.repec.org/e/{shortid}.html",
        }

    def _lookup_shortid(self, first, last):
        """Resolve the RePEc author short-id; None unless exactly one match."""
        resp = self._get(API_URL, params={
            "code": self._api_code,
            "getauthorshortid": f"{first} {last}",
        })
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.debug("Non-JSON RePEc short-id response for %s %s",
                         first, last)
            return None

        # Responses have been observed both as a bare list and as a dict;
        # normalize to a list of candidate short-id strings.
        candidates = []
        if isinstance(data, dict):
            data = data.get("shortid") or data.get("result") or data
            if isinstance(data, str):
                candidates = [data]
            elif isinstance(data, list):
                candidates = data
        elif isinstance(data, list):
            candidates = data
        elif isinstance(data, str):
            candidates = [data]

        shortids = []
        for item in candidates:
            if isinstance(item, str) and item.strip():
                shortids.append(item.strip())
            elif isinstance(item, dict):
                sid = item.get("shortid") or item.get("short-id") or ""
                if isinstance(sid, str) and sid.strip():
                    shortids.append(sid.strip())

        shortids = sorted(set(shortids))
        if len(shortids) != 1:
            if len(shortids) > 1:
                logger.info("RePEc: %d short-ids for %s %s; skipping",
                            len(shortids), first, last)
            return None
        return shortids[0]

    def _fetch_publications(self, shortid):
        """Fetch the full author record and extract publications."""
        resp = self._get(API_URL, params={
            "code": self._api_code,
            "getauthorrecordfull": shortid,
        })
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.debug("Non-JSON RePEc author record for %s", shortid)
            return None

        # The record may be a dict with an item list, or a bare list of items.
        items = []
        if isinstance(data, dict):
            for key in ("items", "publications", "article", "paper", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    items.extend(value)
        elif isinstance(data, list):
            items = data

        publications = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pub = {}

            title = item.get("title") or item.get("name")
            if isinstance(title, str) and title.strip():
                pub["title"] = title.strip()

            year = item.get("year") or item.get("creationdate")
            if year:
                try:
                    pub["year"] = int(str(year)[:4])
                except (TypeError, ValueError):
                    pass

            journal = item.get("journal") or item.get("series")
            if isinstance(journal, str) and journal.strip():
                pub["journal"] = journal.strip()

            if pub.get("title"):
                publications.append(pub)
            if len(publications) >= MAX_PUBLICATIONS:
                break

        return publications or None
