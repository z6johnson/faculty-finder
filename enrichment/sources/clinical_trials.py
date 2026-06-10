"""ClinicalTrials.gov API v2 client.

Queries the public ClinicalTrials.gov v2 API to find clinical trials where
a faculty member is listed as an overall official (PI / study chair / study
director) with a UCSD affiliation. No API key required.

Docs: https://clinicaltrials.gov/data-api/api
Used primarily by health-sciences divisions (medicine, public health,
pharmacy), but safe to run for any division.
"""

import logging

from .base import BaseSource

logger = logging.getLogger(__name__)

API_BASE = "https://clinicaltrials.gov/api/v2/studies"

UCSD_AFFILIATION_KEYWORDS = [
    "university of california, san diego",
    "uc san diego",
    "ucsd",
]

FIELDS = ",".join([
    "protocolSection.identificationModule",
    "protocolSection.statusModule",
    "protocolSection.contactsLocationsModule",
    "protocolSection.sponsorCollaboratorsModule",
])

MAX_GRANTS = 10


class ClinicalTrialsSource(BaseSource):
    source_name = "clinical_trials"
    min_request_interval = 1.0
    confidence = 0.8  # federal registry; identity confirmed via affiliation

    def fields_provided(self):
        return ["funded_grants"]

    def fetch(self, faculty_dict):
        """Search ClinicalTrials.gov for trials led by this faculty member."""
        first = (faculty_dict.get("first_name") or "").strip()
        last = (faculty_dict.get("last_name") or "").strip()
        if not first or not last:
            return None

        params = {
            "query.term": f"{first} {last}",
            "format": "json",
            "pageSize": 20,
            "fields": FIELDS,
        }

        resp = self._get(API_BASE, params=params)
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Invalid JSON from ClinicalTrials.gov for %s %s",
                           first, last)
            return None

        studies = data.get("studies") or []
        if not studies:
            return None

        grants = []
        for study in studies:
            grant = self._extract_grant(study, first, last)
            if grant:
                grants.append(grant)
            if len(grants) >= MAX_GRANTS:
                break

        if not grants:
            return None

        return {
            "funded_grants": grants,
            "_source_url": f"{API_BASE}?query.term={first}+{last}",
        }

    def _extract_grant(self, study, first, last):
        """Return a grant dict if this study's official matches our person.

        Requires both a name match (case-insensitive last name + first-name
        prefix) and a UCSD-ish affiliation string on the same official entry,
        to avoid attaching trials run by same-named investigators elsewhere.
        """
        protocol = study.get("protocolSection") or {}
        contacts = protocol.get("contactsLocationsModule") or {}
        officials = contacts.get("overallOfficials") or []

        if not self._matches_official(officials, first, last):
            return None

        ident = protocol.get("identificationModule") or {}
        status = protocol.get("statusModule") or {}

        grant = {
            "title": (ident.get("briefTitle") or "").strip(),
            "agency": "ClinicalTrials.gov",
            "type": "clinical_trial",
            "nct_id": ident.get("nctId", ""),
            "status": status.get("overallStatus", ""),
        }
        if not grant["title"]:
            return None

        start = (status.get("startDateStruct") or {}).get("date", "")
        if start:
            # Dates look like "2021-03" or "2021-03-15"; keep the year
            grant["start_date"] = start[:4]

        return grant

    def _matches_official(self, officials, first, last):
        """Check officials for a name + UCSD-affiliation match."""
        first_lower = first.lower()
        last_lower = last.lower()

        for official in officials:
            name = (official.get("name") or "").lower()
            if last_lower not in name:
                continue
            # Require a first-name token match (prefix handles "J." or
            # "Jon" style abbreviations of "Jonathan")
            tokens = [t.strip(".,") for t in name.split()]
            if not any(t and (t.startswith(first_lower) or first_lower.startswith(t))
                       for t in tokens if t != last_lower):
                continue

            affiliation = (official.get("affiliation") or "").lower()
            if any(kw in affiliation for kw in UCSD_AFFILIATION_KEYWORDS):
                return True

        return False
