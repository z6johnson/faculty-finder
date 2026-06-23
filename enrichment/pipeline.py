"""Enrichment pipeline orchestrator.

Coordinates fetching data from multiple sources, normalizing it with an LLM,
and writing enriched records back to SQLite (the source of truth — the old
JSON-file authoring path is retired; scripts/export_db_to_json.py produces
JSON snapshots for provenance).

Per-record flow:
  Phase 1  fetch from every routed source concurrently (shared, rate-limited
           source instances)
  Phase 2  write direct fields (write-once unless refreshable) and merge JSON
           list fields across sources with per-field dedupe — higher-confidence
           sources win on conflicts
  Phase 3  LLM normalization, skipped when the raw-context fingerprint
           (faculty.raw_hash) is unchanged from the previous run
"""

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import unquote

from data import db

from .normalizer import build_context, normalize_from_context
from .routing import all_source_classes, source_classes_for

logger = logging.getLogger(__name__)

# Fields that can be directly written to a faculty record (non-JSON)
DIRECT_FIELDS = {"profile_url", "orcid", "openalex_id", "google_scholar_id",
                 "h_index", "citation_count", "works_count", "email"}

# Direct fields that should be refreshed on every run (not write-once)
REFRESHABLE_FIELDS = {"h_index", "citation_count", "works_count"}

# JSON list fields, merged across sources with per-field dedupe keys.
JSON_FIELDS = {"funded_grants", "recent_publications", "expertise_keywords",
               "awards", "patents"}

# Per-field caps after merging.
_JSON_FIELD_CAPS = {
    "funded_grants": 25,
    "recent_publications": 20,
    "expertise_keywords": 30,
    "awards": 20,
    "patents": 20,
}


class SourcePool:
    """One instance per source class per run, so rate-limit state holds
    across faculty instead of resetting on every fetch."""

    def __init__(self):
        self._instances = {}

    def registry_for(self, department):
        return {name: self._get(cls)
                for name, cls in source_classes_for(department).items()}

    def _get(self, cls):
        if cls not in self._instances:
            self._instances[cls] = cls()
        return self._instances[cls]


def _load_faculty(department=None):
    """Load faculty for a division from the DB (pipeline-shaped dict)."""
    return db.fetch_for_enrichment(db.get_write_conn(), department)


