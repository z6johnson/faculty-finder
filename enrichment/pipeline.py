"""Enrichment pipeline orchestrator.

Coordinates fetching data from multiple sources, normalizing it with LLM,
and writing enriched data back to the JSON file.
"""

import json
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import unquote

from .normalizer import normalize_faculty_data
from .sources.nih_reporter import NIHReporterSource
from .sources.nsf_awards import NSFAwardSource
from .sources.orcid import ORCIDSource
from .sources.pubmed import PubMedSource
from .sources.scripps_profile import ScrippsProfileSource
from .sources.semantic_scholar import SemanticScholarSource
from .sources.email_pattern import EmailPatternSource
from .sources.ucsd_profile import UCSDProfileSource

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
FACULTY_PATH = os.path.join(DATA_DIR, "faculty.json")
SIO_FACULTY_PATH = os.path.join(DATA_DIR, "sio_faculty.json")
JACOBS_FACULTY_PATH = os.path.join(DATA_DIR, "jacobs_faculty.json")
LOG_PATH = os.path.join(DATA_DIR, "enrichment_log.jsonl")

# Registry of available sources — used by HWSPH (public health) faculty
SOURCE_CLASSES = {
    "ucsd_profile": UCSDProfileSource,
    "nih_reporter": NIHReporterSource,
    "pubmed": PubMedSource,
    "orcid": ORCIDSource,
    "semantic_scholar": SemanticScholarSource,
}

# Sources for Scripps Institution of Oceanography faculty
SIO_SOURCE_CLASSES = {
    "scripps_profile": ScrippsProfileSource,
    "nsf_awards": NSFAwardSource,
    "nih_reporter": NIHReporterSource,
    "pubmed": PubMedSource,
    "orcid": ORCIDSource,
    "semantic_scholar": SemanticScholarSource,
}

# Sources for Jacobs School of Engineering faculty
JACOBS_SOURCE_CLASSES = {
    "ucsd_profile": UCSDProfileSource,
    "nsf_awards": NSFAwardSource,
    "nih_reporter": NIHReporterSource,
    "pubmed": PubMedSource,
    "orcid": ORCIDSource,
    "semantic_scholar": SemanticScholarSource,
    "email_pattern": EmailPatternSource,
}

# Combined registry of all known sources (for run.py source name validation)
ALL_SOURCE_CLASSES = {**SOURCE_CLASSES, **SIO_SOURCE_CLASSES, **JACOBS_SOURCE_CLASSES}

# Fields that can be directly written to a faculty record (non-JSON)
DIRECT_FIELDS = {"profile_url", "orcid", "google_scholar_id", "h_index", "email"}

# Direct fields that should be refreshed on every run (not write-once)
REFRESHABLE_FIELDS = {"h_index"}

# Fields that are JSON arrays and should be replaced wholesale
JSON_FIELDS = {"funded_grants", "recent_publications", "expertise_keywords"}


def _faculty_path(department=None):
    """Return the faculty JSON path for the given department."""
    if department == "sio":
        return SIO_FACULTY_PATH
    if department == "jacobs":
        return JACOBS_FACULTY_PATH
    return FACULTY_PATH


def _load_faculty(department=None):
    """Load faculty data from JSON file."""
    path = _faculty_path(department)
    with open(path) as f:
        return json.load(f)


def _save_faculty(data, department=None):
    """Atomically write faculty data back to JSON file."""
    path = _faculty_path(department)
    fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def _source_classes_for(department=None):
    """Return the appropriate source class registry for a department."""
    if department == "sio":
        return SIO_SOURCE_CLASSES
    if department == "jacobs":
        return JACOBS_SOURCE_CLASSES
    return SOURCE_CLASSES


