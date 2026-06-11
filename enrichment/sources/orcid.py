"""ORCID public API client.

Queries the public ORCID API to find researcher profiles and extract
works, employment, and funding data. No authentication required for
public records.

Search strategy (in priority order):
  1. Use existing ORCID ID if already on the record (validate affiliation)
  2. Search by email (most reliable disambiguation)
  3. Search by name + UCSD affiliation
  4. (No broad name-only fallback — too many false matches)

All search results are validated against UCSD employment history before
accepting to prevent wrong-person contamination.

API docs: https://info.orcid.org/documentation/api-tutorials/
"""

import logging

from utils.names import name_similarity

from .base import BaseSource

logger = logging.getLogger(__name__)

SEARCH_URL = "https://pub.orcid.org/v3.0/search/"
RECORD_URL = "https://pub.orcid.org/v3.0/{orcid_id}"

# Affiliation strings that indicate UCSD employment.  Checked as
# case-insensitive substrings against the organization name in the
# ORCID employment / education sections.
UCSD_AFFILIATION_STRINGS = [
    "university of california san diego",
    "university of california, san diego",
    "uc san diego",
    "ucsd",
    "scripps institution of oceanography",
    "scripps research",
]

# Employment-grade subset used by the identity auto-accept rules. Excludes
# "scripps research": The Scripps Research Institute is a separate
# institution from UCSD (only Scripps Institution of Oceanography is ours).
UCSD_EMPLOYMENT_STRINGS = [
    s for s in UCSD_AFFILIATION_STRINGS if s != "scripps research"
]


