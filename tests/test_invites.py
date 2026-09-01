from sqlmodel import select

import app.services.plex_oauth as po
import app.services.plex_service as plex_service
from app.models import (
    AppUser,
    Invite,
    InviteStatus,
    Renewal,
    RenewalStatus,
    Role,
)
from app.services import subscriptions as sub_svc


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _plan(session, slug):
    from app.models import Plan

    return session.exec(select(Plan).where(Plan.slug == slug)).one()


def test_admin_creates_invite(client, db_session, login_as, monkeypatch):
    calls = {}
    monkeypatch.setattr(
        plex_service,
        "invite_friend",
        lambda email, sections=None: calls.setdefault("email", email),
    )
    admin = _mk(db_session, Role.admin, "InvAdmin")
    login_as(client, admin.id)
    resp = client.post(
        "/invites",
        data={
            "email": "new@example.com",
            "real_name": "New Person",
            "role": "user",
            "manager_id": str(admin.id),
            "plan_slug": "bronze",
            "trial_days": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert calls["email"] == "new@example.com"
    db_session.commit()
    inv = db_session.exec(
        select(Invite).where(Invite.email == "new@example.com")
    ).one()
    assert inv.status == InviteStatus.pending
    assert inv.plex_invite_sent_at is not None
    assert inv.plan_id == _plan(db_session, "bronze").id


def test_admin_withdraws_pending_invite(client, db_session, login_as, monkeypatch):
    cancelled = {}
    monkeypatch.setattr(
        plex_service, "cancel_invite", lambda email: cancelled.setdefault("email", email)
    )
    admin = _mk(db_session, Role.admin, "DelAdmin")
    inv = Invite(email="bye@example.com", real_name="Bye", token="t-bye")
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert cancelled["email"] == "bye@example.com"
    inv_id = inv.id
    db_session.expunge_all()  # drop the stale instance the request session deleted
    assert db_session.get(Invite, inv_id) is None


def test_withdraw_ignores_already_accepted_invite(client, db_session, login_as, monkeypatch):
    # An accepted invite must not be deletable (and Plex is never touched).
    monkeypatch.setattr(
        plex_service, "cancel_invite", lambda email: (_ for _ in ()).throw(AssertionError)
    )
    admin = _mk(db_session, Role.admin, "KeepAdmin")
    inv = Invite(
        email="kept@example.com", real_name="Kept", token="t-kept",
        status=InviteStatus.accepted,
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    login_as(client, admin.id)
    resp = client.post(f"/invites/{inv.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    db_session.commit()
    assert db_session.get(Invite, inv.id) is not None


def test_moderator_cannot_invite(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "NoInvite")
    login_as(client, mod.id)
    resp = client.post(
        "/invites",
        data={"email": "x@example.com", "real_name": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_invite_not_persisted_when_plex_fails(client, db_session, login_as, monkeypatch):
    def _boom(email):
        raise RuntimeError("plex down")

    monkeypatch.setattr(plex_service, "invite_friend", _boom)
    admin = _mk(db_session, Role.admin, "FailAdmin")
    login_as(client, admin.id)
    resp = client.post(
        "/invites",
        data={"email": "fail@example.com", "real_name": "F", "role": "user"},
        follow_redirects=False,
    )
    assert resp.status_code == 502
    db_session.commit()
    assert (
        db_session.exec(
            select(Invite).where(Invite.email == "fail@example.com")
        ).first()
        is None
    )


def _activate_via_plex(client, db_session, monkeypatch, email, acc_id="500"):
    monkeypatch.setattr(po, "create_pin", lambda: {"id": 1, "code": "C"})
    client.get("/login/plex", follow_redirects=False)
    monkeypatch.setattr(po, "poll_pin", lambda pid: "tok")
    monkeypatch.setattr(
        po,
        "fetch_account",
        lambda t: {"id": acc_id, "email": email, "username": "newp"},
    )
    return client.get("/login/plex/callback", follow_redirects=False)


def test_activation_provisions_paid_subscription(client, db_session, monkeypatch):
    mgr = _mk(db_session, Role.admin, "Mgr")
    bronze = _plan(db_session, "bronze")
    db_session.add(
        Invite(
            email="paid@example.com",
            real_name="Paid User",
            intended_role=Role.user,
            manager_id=mgr.id,
            plan_id=bronze.id,
            token="t-paid",
        )
    )
    db_session.commit()

    resp = _activate_via_plex(client, db_session, monkeypatch, "paid@example.com")
    assert resp.status_code == 303
    db_session.commit()

    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "paid@example.com")
    ).one()
    sub = sub_svc.get_active_subscription(db_session, user.id)
    assert sub is not None and sub.plan_id == bronze.id
    renewals = db_session.exec(
        select(Renewal).where(Renewal.subscription_id == sub.id)
    ).all()
    assert len(renewals) == 1
    assert renewals[0].status == RenewalStatus.pending
    assert renewals[0].collected_by == mgr.id


def test_activation_ff_no_renewal(client, db_session, monkeypatch):
    ff = _plan(db_session, "family_friends")
    db_session.add(
        Invite(
            email="ff@example.com",
            real_name="FF User",
            intended_role=Role.user,
            plan_id=ff.id,
            token="t-ff",
        )
    )
    db_session.commit()

    _activate_via_plex(client, db_session, monkeypatch, "ff@example.com", acc_id="501")
    db_session.commit()

    user = db_session.exec(
        select(AppUser).where(AppUser.plex_email == "ff@example.com")
    ).one()
    sub = sub_svc.get_active_subscription(db_session, user.id)
    assert sub is not None and sub.expiry_at is None
    assert (
        db_session.exec(
            select(Renewal).where(Renewal.subscription_id == sub.id)
        ).first()
        is None
    )


def test_withdraw_reports_plex_failure_but_drops_invite(
    client, db_session, login_as, monkeypatch
):
    """A Plex withdraw that errors must not pass silently: the local invite goes
    (so the admin isn't stuck) but the page says Plex didn't confirm."""

    def _boom(email):
        raise RuntimeError("(401) unauthorized")

    monkeypatch.setattr(plex_service, "cancel_invite", _boom)
    monkeypatch.setattr(
        plex_service, "invite_friend", lambda email, sections=None: None
    )
    admin = _mk(db_session, Role.admin, "WdAdmin")
    login_as(client, admin.id)
    client.post(
        "/invites",
        data={"email": "wd@example.com", "real_name": "Wd", "role": "user"},
    )
    inv = db_session.exec(
        select(Invite).where(Invite.email == "wd@example.com")
    ).one()
    resp = client.post(f"/invites/{inv.id}/delete")
    assert resp.status_code == 200
    assert "401" in resp.text
    db_session.expire_all()
    assert (
        db_session.exec(
            select(Invite).where(Invite.email == "wd@example.com")
        ).first()
        is None
    )


def test_withdraw_is_clean_when_plex_has_nothing(
    client, db_session, login_as, monkeypatch
):
    """PlexShareNotFound just means the share was already gone -- not an error."""

    def _gone(email):
        raise plex_service.PlexShareNotFound(email)

    monkeypatch.setattr(plex_service, "cancel_invite", _gone)
    monkeypatch.setattr(
        plex_service, "invite_friend", lambda email, sections=None: None
    )
    admin = _mk(db_session, Role.admin, "GoneAdmin")
    login_as(client, admin.id)
    client.post(
        "/invites",
        data={"email": "gone@example.com", "real_name": "Gone", "role": "user"},
    )
    inv = db_session.exec(
        select(Invite).where(Invite.email == "gone@example.com")
    ).one()
    resp = client.post(f"/invites/{inv.id}/delete", follow_redirects=False)
    assert resp.status_code == 303


def test_invitable_roles_least_privilege_first(db_session):
    """The dropdown's first (pre-selected) option must be the safest role."""
    from app.routers.invites import _invitable_roles

    assert _invitable_roles(_mk(db_session, Role.superadmin, "OrderSuper")) == [
        Role.user,
        Role.moderator,
        Role.admin,
    ]
    assert _invitable_roles(_mk(db_session, Role.admin, "OrderAdmin")) == [
        Role.user,
        Role.moderator,
    ]
