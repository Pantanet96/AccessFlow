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
    job_id = scheduler_module.JOB_IDS[0]
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
