"""Wikidata SPARQL client.

Looks up a faculty member on Wikidata by ORCID iD (property P496) and
extracts awards received (P166, with point-in-time qualifier P585) and
memberships (P463). No API key required.

Docs: https://www.wikidata.org/wiki/Wikidata:SPARQL_query_service
Safe to run for any division; only runs when the faculty record already
has a stored ORCID, so identity is unambiguous.
"""

import logging

from .base import BaseSource

logger = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

QUERY_TEMPLATE = """
SELECT DISTINCT ?person ?awardLabel ?awardDate ?orgLabel WHERE {{
  ?person wdt:P496 "{orcid}" .
  OPTIONAL {{
    ?person p:P166 ?awardStmt .
    ?awardStmt ps:P166 ?award .
    ?award rdfs:label ?awardLabel .
    FILTER(LANG(?awardLabel) = "en")
    OPTIONAL {{ ?awardStmt pq:P585 ?awardDate . }}
  }}
  OPTIONAL {{
    ?person wdt:P463 ?org .
    ?org rdfs:label ?orgLabel .
    FILTER(LANG(?orgLabel) = "en")
  }}
}}
LIMIT 100
"""

MAX_AWARDS = 20


class WikidataSource(BaseSource):
    source_name = "wikidata"
    min_request_interval = 1.0
    confidence = 0.8  # ORCID-keyed lookup; identity is unambiguous

    def fields_provided(self):
        return ["awards"]

    def fetch(self, faculty_dict):
        """Look up awards and memberships on Wikidata via stored ORCID."""
        orcid = (faculty_dict.get("orcid") or "").strip()
        if not orcid:
            return None
        # Tolerate stored ORCID URLs ("https://orcid.org/0000-...")
        orcid = orcid.rstrip("/").rsplit("/", 1)[-1]

        query = QUERY_TEMPLATE.format(orcid=orcid)
        resp = self._get(
            SPARQL_ENDPOINT,
            params={"query": query, "format": "json"},
            headers={"Accept": "application/sparql-results+json"},
        )
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON from Wikidata SPARQL for ORCID %s",
                           orcid)
            return None

        bindings = ((data.get("results") or {}).get("bindings")) or []
        if not bindings:
            return None

        qid = self._extract_qid(bindings)
        awards = self._extract_awards(bindings)
        if not qid or not awards:
            return None

        return {
            "awards": awards[:MAX_AWARDS],
            "_source_url": f"https://www.wikidata.org/wiki/{qid}",
        }

    def _extract_qid(self, bindings):
        """Pull the person's QID from the first binding's entity URI."""
        for binding in bindings:
            person = (binding.get("person") or {}).get("value", "")
            if "/entity/" in person:
                return person.rsplit("/", 1)[-1]
        return None

    def _extract_awards(self, bindings):
        """Build deduplicated award entries from SPARQL bindings."""
        awards = []
        seen = set()

        for binding in bindings:
            # Award statements
            label = (binding.get("awardLabel") or {}).get("value", "").strip()
            if label:
                entry = {"name": label, "source": "wikidata"}
                date = (binding.get("awardDate") or {}).get("value", "")
                if date:
                    try:
                        entry["year"] = int(date[:4])
                    except ValueError:
                        pass
                key = (entry["name"], entry.get("year"))
                if key not in seen:
                    seen.add(key)
                    awards.append(entry)

            # Memberships, expressed as award-like entries
            org = (binding.get("orgLabel") or {}).get("value", "").strip()
            if org:
                entry = {"name": f"Member, {org}", "source": "wikidata"}
                key = (entry["name"], None)
                if key not in seen:
                    seen.add(key)
                    awards.append(entry)

        return awards
