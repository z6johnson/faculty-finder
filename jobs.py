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

VALID_KINDS = {"enrich", "eah_reconcile", "identity_resolve", "backfill",
               "escholarship_harvest"}


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
    elif job["kind"] == "identity_resolve":
        result = _run_identity(job_id, params)
    elif job["kind"] == "backfill":
        result = _run_backfill(job_id, params)
    elif job["kind"] == "escholarship_harvest":
        result = _run_escholarship(job_id, params)
    else:
        raise ValueError(f"Unknown job kind: {job['kind']}")
    db.finish_job(job_id, "succeeded", result=result)
    logger.info("Job %s succeeded", job_id)


def _progress_writer(job_id, every=5):
    last = [0]

    def progress(done, total):
        # Throttle DB writes — callers report once per record.
        if done == total or done - last[0] >= every:
            last[0] = done
            db.set_job_progress(job_id, f"{done}/{total}")

    return progress


def _run_enrich(job_id, params):
    from enrichment.pipeline import enrich_all

    department = params.get("department") or "hwsph"
    results = enrich_all(department=department,
                         progress_callback=_progress_writer(job_id))
    enriched = sum(
        1 for r in results
        if any(s.get("status") == "data_found" for s in r.get("sources", {}).values())
    )
    return {
        "department": department,
        "processed": len(results),
        "data_found": enriched,
    }


def _run_eah(job_id):
    from scripts import eah_enrichment

    db.set_job_progress(job_id, "reconciling")
    try:
        # The reconcile now writes straight to SQLite (the source of truth):
        # no JSON re-import, so EAH-seeded rows outside the legacy JSON files
        # are never dropped. Rows absent from EAH are soft-flagged inactive.
        return eah_enrichment.run_eah_reconcile()
    except eah_enrichment.EAHFileMissing as e:
        # A scheduled reconcile with no uploaded extract is a no-op, not an error.
        return {"skipped": str(e)}


def _run_identity(job_id, params):
    from enrichment.identity import resolve_batch

    return resolve_batch(
        department=params.get("department") or None,
        pi_only=bool(params.get("pi_only")),
        limit=params.get("limit"),
        include_not_found=bool(params.get("include_not_found")),
        progress_callback=_progress_writer(job_id, every=25),
        time_budget_seconds=params.get("time_budget_seconds"),
    )


def _run_backfill(job_id, params):
    from enrichment.pipeline import enrich_backfill

    results = enrich_backfill(
        pi_only=bool(params.get("pi_only")),
        batch_size=params.get("batch_size"),
        progress_callback=_progress_writer(job_id),
        time_budget_seconds=params.get("time_budget_seconds", 4 * 3600),
    )
    enriched = sum(
        1 for r in results
        if any(s.get("status") == "data_found" for s in r.get("sources", {}).values())
    )
    return {"processed": len(results), "data_found": enriched,
            "pi_only": bool(params.get("pi_only"))}


def _run_escholarship(job_id, params):
    from enrichment.escholarship_harvest import run_harvest

    def progress(records, batches):
        db.set_job_progress(job_id, f"{records} records / {batches} batches")

    return run_harvest(
        time_budget_seconds=params.get("time_budget_seconds", 2 * 3600),
        progress_callback=progress,
    )
