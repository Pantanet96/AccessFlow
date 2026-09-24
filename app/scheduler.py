"""APScheduler background jobs.

Two scheduling styles, per job:
- "daily": fires at a fixed hour of day (CronTrigger) -- for jobs whose output
  reaches people (reminder emails, manager digests), where a predictable time
  of day matters more than exact regularity.
- "interval": fires every N hours counted from whenever the scheduler last
  (re)started (IntervalTrigger) -- for silent internal maintenance, where only
  "often enough" matters.
Both are admin-configurable from Settings > Jobs.
"""
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.i18n import N_

_scheduler: BackgroundScheduler | None = None


def _run_expiry_scan() -> None:
    import logging

    from sqlmodel import Session

    from app.db import engine
    from app.services.access_service import reconcile_all, resync_libraries
    from app.services.invites import expire_invites
    from app.services.notifications import (
        prune_old_notifications,
        run_expiry_scan,
        run_manager_digests,
    )

    log = logging.getLogger("pum.scheduler")
    # Isolate each stage: a failure in one (e.g. a Plex/SMTP hiccup) must NOT skip
    # the others. Auto-suspend in particular must still run even if reminders fail.
    stages = (
        # Send reminders + flip lapsed subs to `expired`.
        ("expiry_scan", run_expiry_scan),
        # Auto-suspend users expired beyond their grace period (Plex + Overseerr).
        ("reconcile", reconcile_all),
        # Re-apply configured libraries to active users (propagates plan/default
        # changes; drops titles deleted on Plex). Never auto-shares new libraries.
        ("resync_libraries", resync_libraries),
        # Pending invites past their 30 days: expire + withdraw the Plex share.
        ("expire_invites", expire_invites),
        # Weekly per-manager collect digest (only fires on each manager's weekday).
        ("manager_digests", run_manager_digests),
        # Trim notification_log to the admin-configured retention window (no-op at 0).
        ("prune_notifications", prune_old_notifications),
    )
    with Session(engine) as session:
        for name, fn in stages:
            try:
                fn(session)
            except Exception as exc:  # noqa: BLE001 - isolate stages
                session.rollback()
                log.warning("scheduled stage %r failed: %s", name, exc)


def _run_plex_import() -> None:
    """Auto-import of Plex-shared users + pending-invite reconciliation."""
    import logging

    from sqlmodel import Session

    from app.db import engine
    from app.services import plex_import
    from app.services.plex_service import PlexNotConnected

    try:
        with Session(engine) as session:
            plex_import.import_plex_users(session)
    except PlexNotConnected:
        return  # Plex not configured yet -> nothing to do
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("pum.scheduler").warning("auto-import failed: %s", exc)


def _run_backup() -> None:
    from app.services.backup import backup_database

    backup_database()


# Every background job, in the order they're listed on Settings > Jobs.
# schedule: "daily" (fixed hour, see runtime_config.job_run_hour) or
# "interval" (every N hours from scheduler start, see runtime_config.job_interval_hours).
JOBS = (
    {
        "id": "expiry_scan", "label": N_("Expiry scan & reminders"),
        "fn": _run_expiry_scan, "schedule": "daily",
    },
    {
        "id": "plex_auto_import", "label": N_("Plex reconciliation"),
        "fn": _run_plex_import, "schedule": "interval",
    },
    {
        "id": "db_backup", "label": N_("Database backup"),
        "fn": _run_backup, "schedule": "interval",
    },
)
JOB_IDS = tuple(j["id"] for j in JOBS)

# One run per job at a time, shared by the scheduler and "Run now": two
# overlapping expiry scans both passed the dedup check and double-sent.
# ponytail: in-process locks, enough for the single-worker container; a
# multi-process deploy would need a DB/file lock instead.
_locks = {job_id: threading.Lock() for job_id in JOB_IDS}


def _run_exclusive(job_id: str) -> bool:
    """Run the job unless it is already running. False if skipped."""
    lock = _locks[job_id]
    if not lock.acquire(blocking=False):
        return False
    try:
        job_by_id(job_id)["fn"]()
    finally:
        lock.release()
    return True


def job_by_id(job_id: str) -> dict | None:
    return next((j for j in JOBS if j["id"] == job_id), None)


def _trigger_for(job: dict):
    from app import runtime_config

    if job["schedule"] == "daily":
        return CronTrigger(hour=runtime_config.job_run_hour(job["id"]), minute=0)
    return IntervalTrigger(hours=runtime_config.job_interval_hours(job["id"]))


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone=settings.tz)
    for job in JOBS:
        _scheduler.add_job(
            _run_exclusive, _trigger_for(job), args=[job["id"]], id=job["id"],
            replace_existing=True,
        )
    _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def reschedule_job(job_id: str) -> None:
    """Apply a freshly-saved schedule to the running job immediately (no app
    restart needed). No-op if the scheduler isn't running (e.g. disabled, or
    called from a test) or job_id is unknown."""
    if _scheduler is None:
        return
    job = job_by_id(job_id)
    if job is None:
        return
    _scheduler.reschedule_job(job_id, trigger=_trigger_for(job))


def job_next_run(job_id: str) -> datetime | None:
    """When the named job will next fire, for display in Settings > Jobs."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None


def run_job_now(job_id: str) -> bool:
    """Execute the named job's function immediately, synchronously (the
    Settings > Jobs 'Run now' button). Does not touch its schedule. Returns
    False if job_id is unknown or the job is already running."""
    if job_by_id(job_id) is None:
        return False
    return _run_exclusive(job_id)


def jobs_status() -> list[dict]:
    """[{id, label, schedule, interval_hours|run_hour, next_run}] for
    Settings > Jobs."""
    from app import runtime_config

    out = []
    for j in JOBS:
        entry = {
            "id": j["id"], "label": j["label"], "schedule": j["schedule"],
            "next_run": job_next_run(j["id"]),
        }
        if j["schedule"] == "daily":
            entry["run_hour"] = runtime_config.job_run_hour(j["id"])
        else:
            entry["interval_hours"] = runtime_config.job_interval_hours(j["id"])
        out.append(entry)
    return out