class ORCIDSource(BaseSource):
    source_name = "orcid"
    min_request_interval = 1.0
    confidence = 0.9  # self-reported by researcher

    def __init__(self):
        super().__init__()
        self._session.headers.update({
            "Accept": "application/json",
        })

    def fields_provided(self):
        return ["orcid", "recent_publications", "funded_grants", "email", "awards"]

    def fetch(self, faculty_dict):
        """Search ORCID for this faculty member and extract their record."""
        first = faculty_dict.get("first_name", "")
        last = faculty_dict.get("last_name", "")
        email = (faculty_dict.get("email") or "").strip().lower()

        # If we already have their ORCID ID, go directly to the record
        # but still validate affiliation to catch stale/wrong IDs.
        existing_orcid = faculty_dict.get("orcid")
        if existing_orcid:
            record = self._fetch_full_record(existing_orcid)
            if record and self._has_ucsd_affiliation(record):
                return self._extract_data(record, existing_orcid, first, last)
            elif record:
                logger.info(
                    "Existing ORCID %s for %s %s has no UCSD affiliation — skipping",
                    existing_orcid, first, last,
                )
                # Fall through to search — the stored ORCID may be wrong

        # Strategy 1: Search by email (most reliable)
        if email:
            orcid_id = self._search_by_email(email)
            if orcid_id:
                record = self._fetch_full_record(orcid_id)
                if record:
                    return self._extract_data(record, orcid_id, first, last)

        # Strategy 2: Search by name + UCSD affiliation
        orcid_id = self._search_by_name(first, last)
        if orcid_id:
            record = self._fetch_full_record(orcid_id)
            if record and self._has_ucsd_affiliation(record):
                return self._extract_data(record, orcid_id, first, last)
            elif record:
                logger.info(
                    "ORCID %s matched name %s %s but has no UCSD affiliation — skipping",
                    orcid_id, first, last,
                )

        return None

    def _search_by_email(self, email):
        """Search ORCID by email address — best disambiguation signal."""
        query = f"email:{email}"
        resp = self._get(SEARCH_URL, params={"q": query, "rows": 1})
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        results = data.get("result") or []
        if not results:
            return None

        orcid_id = results[0].get("orcid-identifier", {}).get("path")
        if orcid_id:
            logger.info("Found ORCID %s via email search for %s", orcid_id, email)
        return orcid_id

    def _search_by_name(self, first_name, last_name):
        """Search ORCID by name + UCSD affiliation. Returns best match."""
        query = (
            f'given-names:{first_name} AND family-name:{last_name} '
            f'AND affiliation-org-name:"University of California San Diego"'
        )

        resp = self._get(SEARCH_URL, params={"q": query, "rows": 5})
        if not resp:
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        results = data.get("result") or []
        if not results:
            return None

        # If only one result, return it (affiliation validation happens in caller)
        if len(results) == 1:
            return results[0].get("orcid-identifier", {}).get("path")

        # Multiple results — return the first one; caller will validate
        # affiliation on the full record.  We do NOT fall back to a broad
        # name-only search, which caused wrong-person contamination.
        return results[0].get("orcid-identifier", {}).get("path")

    @staticmethod
    def _has_ucsd_affiliation(record):
        """Check if the ORCID record has UCSD in employment or education."""
        activities = record.get("activities-summary", {})

        # Check employments
        for group in activities.get("employments", {}).get("affiliation-group", []):
            for summary in group.get("summaries", []):
                emp = summary.get("employment-summary", {})
                org_name = (emp.get("organization", {}).get("name") or "").lower()
                for ucsd_str in UCSD_AFFILIATION_STRINGS:
                    if ucsd_str in org_name:
                        return True

        # Check educations as fallback (some faculty list UCSD only there)
        for group in activities.get("educations", {}).get("affiliation-group", []):
            for summary in group.get("summaries", []):
                edu = summary.get("education-summary", {})
                org_name = (edu.get("organization", {}).get("name") or "").lower()
                for ucsd_str in UCSD_AFFILIATION_STRINGS:
                    if ucsd_str in org_name:
                        return True

        return False

    @staticmethod
    def _has_ucsd_employment(record):
        """Strict check: UCSD appears in the *employments* section.

        Unlike _has_ucsd_affiliation this never falls back to education (a
        UCSD degree does not make someone UCSD faculty) and uses the
        employment-grade string list. Identity auto-accept rules depend on
        this distinction; do not loosen it without revisiting them.
        """
        activities = record.get("activities-summary", {})
        for group in activities.get("employments", {}).get("affiliation-group", []):
            for summary in group.get("summaries", []):
                emp = summary.get("employment-summary", {})
                org_name = (emp.get("organization", {}).get("name") or "").lower()
                for ucsd_str in UCSD_EMPLOYMENT_STRINGS:
                    if ucsd_str in org_name:
                        return True
        return False

    @staticmethod
    def employment_affiliations(record):
        """Org names (with year span when available) from the record's
        *employment* section — the affiliation history shown to the identity
        adjudication LLM. Education is intentionally excluded (a degree is not
        employment), mirroring _has_ucsd_employment.
        """
        activities = record.get("activities-summary", {})
        out = []
        for group in activities.get("employments", {}).get(
                "affiliation-group", []):
            for summary in group.get("summaries", []):
                emp = summary.get("employment-summary", {})
                org = (emp.get("organization", {}).get("name") or "").strip()
                if not org:
                    continue
                label = org
                role = (emp.get("role-title") or "").strip()
                if role:
                    label = f"{role}, {org}"
                start = emp.get("start-date") or {}
                end = emp.get("end-date") or {}
                sy = (start.get("year") or {}).get("value") if start else None
                ey = (end.get("year") or {}).get("value") if end else None
                if sy or ey:
                    label += f" ({sy or '?'}–{ey or 'present'})"
                out.append(label)
        return out

    @staticmethod
    def _record_name(record):
        """(given, family) from the record's own person.name section."""
        name = (record.get("person") or {}).get("name") or {}
        given = ((name.get("given-names") or {}).get("value") or "").strip()
        family = ((name.get("family-name") or {}).get("value") or "").strip()
        return given, family

    def search_by_name_counted(self, first_name, last_name):
        """Like _search_by_name but also returns how many profiles matched,
        so callers can require a unique hit before trusting the match."""
        query = (
            f'given-names:{first_name} AND family-name:{last_name} '
            f'AND affiliation-org-name:"University of California San Diego"'
        )
        resp = self._get(SEARCH_URL, params={"q": query, "rows": 5})
        if not resp:
            return None, 0
        try:
            data = resp.json()
        except ValueError:
            return None, 0
        results = data.get("result") or []
        if not results:
            return None, 0
        return results[0].get("orcid-identifier", {}).get("path"), len(results)

    def verify_ucsd_employment(self, orcid_id, first_name, last_name, email=None):
        """Fetch one ORCID record and verify it at employment grade.

        The single network primitive behind the identity auto-accept rules.
        Returns None when the record cannot be fetched (callers fail closed),
        otherwise the dict described by _verify_record.
        """
        record = self._fetch_full_record(orcid_id)
        if not record:
            return None
        return self._verify_record(record, orcid_id, first_name, last_name,
                                   email=email)

    @classmethod
    def _verify_record(cls, record, orcid_id, first_name, last_name, email=None):
        """Verification facts for an already-fetched record: employment-only
        UCSD check, the record's *own* name similarity to the faculty (the
        search query matching is not enough — wrong records come back), and
        whether the record exposes the faculty's exact email."""
        given, family = cls._record_name(record)
        sim = 0.0
        if given and family:
            sim = name_similarity(first_name, last_name, given, family)
            # Given-names often carries middle names ("Jane Marie"); the
            # first token is the comparable part.
            sim = max(sim, name_similarity(first_name, last_name,
                                           given.split()[0], family))
        record_email = cls._extract_email(record, first_name, last_name)
        email_match = bool(email and record_email
                           and record_email == email.strip().lower())
        return {
            "orcid": orcid_id,
            "employment_verified": cls._has_ucsd_employment(record),
            "record_name_similarity": sim,
            "record_email_match": email_match,
        }

    def _fetch_full_record(self, orcid_id):
        """Fetch the full ORCID record JSON."""
        url = RECORD_URL.format(orcid_id=orcid_id)
        resp = self._get(url)
        if not resp:
            return None

        try:
            return resp.json()
        except ValueError:
            return None

    def _extract_data(self, record, orcid_id, first_name, last_name):
        """Extract structured data from a validated ORCID record."""
        result = {
            "orcid": orcid_id,
            "_source_url": f"https://orcid.org/{orcid_id}",
        }

        # Extract email from ORCID person record
        email = self._extract_email(record, first_name, last_name)
        if email:
            result["email"] = email

        # Extract works (publications)
        works = self._extract_works(record)
        if works:
            result["recent_publications"] = works

        # Extract fundings (grants)
        fundings = self._extract_fundings(record)
        if fundings:
            result["funded_grants"] = fundings

        # Extract distinctions (honors/awards)
        awards = self._extract_distinctions(record)
        if awards:
            result["awards"] = awards

        # Provide works_count for the normalizer
        works_section = (
            record.get("activities-summary", {})
            .get("works", {})
            .get("group", [])
        )
        result["works_count"] = len(works_section)

        # Extract recent work titles for normalizer
        recent_works = []
        for group in works_section[:10]:
            summaries = group.get("work-summary", [])
            if summaries:
                title_obj = summaries[0].get("title", {})
                title_val = title_obj.get("title", {}).get("value", "")
                if title_val:
                    recent_works.append(title_val)
        if recent_works:
            result["recent_works"] = recent_works

        return result if len(result) > 2 else None  # More than just orcid + _source_url

    @staticmethod
    def _extract_email(record, first_name, last_name):
        """Extract a ucsd.edu email from the ORCID person record.

        ORCID profiles may list one or more email addresses under
        person -> emails -> email.  We prefer @ucsd.edu addresses but
        accept @eng.ucsd.edu and other sub-domains.
        """
        emails_section = (
            record.get("person", {})
            .get("emails", {})
            .get("email", [])
        )
        ucsd_emails = []
        for entry in emails_section:
            addr = entry.get("email", "").strip().lower()
            if addr and "ucsd.edu" in addr:
                ucsd_emails.append(addr)

        if not ucsd_emails:
            return None

        # If multiple, prefer one that contains part of the person's name
        first = first_name.lower()
        last = last_name.lower()
        for addr in ucsd_emails:
            local = addr.split("@")[0]
            if (last and last[:3] in local) or (first and first[:3] in local):
                return addr

        # Fall back to first ucsd.edu address found
        return ucsd_emails[0]

    def _extract_works(self, record):
        """Extract recent publications from ORCID record."""
        works_groups = (
            record.get("activities-summary", {})
            .get("works", {})
            .get("group", [])
        )

        publications = []
        for group in works_groups[:20]:  # Most recent 20
            summaries = group.get("work-summary", [])
            if not summaries:
                continue
            summary = summaries[0]

            pub = {}
            title_obj = summary.get("title", {})
            title_val = title_obj.get("title", {}).get("value", "")
            if title_val:
                pub["title"] = title_val

            # Year
            pub_date = summary.get("publication-date") or {}
            year_val = pub_date.get("year", {})
            if isinstance(year_val, dict) and year_val.get("value"):
                try:
                    pub["year"] = int(year_val["value"])
                except (ValueError, TypeError):
                    pass

            # Journal
            journal = summary.get("journal-title")
            if journal and isinstance(journal, dict):
                pub["journal"] = journal.get("value", "")
            elif isinstance(journal, str):
                pub["journal"] = journal

            if pub.get("title"):
                publications.append(pub)

        return publications or None

    @staticmethod
    def _extract_distinctions(record):
        """Extract honors/awards from the ORCID distinctions section."""
        groups = (
            record.get("activities-summary", {})
            .get("distinctions", {})
            .get("affiliation-group", [])
        )

        awards = []
        for group in groups[:20]:
            for summary in group.get("summaries", []):
                dist = summary.get("distinction-summary", {})
                name = (dist.get("role-title") or "").strip()
                if not name:
                    continue
                award = {"name": name, "source": "orcid"}
                org = (dist.get("organization") or {}).get("name")
                if org:
                    award["granting_org"] = org
                start = dist.get("start-date") or {}
                year = (start.get("year") or {}).get("value") if start else None
                if year:
                    try:
                        award["year"] = int(year)
                    except (ValueError, TypeError):
                        pass
                awards.append(award)

        return awards or None

    def _extract_fundings(self, record):
        """Extract funding/grants from ORCID record."""
        funding_groups = (
            record.get("activities-summary", {})
            .get("fundings", {})
            .get("group", [])
        )

        grants = []
        for group in funding_groups[:15]:
            summaries = group.get("funding-summary", [])
            if not summaries:
                continue
            summary = summaries[0]

            grant = {}
            title_obj = summary.get("title", {})
            title_val = title_obj.get("title", {}).get("value", "")
            if title_val:
                grant["title"] = title_val

            org = summary.get("organization", {})
            if org.get("name"):
                grant["agency"] = org["name"]

            # Dates
            start = summary.get("start-date") or {}
            if start.get("year", {}).get("value"):
                grant["start_date"] = start["year"]["value"]

            end = summary.get("end-date") or {}
            if end and end.get("year", {}).get("value"):
                grant["end_date"] = end["year"]["value"]

            if grant.get("title"):
                grants.append(grant)

        return grants or None
