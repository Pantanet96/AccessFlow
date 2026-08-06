"""Sent-notifications history: failure logging + the read/filter service."""
from datetime import timedelta

from sqlmodel import select

import app.services.mail_service as mail_service
import app.services.telegram_service as telegram_service
from app.models import (
    AppUser,
    NotificationChannel,
    NotificationLog,
    NotificationType,
    Plan,
    Role,
    Subscription,
    SubscriptionStatus,
    utcnow,
)
from app.services import audit
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


def _logs(session, user_id):
    return session.exec(
        select(NotificationLog).where(NotificationLog.user_id == user_id)
    ).all()


def test_email_failure_is_logged_and_does_not_block_retry(db_session, monkeypatch):
    def boom(*a, **k):
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    monkeypatch.setattr(mail_service, "send_email", boom)
    user = _user(
        db_session, "Fail", plex_email="fail@example.com", notify_via_telegram=False
    )
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 7)

    notif.notify_expiry(db_session, sub, 7)
    rows = _logs(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].channel == NotificationChannel.email
    assert "ConnectionRefusedError" in rows[0].error

    # Email recovers -> the retry sends; the earlier failed row must NOT dedup it.
    monkeypatch.setattr(mail_service, "send_email", lambda *a, **k: True)
    notif.notify_expiry(db_session, sub, 7)
    assert sorted(r.status for r in _logs(db_session, user.id)) == ["failed", "sent"]


def test_telegram_failure_is_logged_without_raw_reason(db_session, monkeypatch):
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: False)
    user = _user(db_session, "TgFail", telegram_id="555", notify_via_email=False)
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 7)

    notif.notify_expiry(db_session, sub, 7)
    rows = _logs(db_session, user.id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].channel == NotificationChannel.telegram
    assert "Telegram" in rows[0].error


def test_email_unconfigured_writes_no_failure_row(db_session):
    # Real send_email returns False (no SMTP host in tests) -> a choice, not a fault.
    user = _user(
        db_session, "NoSmtp", plex_email="nosmtp@example.com", notify_via_telegram=False
    )
    sub = _sub(db_session, user, _plan(db_session, "bronze"), 7)

    notif.notify_expiry(db_session, sub, 7)
    assert _logs(db_session, user.id) == []


def test_list_notifications_scopes_to_manager(db_session, monkeypatch):
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    mgr = AppUser(role=Role.moderator, real_name="Mgr")
    db_session.add(mgr)
    db_session.commit()
    db_session.refresh(mgr)
    mine = _user(
        db_session, "Mine", telegram_id="1", notify_via_email=False, manager_id=mgr.id
    )
    other = _user(db_session, "Other", telegram_id="2", notify_via_email=False)
    notif.notify_expiry(db_session, _sub(db_session, mine, _plan(db_session, "bronze"), 7), 7)
    notif.notify_expiry(db_session, _sub(db_session, other, _plan(db_session, "bronze"), 7), 7)

    scoped = audit.list_notifications(db_session, for_manager_id=mgr.id)
    assert {r["recipient"] for r in scoped} == {"Mine"}

    everyone = {r["recipient"] for r in audit.list_notifications(db_session)}
    assert {"Mine", "Other"} <= everyone


def test_list_notifications_filters_user_type_status(db_session, monkeypatch):
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    monkeypatch.setattr(mail_service, "send_email", lambda *a, **k: True)
    u1 = _user(db_session, "U1", plex_email="u1@example.com", telegram_id="1")
    u2 = _user(db_session, "U2", plex_email="u2@example.com", telegram_id="2")
    notif.notify_expiry(db_session, _sub(db_session, u1, _plan(db_session, "bronze"), 7), 7)
    notif.notify_welcome(
        db_session, u2, _plan(db_session, "bronze"),
        _sub(db_session, u2, _plan(db_session, "bronze"), 30),
    )

    only_u1 = audit.list_notifications(db_session, user_id=u1.id)
    assert only_u1 and all(r["recipient"] == "U1" for r in only_u1)

    welcomes = audit.list_notifications(db_session, ntype=NotificationType.welcome)
    assert welcomes and all(r["type_key"] == "welcome" for r in welcomes)

    sent = audit.list_notifications(db_session, status="sent")
    assert sent and all(r["status"] == "sent" for r in sent)


