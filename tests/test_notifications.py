from datetime import timedelta

from sqlmodel import select

import app.services.mail_service as mail_service
import app.services.telegram_service as telegram_service
from app.models import (
    AppUser,
    NotificationLog,
    Plan,
    Role,
    Subscription,
    SubscriptionStatus,
    utcnow,
)
from app.services import notifications as notif


def _plan(session, slug):
    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def _user(session, name, **kw):
    u = AppUser(role=Role.user, real_name=name, **kw)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _sub(session, user, plan, days_left):
    s = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        expiry_at=utcnow() + timedelta(days=days_left),
        status=SubscriptionStatus.active,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _capture(monkeypatch):
    sent = {"email": [], "tg": []}
    monkeypatch.setattr(
        mail_service, "send_email",
        lambda to, subj, body, html=None: sent["email"].append(to) or True,
    )
    monkeypatch.setattr(
        telegram_service,
        "send_message",
        lambda chat, text, parse_mode=None: sent["tg"].append(chat) or True,
    )
    return sent


def test_send_email_noop_without_smtp(db_session):
    # SMTP host empty in tests -> returns False
    assert mail_service.send_email("x@example.com", "s", "b") is False


def test_notify_sends_user_only_and_dedups(db_session, monkeypatch):
    sent = _capture(monkeypatch)
    mgr = AppUser(role=Role.moderator, real_name="Mgr", notify_email="mgr@example.com")
    db_session.add(mgr)
    db_session.commit()
    db_session.refresh(mgr)
    user = _user(
        db_session,
        "Paolo",
        plex_email="paolo@example.com",
        telegram_id="111",
        manager_id=mgr.id,
    )
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 7)

    notif.notify_expiry(db_session, sub, 7)
    # Manager is NOT pinged per-user anymore (weekly digest instead): user email + user telegram.
    assert len(sent["email"]) == 1
    assert "mgr@example.com" not in sent["email"]
    assert len(sent["tg"]) == 1

    # second call -> deduped, no new sends
    notif.notify_expiry(db_session, sub, 7)
    assert len(sent["email"]) == 1
    assert len(sent["tg"]) == 1
    logs = db_session.exec(
        select(NotificationLog).where(NotificationLog.subscription_id == sub.id)
    ).all()
    assert len(logs) == 2


def test_collectables_filters_window_overdue_trial_and_pending(db_session):
    bronze = _plan(db_session, "bronze")
    mgr = AppUser(role=Role.moderator, real_name="M")
    db_session.add(mgr)
    db_session.commit()
    db_session.refresh(mgr)

    in_win = _user(db_session, "InWin", manager_id=mgr.id)
    _sub(db_session, in_win, bronze, 5)            # paid, within 14d -> included
    overdue = _user(db_session, "Ovd", manager_id=mgr.id)
    _sub(db_session, overdue, bronze, -3)          # paid, overdue -> included
    far = _user(db_session, "Far", manager_id=mgr.id)
    _sub(db_session, far, bronze, 40)              # beyond 14d window -> excluded
    trial = _user(db_session, "Trial", manager_id=mgr.id)
    _sub(db_session, trial, _plan(db_session, "trial"), 3)  # not paid -> excluded

    rows = notif.collectables(db_session, manager_id=mgr.id, lookahead=14)
    names = {r["user_name"] for r in rows}
    assert names == {"InWin", "Ovd"}
    assert rows[0]["user_name"] == "Ovd"  # most overdue sorts first

    # a pending renewal removes the user from the collectables list
    from app.services import subscriptions as sub_svc
    sub = sub_svc.get_active_subscription(db_session, in_win.id)
    sub_svc.create_renewal(db_session, sub, actor_id=mgr.id, collected_by=mgr.id)
    rows2 = notif.collectables(db_session, manager_id=mgr.id, lookahead=14)
    assert {r["user_name"] for r in rows2} == {"Ovd"}


def test_run_manager_digests_weekday_gate_and_dedup(db_session, monkeypatch):
    from datetime import datetime, timedelta

    from app.models import utcnow

    sent = _capture(monkeypatch)
    bronze = _plan(db_session, "bronze")
    when = utcnow()
    mgr = AppUser(
        role=Role.moderator, real_name="Mgr", notify_email="mgr@example.com",
        telegram_id="999", digest_enabled=True, digest_weekday=when.weekday(),
    )
    db_session.add(mgr)
    db_session.commit()
    db_session.refresh(mgr)
    u = _user(db_session, "Cliente", manager_id=mgr.id)
    _sub(db_session, u, bronze, 5)

    # wrong weekday -> nothing
    other_day = when + timedelta(days=1)
    assert notif.run_manager_digests(db_session, today=other_day) == 0
    assert sent["email"] == []

    # right weekday -> one email + one telegram
    assert notif.run_manager_digests(db_session, today=when) == 2
    assert sent["email"] == ["mgr@example.com"]
    assert sent["tg"] == ["999"]

    # same ISO week -> deduped, no resend
    assert notif.run_manager_digests(db_session, today=when) == 0
    assert sent["email"] == ["mgr@example.com"]


