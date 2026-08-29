"""Invite email: rendering, sending on create, resend, and its log row."""
import json

from sqlmodel import select

import app.services.plex_service as plex_service
from app.models import (
    AppUser,
    Invite,
    InviteStatus,
    NotificationLog,
    NotificationType,
    Role,
)
from app.services import notification_templates as nt
from app.services import notifications, settings_store


def _mk(session, role, name):
    u = AppUser(role=role, real_name=name)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _smtp_on(session):
    settings_store.set_value(session, "smtp_host", "smtp.example.com")
    settings_store.set_value(session, "smtp_from", "no-reply@example.com")


def _invite(session, email="new@example.com", **kw):
    inv = Invite(
        email=email,
        real_name=kw.pop("real_name", "New User"),
        token=kw.pop("token", "tok-" + email),
        status=InviteStatus.pending,
        libraries=json.dumps(kw.pop("libraries", ["Film"])),
        **kw,
    )
    session.add(inv)
    session.commit()
    session.refresh(inv)
    return inv


def test_invite_email_spells_out_both_plex_paths(db_session, monkeypatch):
    """The copy has to serve someone with no Plex account -- the common case."""
    sent = {}
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: sent.update(
            to=to, subject=subject, body=body, html=html
        )
        or True,
    )
    settings_store.set_value(db_session, "public_base_url", "https://af.example.com")
    inv = _invite(db_session)
    assert notifications.notify_invite(db_session, inv) is True
    assert sent["to"] == "new@example.com"
    html = sent["html"]
    # Both routes in, and the sign-in link built from the configured domain.
    assert "https://af.example.com/login" in html
    assert "plex.tv" in html
    # The address constraint: signing up under another email breaks activation.
    assert "new@example.com" in html
    assert html.count("<ol>") == 2  # already-has-Plex, and create-one-first


def test_invite_email_logged_against_the_invite_not_a_user(db_session, monkeypatch):
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: True,
    )
    inv = _invite(db_session, email="logged@example.com")
    notifications.notify_invite(db_session, inv)
    row = db_session.exec(
        select(NotificationLog).where(NotificationLog.type == NotificationType.invite)
    ).one()
    assert row.user_id is None  # no AppUser exists yet
    assert row.invite_id == inv.id


def test_second_send_is_deduped_but_resend_is_not(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: calls.append(to) or True,
    )
    inv = _invite(db_session, email="dedup@example.com")
    assert notifications.notify_invite(db_session, inv) is True
    assert notifications.notify_invite(db_session, inv) is False  # same key
    assert notifications.notify_invite(db_session, inv, resend=True) is True
    assert len(calls) == 2


def test_dedup_key_survives_sqlite_reusing_an_invite_id(db_session, monkeypatch):
    """Withdrawing an invite frees its rowid; the next invite can inherit it.
    Keying on the invite token keeps that from swallowing the new email."""
    calls = []
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: calls.append(to) or True,
    )
    first = _invite(db_session, email="first@example.com", token="tok-first")
    notifications.notify_invite(db_session, first)
    reused_id = first.id
    db_session.delete(first)
    db_session.commit()
    second = Invite(
        id=reused_id,  # what SQLite hands out again
        email="second@example.com",
        real_name="Second",
        token="tok-second",
        status=InviteStatus.pending,
    )
    db_session.add(second)
    db_session.commit()
    assert notifications.notify_invite(db_session, second) is True
    assert calls == ["first@example.com", "second@example.com"]


def test_create_invite_sends_the_email(client, db_session, login_as, monkeypatch):
    monkeypatch.setattr(
        plex_service, "invite_friend", lambda email, sections=None: None
    )
    sent = {}
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: sent.update(to=to) or True,
    )
    _smtp_on(db_session)
    admin = _mk(db_session, Role.admin, "MailAdmin")
    login_as(client, admin.id)
    resp = client.post(
        "/invites",
        data={
            "email": "flow@example.com",
            "real_name": "Flow",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert sent["to"] == "flow@example.com"


def test_create_invite_survives_a_broken_smtp(client, db_session, login_as, monkeypatch):
    """A dead mail server must not undo a Plex share that already exists."""
    monkeypatch.setattr(
        plex_service, "invite_friend", lambda email, sections=None: None
    )

    def _boom(to, subject, body, html=None):
        raise OSError("connection refused")

    monkeypatch.setattr(notifications.mail_service, "send_email", _boom)
    _smtp_on(db_session)
    admin = _mk(db_session, Role.admin, "BrokenSmtpAdmin")
    login_as(client, admin.id)
    resp = client.post(
        "/invites",
        data={"email": "kept@example.com", "real_name": "Kept", "role": "user"},
    )
    # The admin is told the mail didn't go out...
    assert resp.status_code == 200
    assert "SMTP" in resp.text
    # ...the invite (and the Plex share behind it) stays...
    inv = db_session.exec(
        select(Invite).where(Invite.email == "kept@example.com")
    ).first()
    assert inv is not None
    # ...and the reason is in the notification history, like any other failure.
    row = db_session.exec(
        select(NotificationLog).where(NotificationLog.invite_id == inv.id)
    ).one()
    assert row.status == "failed"
    assert "connection refused" in row.error


def test_resend_button_sends_again(client, db_session, login_as, monkeypatch):
    calls = []
    monkeypatch.setattr(
        plex_service, "invite_friend", lambda email, sections=None: None
    )
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: calls.append(to) or True,
    )
    _smtp_on(db_session)
    admin = _mk(db_session, Role.admin, "ResendAdmin")
    login_as(client, admin.id)
    client.post(
        "/invites",
        data={"email": "again@example.com", "real_name": "Again", "role": "user"},
    )
    inv = db_session.exec(
        select(Invite).where(Invite.email == "again@example.com")
    ).one()
    resp = client.post(f"/invites/{inv.id}/resend")
    assert resp.status_code == 200
    assert calls == ["again@example.com", "again@example.com"]


def test_invite_type_is_email_only_in_the_editor(db_session):
    """No Telegram part: the invitee has no bot link until after first sign-in."""
    assert nt.parts_for("invite") == ("email_subject", "email_html")
    entry = next(
        e for e in nt.editor_entries(db_session) if e["type"] == "invite"
    )
    assert {r["part"] for r in entry["rows"]} == {"email_subject", "email_html"}


def test_admin_override_replaces_the_default_copy(db_session, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        notifications.mail_service,
        "send_email",
        lambda to, subject, body, html=None: sent.update(subject=subject) or True,
    )
    settings_store.set_value(
        db_session, "ntpl:invite:email_subject:it", "Ciao {{ name }}, entra"
    )
    settings_store.set_value(
        db_session, "ntpl:invite:email_subject:en", "Ciao {{ name }}, entra"
    )
    inv = _invite(db_session, email="custom@example.com", real_name="Custom")
    notifications.notify_invite(db_session, inv)
    assert sent["subject"] == "Ciao Custom, entra"