def test_notification_recipients_scoped(db_session, monkeypatch):
    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    mgr = AppUser(role=Role.moderator, real_name="Boss")
    db_session.add(mgr)
    db_session.commit()
    db_session.refresh(mgr)
    mine = _user(db_session, "Mine", telegram_id="1", notify_via_email=False, manager_id=mgr.id)
    other = _user(db_session, "Other", telegram_id="2", notify_via_email=False)
    notif.notify_expiry(db_session, _sub(db_session, mine, _plan(db_session, "bronze"), 7), 7)
    notif.notify_expiry(db_session, _sub(db_session, other, _plan(db_session, "bronze"), 7), 7)

    scoped = audit.notification_recipients(db_session, for_manager_id=mgr.id)
    assert set(scoped.values()) == {"Mine"}
    assert set(audit.notification_recipients(db_session).values()) >= {"Mine", "Other"}


def test_notification_retention_clamp(db_session):
    from app import runtime_config
    from app.services import settings_store

    # Never configured -> 30 days, not "keep forever".
    assert runtime_config.notification_retention_days() == 30

    # 1 day is allowed: there is no 30-day floor any more.
    settings_store.set_value(db_session, "notification_retention_days", "1")
    assert runtime_config.notification_retention_days() == 1

    # 0 stays reachable, but only by asking for it explicitly.
    settings_store.set_value(db_session, "notification_retention_days", "0")
    assert runtime_config.notification_retention_days() == 0

    # Negatives collapse to "keep forever"; the upper bound still clamps.
    settings_store.set_value(db_session, "notification_retention_days", "-5")
    assert runtime_config.notification_retention_days() == 0
    settings_store.set_value(db_session, "notification_retention_days", "99999")
    assert runtime_config.notification_retention_days() == 3650

    # A hand-corrupted row must not start deleting history.
    settings_store.set_value(db_session, "notification_retention_days", "abc")
    assert runtime_config.notification_retention_days() == 0


def test_notification_prune_respects_window_and_keeps_welcome(db_session, monkeypatch):
    from app.services import settings_store

    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    old = _user(db_session, "Old", telegram_id="9", notify_via_email=False)
    notif.notify_expiry(db_session, _sub(db_session, old, _plan(db_session, "bronze"), 7), 7)
    orow = db_session.exec(
        select(NotificationLog).where(NotificationLog.user_id == old.id)
    ).one()
    orow.sent_at = utcnow() - timedelta(days=40)
    db_session.add(orow)
    db_session.commit()

    fresh = _user(db_session, "Fresh", telegram_id="8", notify_via_email=False)
    notif.notify_expiry(db_session, _sub(db_session, fresh, _plan(db_session, "bronze"), 7), 7)

    # Explicit 0 -> keep everything.
    settings_store.set_value(db_session, "notification_retention_days", "0")
    assert notif.prune_old_notifications(db_session) == 0

    # Only the 40-day-old row exceeds a 30-day window; the fresh row survives.
    settings_store.set_value(db_session, "notification_retention_days", "30")
    assert notif.prune_old_notifications(db_session) == 1
    assert _logs(db_session, old.id) == []
    assert len(_logs(db_session, fresh.id)) == 1


def test_notification_prune_never_deletes_welcome(db_session, monkeypatch):
    """`welcome:{user_id}` has no date in it, so pruning it would let a
    re-activated user get a second welcome message."""
    from app.services import settings_store

    monkeypatch.setattr(telegram_service, "send_message", lambda *a, **k: True)
    user = _user(db_session, "Welcomed", telegram_id="7", notify_via_email=False)
    plan = _plan(db_session, "bronze")
    sub = _sub(db_session, user, plan, 30)
    notif.notify_welcome(db_session, user, plan, sub)
    notif.notify_expiry(db_session, _sub(db_session, user, plan, 7), 7)

    # Age every row well past the window.
    for row in db_session.exec(
        select(NotificationLog).where(NotificationLog.user_id == user.id)
    ).all():
        row.sent_at = utcnow() - timedelta(days=400)
        db_session.add(row)
    db_session.commit()

    settings_store.set_value(db_session, "notification_retention_days", "1")
    notif.prune_old_notifications(db_session)

    kinds = {r.type for r in _logs(db_session, user.id)}
    assert NotificationType.welcome in kinds
    assert NotificationType.expiry_reminder not in kinds
