"""APScheduler background jobs, each on its own admin-configurable interval."""
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.i18n import N_

_scheduler: BackgroundScheduler | None = None


def _run_expiry_scan() -> None:
    import logging

    from sqlmodel import Session

    from app.db import engine
    from app.services.access_service import reconcile_all, resync_libraries
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


# Every background job, in the order they're listed on Settings > Jobs. Each
# runs on its own admin-configurable interval (see runtime_config.job_interval_hours),
# counted from whenever the scheduler last (re)started -- not a fixed clock time.
JOBS = (
    {"id": "expiry_scan", "label": N_("Expiry scan & reminders"), "fn": _run_expiry_scan},
    {"id": "plex_auto_import", "label": N_("Plex reconciliation"), "fn": _run_plex_import},
    {"id": "db_backup", "label": N_("Database backup"), "fn": _run_backup},
)
JOB_IDS = tuple(j["id"] for j in JOBS)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone=settings.tz)
    from app import runtime_config

    for job in JOBS:
        _scheduler.add_job(
            job["fn"],
            IntervalTrigger(hours=runtime_config.job_interval_hours(job["id"])),
            id=job["id"],
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
    """Apply a freshly-saved interval to the running job immediately (no app
    restart needed). No-op if the scheduler isn't running (e.g. disabled, or
    called from a test)."""
    if _scheduler is None:
        return
    from app import runtime_config

    hours = runtime_config.job_interval_hours(job_id)
    _scheduler.reschedule_job(job_id, trigger=IntervalTrigger(hours=hours))


def job_next_run(job_id: str) -> datetime | None:
    """When the named job will next fire, for display in Settings > Jobs."""
    if _scheduler is None:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None


def run_job_now(job_id: str) -> bool:
    """Execute the named job's function immediately, synchronously (the
    Settings > Jobs 'Run now' button). Does not touch its schedule. Returns
    False if job_id is unknown."""
    for j in JOBS:
        if j["id"] == job_id:
            j["fn"]()
            return True
    return False


def jobs_status() -> list[dict]:
    """[{id, label, interval_hours, next_run}] for the Settings > Jobs page."""
    from app import runtime_config

    return [
        {
            "id": j["id"],
            "label": j["label"],
            "interval_hours": runtime_config.job_interval_hours(j["id"]),
            "next_run": job_next_run(j["id"]),
        }
        for j in JOBS
    ]
