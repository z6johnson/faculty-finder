"""In-process background job runner.

Replaces GitHub Actions as the place enrichment and EAH reconciliation run.
A single daemon worker thread processes jobs serially (enrichment is heavy and
the DB has a single writer), recording status in the `jobs` table so the admin
dashboard reflects live progress. Designed for the single-gunicorn-worker
deployment, so exactly one runner exists per process.
"""

import json
import logging
import queue
import threading

from data import db

logger = logging.getLogger(__name__)

_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()

VALID_KINDS = {"enrich", "eah_reconcile"}


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_loop, name="job-runner", daemon=True)
            _worker.start()


def submit(kind, params=None, trigger="manual"):
    """Queue a job and return its id."""
    if kind not in VALID_KINDS:
        raise ValueError(f"Unknown job kind: {kind}")
    job_id = db.create_job(kind, params or {}, trigger=trigger)
    _ensure_worker()
    _queue.put(job_id)
    logger.info("Queued job %s (%s, %s)", job_id, kind, trigger)
    return job_id


def _run_loop():
    while True:
        job_id = _queue.get()
        try:
            _execute(job_id)
        except Exception as e:
            logger.exception("Job %s failed", job_id)
            db.finish_job(job_id, "failed", error=str(e))
        finally:
            _queue.task_done()


def _execute(job_id):
    conn = db.get_read_conn()
    job = db.get_job(conn, job_id)
    if not job:
        logger.warning("Job %s vanished before execution", job_id)
        return
    db.start_job(job_id)
    params = json.loads(job["params"] or "{}")
    if job["kind"] == "enrich":
        result = _run_enrich(job_id, params)
    elif job["kind"] == "eah_reconcile":
        result = _run_eah(job_id)
    else:
        raise ValueError(f"Unknown job kind: {job['kind']}")
    db.finish_job(job_id, "succeeded", result=result)
    logger.info("Job %s succeeded", job_id)


def _run_enrich(job_id, params):
    from enrichment.pipeline import enrich_all
    from scripts.migrate_json_to_sqlite import run_migration

    department = params.get("department") or None
    last = [0]

    def progress(done, total):
        # Throttle DB writes — enrich_all calls this once per faculty.
        if done == total or done - last[0] >= 5:
            last[0] = done
            db.set_job_progress(job_id, f"{done}/{total}")

    results = enrich_all(department=department, progress_callback=progress)
    # Enrichment only updates existing records (no removals), so an upsert sync
    # is sufficient and cheaper than a full rebuild.
    run_migration()
    enriched = sum(
        1 for r in results
        if any(s.get("status") == "data_found" for s in r.get("sources", {}).values())
    )
    return {
        "department": department or "hwsph",
        "processed": len(results),
        "data_found": enriched,
    }


def _run_eah(job_id):
    from scripts import eah_enrichment
    from scripts.migrate_json_to_sqlite import run_migration

    db.set_job_progress(job_id, "reconciling")
    try:
        result = eah_enrichment.run_eah_reconcile()
    except eah_enrichment.EAHFileMissing as e:
        # A scheduled reconcile with no uploaded extract is a no-op, not an error.
        return {"skipped": str(e)}
    # rebuild=True so pruned-inactive faculty are removed from the live DB.
    run_migration(rebuild=True)
    return result
