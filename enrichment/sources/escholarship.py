"""eScholarship (UC open-access repository) adapter.

eScholarship (https://escholarship.org) holds the open-access output of every
UC campus and is the best free coverage of UCSD humanities and social-science
scholarship. It has no author-search API, only a bulk OAI-PMH endpoint
(https://escholarship.org/oai), so this source works in two parts:

  1. A bulk harvest job (enrichment/escholarship_harvest.py, job kind
     'escholarship_harvest') pulls oai_dc records into the local
     ``escholarship_pubs`` lookup table, keyed by normalized author name.
  2. This adapter reads that table at enrichment time — no network calls.

Until a harvest has run the table is empty and this source returns None.
Used by the arts-hum bundle (and as a supplement wherever configured).
"""

import logging

from utils.names import normalize_name

from .base import BaseSource

logger = logging.getLogger(__name__)


def author_key(first_name, last_name):
    """Lookup key used by both the harvester and the adapter."""
    return f"{normalize_name(last_name)}|{normalize_name(first_name)}"


class EScholarshipSource(BaseSource):
    source_name = "escholarship"
    min_request_interval = 0.0  # local DB lookup, no network
    confidence = 0.75

    def fields_provided(self):
        return ["recent_publications"]

    def fetch(self, faculty_dict):
        first = faculty_dict.get("first_name", "")
        last = faculty_dict.get("last_name", "")
        if not first or not last:
            return None

        from data import db
        try:
            conn = db.get_read_conn()
            rows = conn.execute(
                "SELECT title, year, journal, doi, source_url"
                " FROM escholarship_pubs WHERE author_norm = ?"
                " ORDER BY year DESC LIMIT 15",
                (author_key(first, last),),
            ).fetchall()
        except Exception:
            logger.debug("eScholarship lookup table unavailable — skipping")
            return None
        if not rows:
            return None

        pubs = []
        for row in rows:
            pub = {"title": row["title"]}
            if row["year"]:
                pub["year"] = row["year"]
            if row["journal"]:
                pub["journal"] = row["journal"]
            if row["doi"]:
                pub["doi"] = row["doi"]
            pubs.append(pub)
        return {
            "recent_publications": pubs,
            "_source_url": rows[0]["source_url"] or "https://escholarship.org",
        }