def test_manager_collect_skipped_for_free_plan(db_session, monkeypatch):
    sent = _capture(monkeypatch)
    mgr = AppUser(role=Role.admin, real_name="M2", notify_email="m2@example.com")
    db_session.add(mgr)
    db_session.commit()
    db_session.refresh(mgr)
    user = _user(db_session, "Free", plex_email="free@example.com", manager_id=mgr.id)
    sub = _sub(db_session, user, _plan(db_session, "trial"), 3)

    notif.notify_expiry(db_session, sub, 3)
    # user gets notified, manager does NOT (plan not paid)
    assert "free@example.com" in sent["email"]
    assert "m2@example.com" not in sent["email"]


def test_run_expiry_scan_buckets_and_expiry(db_session, monkeypatch):
    _capture(monkeypatch)
    bronze = _plan(db_session, "bronze")
    for d in (7, 3, 1, 5):  # 7/3/1 notified, 5 ignored
        u = _user(db_session, f"U{d}", plex_email=f"u{d}@example.com")
        _sub(db_session, u, bronze, d)
    expired_user = _user(db_session, "Exp", plex_email="exp@example.com")
    expired_sub = _sub(db_session, expired_user, bronze, -2)

    counts = notif.run_expiry_scan(db_session)
    assert counts["notified"] == 3
    assert counts["expired"] == 1
    db_session.refresh(expired_sub)
    assert expired_sub.status == SubscriptionStatus.expired

    # rerun -> already-sent notifications are deduped (no new logs added)
    before = len(db_session.exec(select(NotificationLog)).all())
    notif.run_expiry_scan(db_session)
    after = len(db_session.exec(select(NotificationLog)).all())
    assert before == after


def test_run_expiry_scan_respects_configured_days(db_session, monkeypatch):
    from app.services import settings_store

    _capture(monkeypatch)
    bronze = _plan(db_session, "bronze")
    # Admin sets a custom schedule: only 10 days before + 2 days overdue.
    settings_store.set_value(db_session, "reminder_days_before", "10")
    settings_store.set_value(db_session, "reminder_days_after", "2")
    for d in (10, 7, 1):  # only the 10-day sub is in the schedule now
        u = _user(db_session, f"C{d}", plex_email=f"c{d}@example.com")
        _sub(db_session, u, bronze, d)
    overdue = _user(db_session, "Ovd", plex_email="ovd@example.com")
    _sub(db_session, overdue, bronze, -2)  # 2 days overdue -> fires

    counts = notif.run_expiry_scan(db_session)
    assert counts["notified"] == 2  # the 10-day + the -2 overdue, not 7/1

    # rerun is idempotent (dedup holds for configured days too)
    before = len(db_session.exec(select(NotificationLog)).all())
    notif.run_expiry_scan(db_session)
    assert len(db_session.exec(select(NotificationLog)).all()) == before


def test_parse_days_validation():
    from app.runtime_config import _parse_days

    assert _parse_days("7,3,1", lo=1, hi=90) == [1, 3, 7]
    assert _parse_days("7, ,abc,3,3", lo=1, hi=90) == [3, 7]   # junk + dupes dropped
    assert _parse_days("0,200,5", lo=1, hi=90) == [5]          # out-of-range clamped
    assert _parse_days("none", lo=1, hi=90) == []              # sentinel = disabled
    assert _parse_days("", lo=1, hi=90) == []
    assert _parse_days(None, lo=0, hi=90) == []


def test_backup_creates_file(db_session, tmp_path, monkeypatch):
    from app.services import backup

    # point at the test DB
    dest = backup.backup_database()
    assert dest is not None and dest.exists()


def test_user_can_opt_out_of_channels(db_session, monkeypatch):
    sent = _capture(monkeypatch)
    # User opts out of email, keeps telegram
    user = _user(
        db_session, "OptOut",
        plex_email="opt@example.com", telegram_id="222",
        notify_via_email=False, notify_via_telegram=True,
    )
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 7)
    notif.notify_expiry(db_session, sub, 7)
    assert sent["email"] == []        # email suppressed
    assert sent["tg"] == ["222"]      # telegram still sent


def test_manual_reminder_resends_each_click(db_session, monkeypatch):
    # Manual "send reminder" must fire every click (managers test/resend),
    # unlike the daily scan which dedups per day.
    sent = _capture(monkeypatch)
    user = _user(
        db_session, "Manual",
        plex_email="manual@example.com", telegram_id="444",
    )
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 5)

    assert notif.notify_expiry_manual(db_session, sub) == 2  # email + telegram
    assert notif.notify_expiry_manual(db_session, sub) == 2  # again, not deduped
    assert sent["email"] == ["manual@example.com", "manual@example.com"]
    assert sent["tg"] == ["444", "444"]


def test_user_opts_out_of_telegram(db_session, monkeypatch):
    sent = _capture(monkeypatch)
    user = _user(
        db_session, "NoTg",
        plex_email="notg@example.com", telegram_id="333",
        notify_via_email=True, notify_via_telegram=False,
    )
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 3)
    notif.notify_expiry(db_session, sub, 3)
    assert sent["email"] == ["notg@example.com"]
    assert sent["tg"] == []
