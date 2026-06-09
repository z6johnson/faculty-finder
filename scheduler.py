"""In-process weekly scheduler — replaces the retired GitHub Actions cron.

Mirrors the old cadence (one school per slot, Sundays UTC) plus a weekly EAH
reconcile that no-ops if no extract has been uploaded. Runs inside the single
gunicorn worker, so exactly one scheduler exists. Disable with
ENABLE_SCHEDULER=false (e.g. in tests).
"""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

import jobs

logger = logging.getLogger(__name__)

_scheduler = None

# (department, UTC hour) — matches the retired .github/workflows/enrich.yml.
ENRICH_SLOTS = [("hwsph", 0), ("sio", 2), ("jacobs", 4)]


def start():
    """Start the scheduler once; subsequent calls are no-ops."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone="UTC")
    for dept, hour in ENRICH_SLOTS:
        sched.add_job(
            jobs.submit, "cron", day_of_week="sun", hour=hour, minute=0,
            args=["enrich", {"department": dept}], kwargs={"trigger": "schedule"},
            id=f"enrich_{dept}", replace_existing=True,
        )
    eah_hour = int(os.environ.get("EAH_RECONCILE_HOUR", "6"))
    sched.add_job(
        jobs.submit, "cron", day_of_week="sun", hour=eah_hour, minute=0,
        args=["eah_reconcile"], kwargs={"trigger": "schedule"},
        id="eah_reconcile", replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    logger.info("Scheduler started (weekly enrichment + EAH reconcile, UTC).")
    return sched


def scheduled_jobs():
    """List scheduled jobs with their next run time, for the admin UI."""
    if _scheduler is None:
        return []
    return [
        {"id": j.id, "next_run": j.next_run_time.isoformat() if j.next_run_time else None}
        for j in _scheduler.get_jobs()
    ]
