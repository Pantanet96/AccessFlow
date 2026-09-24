from datetime import timedelta

import pytest
from sqlmodel import select

import app.services.plex_service as plex_service
from app.models import AppUser, Invite, InviteStatus, Role, local_date, utcnow
from app.services import invites as inv_svc
from app.services import plex_import


def _mk(session, role, name):
    u = AppUser(role=role, real_name=name)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _invite(session, email, *, days_ago=0, manager_id=None):
    sent = utcnow() - timedelta(days=days_ago)
    inv = Invite(email=email, real_name=email.split("@")[0], token=email,
                 manager_id=manager_id, plex_invite_sent_at=sent,
                 created_at=sent, expires_at=inv_svc.new_expiry(sent))
    session.add(inv)
    session.commit()
    session.refresh(inv)
    return inv


@pytest.fixture
def plex(monkeypatch):
    calls = {"cancelled": [], "imported": 0}

    def _import(session):
        calls["imported"] += 1
        return {"activated": 0}

    monkeypatch.setattr(plex_import, "import_plex_users", _import)
    monkeypatch.setattr(plex_service, "cancel_invite", calls["cancelled"].append)
    return calls


def test_expired_invite_is_closed_and_plex_share_withdrawn(db_session, plex):
    old = _invite(db_session, "old@example.com", days_ago=31)
    fresh = _invite(db_session, "fresh@example.com", days_ago=5)

    assert inv_svc.expire_invites(db_session) == 1
    db_session.refresh(old), db_session.refresh(fresh)
    assert old.status == InviteStatus.expired
    assert fresh.status == InviteStatus.pending
    assert plex["cancelled"] == ["old@example.com"]
    # Accepted shares are activated before anything is withdrawn.
    assert plex["imported"] == 1


def test_nothing_due_does_not_touch_plex(db_session, plex):
    _invite(db_session, "fresh@example.com", days_ago=5)
    assert inv_svc.expire_invites(db_session) == 0
    assert plex["imported"] == 0


def test_invite_accepted_meanwhile_is_not_revoked(db_session, plex, monkeypatch):
    inv = _invite(db_session, "late@example.com", days_ago=31)

    def _import(session):  # the invitee accepted on Plex after the deadline
        inv.status = InviteStatus.accepted
        session.add(inv)
        session.commit()
        return {"activated": 1}

    monkeypatch.setattr(plex_import, "import_plex_users", _import)
    assert inv_svc.expire_invites(db_session) == 0
    assert plex["cancelled"] == []


def test_plex_failure_keeps_invite_pending_for_retry(db_session, plex, monkeypatch):
    inv = _invite(db_session, "stuck@example.com", days_ago=31)

    def _boom(email):
        raise RuntimeError("plex.tv down")

    monkeypatch.setattr(plex_service, "cancel_invite", _boom)
    assert inv_svc.expire_invites(db_session) == 0
    db_session.refresh(inv)
    assert inv.status == InviteStatus.pending


def test_extend_and_home_page_scoping(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "Mod")
    other = _mk(db_session, Role.moderator, "Other")
    mine = _invite(db_session, "mine@example.com", days_ago=25, manager_id=mod.id)
    _invite(db_session, "theirs@example.com", days_ago=25, manager_id=other.id)

    login_as(client, mod.id)
    client.cookies.set("locale", "en")
    home = client.get("/").text
    assert "mine@example.com" in home and "theirs@example.com" not in home
    assert "5 day(s) left" in home

    resp = client.post(f"/invites/{mine.id}/extend", data={"next": "/"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    db_session.refresh(mine)
    assert inv_svc.days_left(mine) == inv_svc.INVITE_DAYS

    theirs = db_session.exec(select(Invite).where(Invite.email == "theirs@example.com")).one()
    assert client.post(f"/invites/{theirs.id}/extend").status_code == 403


def test_invites_page_shows_history(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "Adm")
    done = _invite(db_session, "done@example.com")
    done.status = InviteStatus.expired
    db_session.add(done)
    db_session.commit()

    login_as(client, admin.id)
    client.cookies.set("locale", "en")
    page = client.get("/invites").text
    assert "Invite history" in page and "done@example.com" in page


def test_invite_email_states_the_expiry(db_session, monkeypatch):
    from app.services import mail_service, notifications

    sent = {}
    monkeypatch.setattr(mail_service, "send_email",
                        lambda to, subject, text, html=None: sent.update(html=html) or True)
    inv = _invite(db_session, "mail@example.com")
    notifications.notify_invite(db_session, inv)
    assert local_date(inv.expires_at).isoformat() in sent["html"]
