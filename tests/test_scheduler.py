from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import app.scheduler as scheduler_module


def test_job_next_run_none_without_scheduler():
    scheduler_module._scheduler = None
    assert scheduler_module.job_next_run("plex_auto_import") is None


def test_jobs_status_lists_all_jobs_even_without_scheduler(db_session):
    # jobs_status() reads each job's interval from the DB -- db_session guarantees
    # the schema exists regardless of what other tests have run before this one.
    scheduler_module._scheduler = None
    status = scheduler_module.jobs_status()
    assert {j["id"] for j in status} == set(scheduler_module.JOB_IDS)
    assert all(j["next_run"] is None for j in status)


def test_reschedule_job_applies_new_interval(monkeypatch):
    job_id = "plex_auto_import"  # an "interval"-scheduled job, not the "daily" expiry_scan
    sched = BackgroundScheduler()
    sched.add_job(lambda: None, IntervalTrigger(hours=24), id=job_id)
    sched.start()
    scheduler_module._scheduler = sched
    try:
        first = scheduler_module.job_next_run(job_id)
        assert first is not None

        monkeypatch.setattr(
            "app.runtime_config.job_interval_hours", lambda jid: 1
        )
        scheduler_module.reschedule_job(job_id)
        second = scheduler_module.job_next_run(job_id)
        assert second is not None
        assert second < first  # 1h from now is sooner than the old 24h schedule
    finally:
        sched.shutdown(wait=False)
        scheduler_module._scheduler = None


def test_reschedule_job_noop_without_scheduler():
    scheduler_module._scheduler = None
    scheduler_module.reschedule_job("plex_auto_import")  # must not raise


def test_expiry_scan_is_daily_scheduled():
    """The only job whose output reaches people (reminders, manager digests)
    must run at a fixed hour of day, not an arbitrary interval from restart."""
    job = scheduler_module.job_by_id("expiry_scan")
    assert job["schedule"] == "daily"
    for job_id in ("plex_auto_import", "db_backup"):
        assert scheduler_module.job_by_id(job_id)["schedule"] == "interval"


def test_reschedule_daily_job_uses_run_hour(monkeypatch):
    from apscheduler.triggers.cron import CronTrigger

    sched = BackgroundScheduler()
    sched.add_job(lambda: None, CronTrigger(hour=9, minute=0), id="expiry_scan")
    sched.start()
    scheduler_module._scheduler = sched
    try:
        monkeypatch.setattr("app.runtime_config.job_run_hour", lambda jid: 14)
        scheduler_module.reschedule_job("expiry_scan")
        job = sched.get_job("expiry_scan")
        assert isinstance(job.trigger, CronTrigger)
        assert job.next_run_time.hour == 14
    finally:
        sched.shutdown(wait=False)
        scheduler_module._scheduler = None


def test_job_by_id_unknown_returns_none():
    assert scheduler_module.job_by_id("not-a-real-job") is None
