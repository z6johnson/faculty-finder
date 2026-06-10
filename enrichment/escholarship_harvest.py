"""Bulk OAI-PMH harvest of eScholarship into the local lookup table.

eScholarship exposes no author search, so we harvest oai_dc records from
https://escholarship.org/oai into ``escholarship_pubs`` (keyed by normalized
author name); the escholarship source adapter then resolves faculty against
that table locally. Run as the 'escholarship_harvest' job kind.

Environment:
    ESCHOLARSHIP_SET        OAI set to harvest (default "ucsd" — the UCSD
                            campus set; adjust if the repository renames it).
    ESCHOLARSHIP_FROM_YEARS How many years back to harvest (default 6).
"""

import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from data import db
from enrichment.sources.escholarship import author_key
from utils.names import parse_eah_name

logger = logging.getLogger(__name__)

OAI_URL = "https://escholarship.org/oai"

_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

REQUEST_INTERVAL = 2.0  # be gentle with the repository


def _request(session, params):
    try:
        resp = session.get(OAI_URL, params=params, timeout=60)
        resp.raise_for_status()
        return ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as e:
        logger.warning("eScholarship OAI request failed: %s", e)
        return None


def _parse_record(record):
    """Extract (creators, title, year, journal, doi, url) from an oai_dc record."""
    meta = record.find(".//oai_dc:dc", _NS)
    if meta is None:
        return None
    title_el = meta.find("dc:title", _NS)
    title = (title_el.text or "").strip() if title_el is not None else ""
    if not title:
        return None

    creators = [(c.text or "").strip() for c in meta.findall("dc:creator", _NS)
                if c.text and c.text.strip()]
    if not creators:
        return None

    year = None
    for d in meta.findall("dc:date", _NS):
        text = (d.text or "").strip()
        if len(text) >= 4 and text[:4].isdigit():
            year = int(text[:4])
            break

    journal = None
    for s in meta.findall("dc:source", _NS):
        if s.text and s.text.strip():
            journal = s.text.strip()
            break

    doi = None
    url = None
    for ident in meta.findall("dc:identifier", _NS):
        text = (ident.text or "").strip()
        if "doi.org/" in text and not doi:
            doi = text.split("doi.org/", 1)[1]
        elif text.startswith("http") and not url:
            url = text

    return creators, title, year, journal, doi, url


def run_harvest(time_budget_seconds=None, progress_callback=None):
    """Harvest records into escholarship_pubs. Returns a stats dict."""
    oai_set = os.environ.get("ESCHOLARSHIP_SET", "ucsd").strip()
    years_back = int(os.environ.get("ESCHOLARSHIP_FROM_YEARS", "6"))
    from_date = (datetime.now(timezone.utc) - timedelta(days=365 * years_back))

    session = requests.Session()
    session.headers["User-Agent"] = ("UCSD-GrantMatch/1.0 (academic research "
                                     "tool; contact: hwsph-grants@ucsd.edu)")

    conn = db.connect(readonly=False)
    start = time.monotonic()
    harvested = inserted = batches = 0
    now = datetime.now(timezone.utc).isoformat()
    params = {
        "verb": "ListRecords",
        "metadataPrefix": "oai_dc",
        "from": from_date.strftime("%Y-%m-%d"),
    }
    if oai_set:
        params["set"] = oai_set

    try:
        while True:
            if time_budget_seconds is not None:
                if time.monotonic() - start > time_budget_seconds - 60:
                    logger.warning("eScholarship harvest: time budget reached "
                                   "after %d records", harvested)
                    break

            root = _request(session, params)
            if root is None:
                break
            error = root.find("oai:error", _NS)
            if error is not None:
                logger.warning("eScholarship OAI error (%s): %s",
                               error.get("code"), error.text)
                break

            for record in root.findall(".//oai:record", _NS):
                parsed = _parse_record(record)
                if not parsed:
                    continue
                creators, title, year, journal, doi, url = parsed
                harvested += 1
                for creator in creators[:25]:
                    first, last = parse_eah_name(creator)  # "Last, First" form
                    key = author_key(first, last)
                    if not key or key == "|":
                        continue
                    cur = conn.execute(
                        "INSERT INTO escholarship_pubs"
                        " (author_norm, title, year, journal, doi, source_url, harvested_at)"
                        " SELECT ?, ?, ?, ?, ?, ?, ?"
                        " WHERE NOT EXISTS (SELECT 1 FROM escholarship_pubs"
                        "  WHERE author_norm = ? AND title = ?)",
                        (key, title, year, journal, doi, url, now, key, title),
                    )
                    inserted += cur.rowcount

            batches += 1
            conn.commit()
            if progress_callback:
                progress_callback(harvested, batches)

            token_el = root.find(".//oai:resumptionToken", _NS)
            token = (token_el.text or "").strip() if token_el is not None else ""
            if not token:
                break
            params = {"verb": "ListRecords", "resumptionToken": token}
            time.sleep(REQUEST_INTERVAL)
    finally:
        conn.commit()
        conn.close()

    logger.info("eScholarship harvest done: %d records seen, %d rows inserted "
                "(%d batches)", harvested, inserted, batches)
    return {"records_seen": harvested, "rows_inserted": inserted,
            "batches": batches, "set": oai_set}
