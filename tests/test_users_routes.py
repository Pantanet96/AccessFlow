from sqlmodel import select

from app.models import AppUser, Role


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_users_list_requires_login(client):
    resp = client.get("/users", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_plain_user_redirected_to_profile(client, db_session, login_as):
    u = _mk(db_session, Role.user, "Plain")
    login_as(client, u.id)
    resp = client.get("/users", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/profile"


def test_moderator_sees_only_assigned(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModX")
    _mk(db_session, Role.user, "MineUser", manager_id=mod.id)
    _mk(db_session, Role.user, "OtherUser")
    login_as(client, mod.id)
    resp = client.get("/users")
    assert resp.status_code == 200
    assert "MineUser" in resp.text
    assert "OtherUser" not in resp.text


def test_admin_can_assign_manager(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminX")
    mod = _mk(db_session, Role.moderator, "ModY")
    target = _mk(db_session, Role.user, "Target")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/manager",
        data={"manager_id": str(mod.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    refreshed = db_session.get(AppUser, target.id)
    assert refreshed.manager_id == mod.id


def test_moderator_cannot_assign_manager(client, db_session, login_as):
    mod = _mk(db_session, Role.moderator, "ModZ")
    target = _mk(db_session, Role.user, "T2", manager_id=mod.id)
    login_as(client, mod.id)
    resp = client.post(
        f"/users/{target.id}/manager",
        data={"manager_id": str(mod.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_delete_orphan_protection_via_route(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminDel")
    mod = _mk(db_session, Role.moderator, "MgrToDel")
    _mk(db_session, Role.user, "StillAssigned", manager_id=mod.id)
    login_as(client, admin.id)
    resp = client.post(f"/users/{mod.id}/delete", follow_redirects=False)
    assert resp.status_code == 400
    db_session.commit()
    assert db_session.get(AppUser, mod.id).is_active is True


def test_superadmin_change_role(client, db_session, login_as):
    # log in as the seeded superadmin
    from app.models import AppUser as AU

    admin = db_session.exec(
        select(AU).where(AU.role == Role.superadmin)
    ).first()
    target = _mk(db_session, Role.user, "Promote")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/role",
        data={"role": "moderator"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    assert db_session.get(AppUser, target.id).role == Role.moderator


def test_profile_self_edit(client, db_session, login_as):
    u = _mk(db_session, Role.user, "Self")
    u.plex_email = "self@example.com"
    db_session.add(u)
    db_session.commit()
    login_as(client, u.id)
    resp = client.post(
        "/profile",
        data={
            "real_name": "Self Edited",
            "notify_email": "",
            "telegram_id": "555",
            "locale": "en",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.commit()
    refreshed = db_session.get(AppUser, u.id)
    assert refreshed.real_name == "Self Edited"
    assert refreshed.telegram_id == "555"
    assert refreshed.locale == "en"
    assert refreshed.notify_email is None


def test_manager_assign_redirects_to_next(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminNext")
    mod = _mk(db_session, Role.moderator, "ModNext")
    target = _mk(db_session, Role.user, "TargetNext")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/manager",
        data={"manager_id": str(mod.id), "next": f"/users/{target.id}/subscription"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/users/{target.id}/subscription"


def test_manager_assign_rejects_unsafe_next(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminNext2")
    target = _mk(db_session, Role.user, "TargetNext2")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/manager",
        data={"manager_id": "", "next": "//evil.com/steal"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/users"


def test_manager_assign_rejects_backslash_redirect(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminNext4")
    target = _mk(db_session, Role.user, "TargetNext4")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/manager",
        data={"manager_id": "", "next": "/\\evil.com/steal"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/users"


def test_manager_assign_default_next_unchanged(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminNext3")
    target = _mk(db_session, Role.user, "TargetNext3")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/manager",
        data={"manager_id": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/users"


def test_rename_redirects_to_next(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminRenNext")
    target = _mk(db_session, Role.user, "RenNextTarget")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/name",
        data={"real_name": "Renamed", "next": f"/users/{target.id}/subscription"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == f"/users/{target.id}/subscription"


def test_role_change_redirects_to_next(client, db_session, login_as):
    admin = db_session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    target = _mk(db_session, Role.user, "RoleNextTarget")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/role",
        data={"role": "moderator", "next": f"/users/{target.id}/subscription"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == f"/users/{target.id}/subscription"


def test_delete_user_redirects_to_next_on_success(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminDelNext")
    target = _mk(db_session, Role.user, "DelNextTarget")
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{target.id}/delete",
        data={"next": "/users/999/subscription"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/users/999/subscription"


def test_delete_orphan_error_ignores_next(client, db_session, login_as):
    # OrphanError renders the list directly (unchanged) — never a redirect,
    # so `next` must NOT apply here (deleting a manager routes you back to
    # the list to reassign their users, regardless of where you came from).
    admin = _mk(db_session, Role.admin, "AdminDel2")
    mod = _mk(db_session, Role.moderator, "MgrToDel2")
    _mk(db_session, Role.user, "StillAssigned2", manager_id=mod.id)
    login_as(client, admin.id)
    resp = client.post(
        f"/users/{mod.id}/delete",
        data={"next": "/users/1/subscription"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_users_mobile_cards_present_with_status_badges(client, db_session, login_as):
    from app.services import subscriptions as sub_svc

    admin = _mk(db_session, Role.admin, "AdminCards")
    login_as(client, admin.id)

    off_user = _mk(db_session, Role.user, "OffUser")
    off_user.is_active = False
    db_session.add(off_user)

    suspended_user = _mk(db_session, Role.user, "SuspendedUser")
    suspended_user.access_suspended = True
    db_session.add(suspended_user)
    db_session.commit()

    resp = client.get("/users")
    assert resp.status_code == 200
    assert 'id="usersCards"' in resp.text
    # una card per utente, tap verso il dettaglio
    assert f'href="/users/{off_user.id}/subscription"' in resp.text
    assert f'href="/users/{suspended_user.id}/subscription"' in resp.text
    # markup della card (non della tabella): id univoco della lista mobile
    cards_section = resp.text.split('id="usersCards"')[1].split("</ul>")[0]
    assert "badge--cancelled" in cards_section  # Off
    assert "badge--suspended" in cards_section  # Suspended


def test_users_toolbar_has_mobile_filters_disclosure(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminFilters")
    login_as(client, admin.id)
    resp = client.get("/users")
    assert resp.status_code == 200
    assert 'class="filters-details' in resp.text
    assert 'id="filterCount"' in resp.text
    # i controlli restano tutti presenti, solo raggruppati
    assert 'id="filterRole"' in resp.text
    assert 'id="sortBy"' in resp.text
