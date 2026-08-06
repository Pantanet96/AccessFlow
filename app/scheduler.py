"""APScheduler background jobs: daily expiry scan + nightly DB backup."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings

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
    """Daily auto-import of Plex-shared users (keeps app in sync, no ghosts)."""
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


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone=settings.tz)
    _scheduler.add_job(
        _run_expiry_scan,
        CronTrigger(hour=settings.notify_hour, minute=0),
        id="expiry_scan",
        replace_existing=True,
    )
    _scheduler.add_job(
        _run_plex_import,
        CronTrigger(hour=4, minute=0),
        id="plex_auto_import",
        replace_existing=True,
    )
    _scheduler.add_job(
        _run_backup,
        CronTrigger(hour=3, minute=30),
        id="db_backup",
        replace_existing=True,
    )
    _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