def _load_log():
    """Load enrichment log (JSONL format), returning empty list if missing."""
    if not os.path.exists(LOG_PATH):
        return []
    entries = []
    with open(LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def _append_log_batch(entries):
    """Append multiple entries to the enrichment log (JSONL, O(1) per entry)."""
    if not entries:
        return
    with open(LOG_PATH, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _append_log(entry):
    """Append a single entry to the enrichment log."""
    _append_log_batch([entry])


def _rotate_log(max_age_days=30):
    """Prune log entries older than max_age_days."""
    if not os.path.exists(LOG_PATH):
        return
    entries = _load_log()
    if not entries:
        return
    from datetime import timedelta
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cutoff_iso = cutoff_dt.isoformat()

    kept = [e for e in entries if (e.get("retrieved_at") or "") >= cutoff_iso]
    pruned = len(entries) - len(kept)
    if pruned > 0:
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR, suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as f:
                for entry in kept:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp, LOG_PATH)
        except Exception:
            os.unlink(tmp)
            raise
        logger.info("Log rotation: pruned %d entries older than %d days, kept %d.",
                     pruned, max_age_days, len(kept))


def _make_log_entry(faculty_index, source_name, field, old_value, new_value,
                    confidence, method, source_url=None, raw_response=None):
    """Create a log entry dict."""
    return {
        "faculty_index": faculty_index,
        "source_name": source_name,
        "source_url": source_url,
        "field_updated": field,
        "old_value": json.dumps(old_value) if isinstance(old_value, (list, dict)) else str(old_value),
        "new_value": json.dumps(new_value) if isinstance(new_value, (list, dict)) else str(new_value),
        "confidence": confidence,
        "method": method,
        "raw_response": (json.dumps(raw_response)[:5000]) if raw_response else None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_source(source_name, registry, faculty_dict, name):
    """Fetch data from a single source. Designed to run in a thread."""
    source = registry[source_name]()
    try:
        result = source.fetch(faculty_dict)
    except Exception:
        logger.exception("Source %s failed for %s", source_name, name)
        result = None
    return source_name, result


def enrich_faculty(faculty_index, sources=None, dry_run=False, department=None,
                   _data=None):
    """Enrich a single faculty member from specified sources.

    Args:
        faculty_index: Index of the faculty member in the faculty array.
        sources: List of source names to use, or None for all.
        dry_run: If True, fetch data but don't write.
        department: Department key ("sio" for Scripps, None for HWSPH).
        _data: Pre-loaded faculty data dict (used by enrich_all to avoid
               repeated file I/O). When provided, the caller is responsible
               for saving.

    Returns:
        Dict summarizing what was enriched.
    """
    registry = _source_classes_for(department)
    caller_owns_data = _data is not None
    data = _data if caller_owns_data else _load_faculty(department)
    faculty_list = data["faculty"]

    if faculty_index < 0 or faculty_index >= len(faculty_list):
        logger.error("Faculty index %d out of range (0-%d).", faculty_index, len(faculty_list) - 1)
        return {"error": f"Faculty index {faculty_index} out of range"}, []

    faculty_dict = faculty_list[faculty_index]
    name = f"{faculty_dict.get('first_name', '')} {faculty_dict.get('last_name', '')}"
    logger.info("Enriching: %s (index: %d, dept: %s)", name, faculty_index, department or "hwsph")

    source_names = sources or list(registry.keys())
    valid_sources = [s for s in source_names if s in registry]
    for s in source_names:
        if s not in registry:
            logger.warning("Unknown source for dept %s: %s", department or "hwsph", s)

    raw_data = {}
    summary = {"faculty_index": faculty_index, "name": name, "sources": {}}

    # Phase 1: Fetch from all sources concurrently
    with ThreadPoolExecutor(max_workers=len(valid_sources)) as executor:
        futures = {
            executor.submit(_fetch_source, sn, registry, faculty_dict, name): sn
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

    # Phase 2: Write direct fields
    # Process sources in descending confidence order so higher-confidence
    # values are written first (and won't be overwritten by lower ones).
    log_entries = []
    for source_name, sdata in sorted(
        raw_data.items(),
        key=lambda item: getattr(registry.get(item[0], object), "confidence", 0.5),
        reverse=True,
    ):
        source_cls = registry[source_name]
        for field, value in sdata.items():
            if field.startswith("_"):
                continue

            if field in DIRECT_FIELDS or field in JSON_FIELDS:
                old_value = faculty_dict.get(field)
                if old_value is not None and field not in JSON_FIELDS and field not in REFRESHABLE_FIELDS:
                    continue  # Don't overwrite existing direct fields

                if field == "email" and isinstance(value, str):
                    value = unquote(value)
                faculty_dict[field] = value

                log_entries.append(_make_log_entry(
                    faculty_index=faculty_index,
                    source_name=source_name,
                    field=field,
                    old_value=old_value,
                    new_value=value,
                    confidence=source_cls.confidence if hasattr(source_cls, "confidence") else 0.5,
                    method="api" if source_name not in ("ucsd_profile", "scripps_profile") else "scrape",
                    source_url=sdata.get("_source_url"),
                    raw_response=sdata,
                ))

    # Phase 3: LLM normalization
    normalized = normalize_faculty_data(faculty_dict, raw_data)
    if normalized:
        for field in ("research_interests_enriched", "expertise_keywords",
                       "methodologies", "disease_areas", "populations"):
            value = normalized.get(field)
            if value:
                old_value = faculty_dict.get(field)
                faculty_dict[field] = value

                log_entries.append(_make_log_entry(
                    faculty_index=faculty_index,
                    source_name="llm_normalizer",
                    field=field,
                    old_value=old_value,
                    new_value=value,
                    confidence=0.85,
                    method="llm_extraction",
                ))

        summary["normalization"] = "success"
    else:
        summary["normalization"] = "skipped_or_failed"

    # Mark when this faculty was last enriched
    faculty_dict["last_enriched"] = datetime.now(timezone.utc).isoformat()

    # If caller doesn't own data, save here (single-faculty mode)
    if not caller_owns_data:
        _save_faculty(data, department)
        for entry in log_entries:
            _append_log(entry)
        log_entries = []

    logger.info("Enrichment complete for %s", name)
    return summary, log_entries


def enrich_all(sources=None, faculty_ids=None, dry_run=False,
               progress_callback=None, department=None,
               time_budget_seconds=None):
    """Enrich all (or specified) faculty members.

    Args:
        sources: List of source names to use, or None for all.
        faculty_ids: List of specific faculty indices, or None for all.
        dry_run: If True, fetch but don't write.
        progress_callback: Optional callable(completed, total) for progress tracking.
        department: Department key ("sio" for Scripps, None for HWSPH).
        time_budget_seconds: Maximum wall-clock seconds for the run. When
            approaching this limit the pipeline saves partial results and
            stops gracefully.  None means no limit.

    Returns:
        List of per-faculty summary dicts.
    """
    # Rotate log at the start of each batch run to prevent unbounded growth
    if not dry_run:
        _rotate_log()

    data = _load_faculty(department)
    faculty_list = data["faculty"]

    if faculty_ids:
        indices = [i for i in faculty_ids if 0 <= i < len(faculty_list)]
    else:
        indices = list(range(len(faculty_list)))
        # Prioritize never-enriched faculty so time-budget cuts hit
        # already-enriched records instead of perpetually skipping the tail.
        # Secondary: among enriched, prioritize those still missing interests.
        indices.sort(key=lambda i: (
            bool(faculty_list[i].get("last_enriched")),
            bool(faculty_list[i].get("research_interests_enriched")),
        ))

    logger.info("Starting enrichment for %d faculty members (dept: %s).",
                len(indices), department or "hwsph")
    results = []
    all_log_entries = []
    start_time = time.monotonic()

    # Save interval: persist to disk every N faculty to avoid losing work
    SAVE_INTERVAL = 10

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
                idx, sources=sources, dry_run=dry_run,
                department=department, _data=data,
            )
        except Exception:
            name = faculty_list[idx].get("last_name", str(idx))
            logger.exception("Unhandled error enriching faculty %s (index %d)", name, idx)
            result = {"faculty_index": idx, "name": name, "error": "Unhandled exception"}
            log_entries = []

        results.append(result)
        all_log_entries.extend(log_entries)

        if progress_callback:
            progress_callback(i + 1, len(indices))

        # Periodic save to avoid losing work on crash/timeout
        if not dry_run and (i + 1) % SAVE_INTERVAL == 0 and all_log_entries:
            _save_faculty(data, department)
            _append_log_batch(all_log_entries)
            all_log_entries = []
            logger.info("Checkpoint: saved after %d/%d faculty.", i + 1, len(indices))

    # Final save
    if not dry_run and all_log_entries:
        _save_faculty(data, department)
        _append_log_batch(all_log_entries)
    elif not dry_run and results:
        # Even if no new log entries in last batch, save faculty data
        _save_faculty(data, department)

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


def get_enrichment_status(department=None):
    """Return a summary of enrichment coverage."""
    data = _load_faculty(department)
    faculty_list = data["faculty"]
    total = len(faculty_list)

    with_original = sum(1 for f in faculty_list if f.get("research_interests"))
    with_enriched = sum(1 for f in faculty_list if f.get("research_interests_enriched"))
    with_grants = sum(1 for f in faculty_list if f.get("funded_grants"))
    with_pubs = sum(1 for f in faculty_list if f.get("recent_publications"))

    return {
        "total_faculty": total,
        "with_original_interests": with_original,
        "with_enriched_interests": with_enriched,
        "with_funded_grants": with_grants,
        "with_publications": with_pubs,
        "coverage_original": round(with_original / total * 100, 1) if total else 0,
        "coverage_enriched": round(with_enriched / total * 100, 1) if total else 0,
    }