def _make_log_entry(faculty_dict, source_name, field, old_value, new_value,
                    confidence, method, source_url=None, raw_response=None):
    """Create an enrichment_log entry dict (column-name keyed)."""
    # The LLM normalizer's context is stored verbatim and IN FULL (no JSON
    # wrapping, no 5000-char truncation) so any enriched value is traceable to
    # exactly what the model saw. These rows are also exempt from log rotation
    # (see db.rotate_log) so provenance for the current enriched value survives.
    if method in ("llm_extraction", "no_context"):
        raw = raw_response if (raw_response is None or isinstance(raw_response, str)) \
            else json.dumps(raw_response)
    else:
        raw = (json.dumps(raw_response)[:5000]) if raw_response else None
    return {
        "faculty_id": faculty_dict.get("_db_id"),
        "stable_key": faculty_dict.get("_stable_key"),
        "source_name": source_name,
        "source_url": source_url,
        "field_updated": field,
        "old_value": json.dumps(old_value) if isinstance(old_value, (list, dict)) else str(old_value),
        "new_value": json.dumps(new_value) if isinstance(new_value, (list, dict)) else str(new_value),
        "confidence": confidence,
        "method": method,
        "raw_response": raw,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_source(source_name, source, faculty_dict, name):
    """Fetch data from a single source instance. Designed to run in a thread."""
    try:
        result = source.fetch(faculty_dict)
    except Exception:
        logger.exception("Source %s failed for %s", source_name, name)
        result = None
    return source_name, result


# ---------------------------------------------------------------------------
# JSON-field merging
# ---------------------------------------------------------------------------

def _norm_title(value):
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _merge_key(field, item):
    """Dedupe key for one entry of a JSON list field."""
    if field == "recent_publications":
        if isinstance(item, dict):
            return ("doi:" + item["doi"].lower()) if item.get("doi") \
                else "title:" + _norm_title(item.get("title"))
        return "title:" + _norm_title(item)
    if field == "funded_grants":
        if isinstance(item, dict):
            return _norm_title(item.get("title")) or _norm_title(item.get("agency"))
        return _norm_title(item)
    if field == "patents":
        if isinstance(item, dict) and item.get("patent_number"):
            return str(item["patent_number"])
        return _norm_title(item.get("title") if isinstance(item, dict) else item)
    if field == "awards":
        if isinstance(item, dict):
            return _norm_title(item.get("name")) + str(item.get("year") or "")
        return _norm_title(item)
    # expertise_keywords and other simple lists
    return _norm_title(item if isinstance(item, str) else json.dumps(item))


def _merge_json_field(field, contributions):
    """Merge per-source lists (already ordered by descending confidence).

    Higher-confidence entries win on key collisions; output keeps the order
    contributions arrived in and is capped per field.
    """
    merged = []
    seen = set()
    cap = _JSON_FIELD_CAPS.get(field, 20)
    for items in contributions:
        for item in items or []:
            key = _merge_key(field, item)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    if field == "recent_publications":
        merged.sort(key=lambda p: -(p.get("year") or 0) if isinstance(p, dict) else 0)
    return merged[:cap]


def _raw_fingerprint(context):
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-faculty enrichment
# ---------------------------------------------------------------------------

def enrich_faculty(faculty_index, sources=None, dry_run=False, department=None,
                   _data=None, _pool=None):
    """Enrich a single faculty member from the routed sources.

    Args:
        faculty_index: Index of the faculty member in the loaded faculty list.
        sources: List of source names to use, or None for all routed sources.
        dry_run: If True, fetch data but don't write.
        department: Division slug (selects the source bundle).
        _data: Pre-loaded faculty data dict (used by enrich_all/backfill to
               avoid repeated DB reads). When provided, the caller is
               responsible for saving.
        _pool: Shared SourcePool (created if absent).

    Returns:
        (summary dict, list of enrichment_log entry dicts)
    """
    pool = _pool or SourcePool()
    caller_owns_data = _data is not None
    data = _data if caller_owns_data else _load_faculty(department)
    faculty_list = data["faculty"]

    if faculty_index < 0 or faculty_index >= len(faculty_list):
        logger.error("Faculty index %d out of range (0-%d).", faculty_index, len(faculty_list) - 1)
        return {"error": f"Faculty index {faculty_index} out of range"}, []

    faculty_dict = faculty_list[faculty_index]
    record_dept = faculty_dict.get("department") or department
    registry = pool.registry_for(record_dept)
    name = f"{faculty_dict.get('first_name', '')} {faculty_dict.get('last_name', '')}"
    logger.info("Enriching: %s (index: %d, dept: %s)", name, faculty_index, record_dept)

    source_names = sources or list(registry.keys())
    valid_sources = [s for s in source_names if s in registry]
    for s in source_names:
        if s not in registry:
            logger.warning("Unknown source for dept %s: %s", record_dept, s)

    raw_data = {}
    summary = {"faculty_index": faculty_index, "name": name, "sources": {}}

    # Phase 1: Fetch from all sources concurrently
    with ThreadPoolExecutor(max_workers=max(len(valid_sources), 1)) as executor:
        futures = {
            executor.submit(_fetch_source, sn, registry[sn], faculty_dict, name): sn
            for sn in valid_sources
        }
        for future in as_completed(futures):
            source_name, result = future.result()
            if result:
                raw_data[source_name] = result
                summary["sources"][source_name] = {
                    "status": "data_found",
                    "fields": [k for k in result if not k.startswith("_")],
                }
            else:
                summary["sources"][source_name] = {"status": "no_data"}

    # If no sources returned data AND the faculty has no stored grants/pubs
    # to feed the normalizer, skip early.  Otherwise fall through so the
    # normalizer can synthesise from previously-stored data.
    has_stored_data = (faculty_dict.get("funded_grants") or
                       faculty_dict.get("recent_publications") or
                       faculty_dict.get("research_interests"))
    if not raw_data and not has_stored_data:
        logger.info("No enrichment data found for %s", name)
        return summary, []

    if dry_run:
        summary["dry_run"] = True
        summary["raw_data"] = {
            k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
            for k, v in raw_data.items()
        }
        return summary, []

    # Phase 2: Write fields. Sources are processed in descending confidence
    # order: direct fields are write-once (first/highest-confidence writer
    # wins) and JSON list fields are merged with higher-confidence entries
    # taking precedence on dedupe collisions.
    log_entries = []
    json_contributions = {}   # field -> [list per source, conf-desc order]
    json_provenance = {}      # field -> first (highest-conf) source info
    ordered = sorted(
        raw_data.items(),
        key=lambda item: getattr(registry.get(item[0], object), "confidence", 0.5),
        reverse=True,
    )
    for source_name, sdata in ordered:
        source = registry[source_name]
        for field, value in sdata.items():
            if field.startswith("_"):
                continue

            if field in JSON_FIELDS:
                json_contributions.setdefault(field, []).append(value)
                json_provenance.setdefault(field, (source_name, source, sdata))
                continue

            if field in DIRECT_FIELDS:
                old_value = faculty_dict.get(field)
                if old_value is not None and field not in REFRESHABLE_FIELDS:
                    continue  # Don't overwrite existing direct fields

                if field == "email" and isinstance(value, str):
                    value = unquote(value)
                faculty_dict[field] = value

                log_entries.append(_make_log_entry(
                    faculty_dict,
                    source_name=source_name,
                    field=field,
                    old_value=old_value,
                    new_value=value,
                    confidence=getattr(source, "confidence", 0.5),
                    method="api" if source_name not in ("ucsd_profile", "scripps_profile") else "scrape",
                    source_url=sdata.get("_source_url"),
                    raw_response=sdata,
                ))

    for field, contributions in json_contributions.items():
        old_value = faculty_dict.get(field)
        merged = _merge_json_field(field, contributions)
        if not merged:
            continue
        faculty_dict[field] = merged
        source_name, source, sdata = json_provenance[field]
        log_entries.append(_make_log_entry(
            faculty_dict,
            source_name=source_name if len(contributions) == 1 else f"{source_name}+merge",
            field=field,
            old_value=old_value,
            new_value=merged,
            confidence=getattr(source, "confidence", 0.5),
            method="api" if source_name not in ("ucsd_profile", "scripps_profile") else "scrape",
            source_url=sdata.get("_source_url"),
        ))

    # Phase 3: LLM normalization — skipped when the raw context that feeds
    # the LLM is byte-identical to the previous run (cost control).
    context = build_context(faculty_dict, raw_data)
    if context:
        fingerprint = _raw_fingerprint(context)
        already_normalized = bool(faculty_dict.get("research_interests_enriched"))
        if already_normalized and faculty_dict.get("raw_hash") == fingerprint:
            summary["normalization"] = "skipped_unchanged"
        else:
            normalized = normalize_from_context(name, context)
            if normalized:
                logged_context = False
                for field in ("research_interests_enriched", "expertise_keywords",
                               "methodologies", "disease_areas", "populations"):
                    value = normalized.get(field)
                    if value:
                        old_value = faculty_dict.get(field)
                        faculty_dict[field] = value

                        # Persist the full context the model saw on the first
                        # logged field (the enriched blurb, normally) so every
                        # enriched value is traceable; its hash equals raw_hash.
                        log_entries.append(_make_log_entry(
                            faculty_dict,
                            source_name="llm_normalizer",
                            field=field,
                            old_value=old_value,
                            new_value=value,
                            confidence=0.85,
                            method="llm_extraction",
                            raw_response=None if logged_context else context,
                        ))
                        logged_context = True
                faculty_dict["raw_hash"] = fingerprint
                summary["normalization"] = "success"
            else:
                summary["normalization"] = "skipped_or_failed"
    else:
        # No context means build_context had only the name to work with — this
        # is the exact signal behind the ledger's normalizer_no_input bucket.
        # Record it (empty context) so the cause is auditable, not inferred.
        log_entries.append(_make_log_entry(
            faculty_dict,
            source_name="llm_normalizer",
            field="research_interests_enriched",
            old_value=faculty_dict.get("research_interests_enriched"),
            new_value=None,
            confidence=0.0,
            method="no_context",
            raw_response=None,
        ))
        summary["normalization"] = "no_context"

    # Mark when this faculty was last enriched
    faculty_dict["last_enriched"] = datetime.now(timezone.utc).isoformat()

    # If caller doesn't own data, save here (single-faculty mode)
    if not caller_owns_data:
        _persist_record(faculty_dict, log_entries)
        log_entries = []

    logger.info("Enrichment complete for %s", name)
    return summary, log_entries


def _persist_record(faculty_dict, log_entries):
    """Write one mutated record + its log entries to the DB and commit."""
    conn = db.get_write_conn()
    faculty_id = faculty_dict.get("_db_id")
    if faculty_id is None:
        logger.error("Record for %s %s has no _db_id — not saved",
                     faculty_dict.get("first_name"), faculty_dict.get("last_name"))
        return
    db.save_faculty_record(conn, faculty_id, faculty_dict)
    db.append_log(conn, log_entries)
    conn.commit()


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------

def enrich_records(data, sources=None, dry_run=False, progress_callback=None,
                   time_budget_seconds=None, indices=None):
    """Enrich the given pipeline-shaped data dict record by record.

    Each record is persisted (and committed) as soon as it finishes, so a
    crash or time-budget stop never loses completed work — the natural
    checkpoint/resume unit is the faculty row itself.
    """
    if not dry_run:
        conn = db.get_write_conn()
        db.rotate_log(conn)
        conn.commit()

    faculty_list = data["faculty"]
    if indices is None:
        indices = list(range(len(faculty_list)))

    pool = SourcePool()
    results = []
    start_time = time.monotonic()

    for i, idx in enumerate(indices):
        # Check time budget before starting next faculty
        if time_budget_seconds is not None:
            elapsed = time.monotonic() - start_time
            remaining = time_budget_seconds - elapsed
            if remaining < 60:  # less than 60s left — stop gracefully
                logger.warning(
                    "Time budget nearly exhausted (%.0fs elapsed, %.0fs remaining). "
                    "Stopping after %d/%d faculty.",
                    elapsed, remaining, i, len(indices),
                )
                break

        try:
            result, log_entries = enrich_faculty(
                idx, sources=sources, dry_run=dry_run, _data=data, _pool=pool,
            )
            if not dry_run and not result.get("error"):
                _persist_record(faculty_list[idx], log_entries)
        except Exception:
            name = faculty_list[idx].get("last_name", str(idx))
            logger.exception("Unhandled error enriching faculty %s (index %d)", name, idx)
            result = {"faculty_index": idx, "name": name, "error": "Unhandled exception"}

        results.append(result)
        if progress_callback:
            progress_callback(i + 1, len(indices))

    enriched_count = sum(
        1 for r in results
        if any(s.get("status") == "data_found" for s in r.get("sources", {}).values())
    )
    elapsed = time.monotonic() - start_time
    logger.info(
        "Enrichment complete. %d/%d faculty had data found (%.0fs elapsed).",
        enriched_count, len(results), elapsed,
    )
    return results


def enrich_all(sources=None, faculty_ids=None, dry_run=False,
               progress_callback=None, department=None,
               time_budget_seconds=None):
    """Enrich all (or specified) faculty of one division.

    Args:
        sources: List of source names to use, or None for the routed bundle.
        faculty_ids: List of faculty indices within the division, or None.
        dry_run: If True, fetch but don't write.
        progress_callback: Optional callable(completed, total).
        department: Division slug; None means every division.
        time_budget_seconds: Maximum wall-clock seconds for the run.
    """
    data = _load_faculty(department)
    faculty_list = data["faculty"]

    if faculty_ids:
        indices = [i for i in faculty_ids if 0 <= i < len(faculty_list)]
    else:
        indices = list(range(len(faculty_list)))
        # Prioritize never-enriched faculty so time-budget cuts hit
        # already-enriched records instead of perpetually skipping the tail.
        indices.sort(key=lambda i: (
            bool(faculty_list[i].get("last_enriched")),
            bool(faculty_list[i].get("research_interests_enriched")),
        ))

    logger.info("Starting enrichment for %d faculty members (dept: %s).",
                len(indices), department or "all")
    return enrich_records(
        data, sources=sources, dry_run=dry_run,
        progress_callback=progress_callback,
        time_budget_seconds=time_budget_seconds, indices=indices,
    )


def enrich_backfill(pi_only=False, batch_size=None, dry_run=False,
                    progress_callback=None, time_budget_seconds=None):
    """Backfill never-enriched, identity-resolved faculty across divisions.

    Selection (and therefore resume-after-interruption) is the WHERE clause
    itself: anything finished gets last_enriched set and drops out of the
    next batch.
    """
    records = db.fetch_backfill_candidates(
        db.get_write_conn(), pi_only=pi_only, limit=batch_size,
    )
    logger.info("Backfill: %d candidates (pi_only=%s).", len(records), pi_only)
    return enrich_records(
        {"faculty": records}, dry_run=dry_run,
        progress_callback=progress_callback,
        time_budget_seconds=time_budget_seconds,
    )


def get_enrichment_status(department=None):
    """Return a summary of enrichment coverage."""
    return db.load_status(db.get_read_conn(), department)


# Combined registry of all known sources (for run.py source name validation)
ALL_SOURCE_CLASSES = all_source_classes()
