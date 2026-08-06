"""Tests for the shared nav (desktop sidebar + mobile bottom-nav)."""
from sqlmodel import select

from app.models import AppUser, Role


def _mk(session, role, name, manager_id=None):
    u = AppUser(role=role, real_name=name, manager_id=manager_id)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def test_plans_nav_icon_is_checklist_not_workspace_premium(client, db_session, login_as):
    superadmin = db_session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    login_as(client, superadmin.id)
    resp = client.get("/profile")
    assert resp.status_code == 200
    assert "workspace_premium" not in resp.text
    assert "checklist" in resp.text


def test_bottomnav_flat_for_moderator_five_items(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminForMod")
    mod = _mk(db_session, Role.moderator, "NavMod", manager_id=admin.id)
    login_as(client, mod.id)
    resp = client.get("/profile")
    assert resp.status_code == 200
    bottomnav = resp.text.split('id="bottomnav"')[1]
    assert "bottomnav-more" not in bottomnav
    assert 'href="/users"' in bottomnav
    assert 'href="/requests"' in bottomnav
    assert 'href="/audit?tab=notifications"' in bottomnav
    assert 'href="/profile"' in bottomnav
    assert 'action="/logout"' in bottomnav


def test_bottomnav_flat_for_plain_user(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "AdminForUser")
    user = _mk(db_session, Role.user, "NavUser", manager_id=admin.id)
    login_as(client, user.id)
    resp = client.get("/profile")
    assert resp.status_code == 200
    bottomnav = resp.text.split('id="bottomnav"')[1]
    assert "bottomnav-more" not in bottomnav
    assert f'href="/users/{user.id}/subscription"' in bottomnav
    assert 'href="/profile"' in bottomnav
    assert 'action="/logout"' in bottomnav


def test_bottomnav_overflow_for_admin_eight_items(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "NavAdminOverflow")
    login_as(client, admin.id)
    resp = client.get("/profile")
    assert resp.status_code == 200
    bottomnav = resp.text.split('id="bottomnav"')[1]
    assert 'class="bottomnav-more"' in bottomnav
    before, sheet = bottomnav.split('class="bottomnav-more-sheet"', 1)
    # primary 4 restano diretti
    assert 'href="/users"' in before
    assert 'href="/requests"' in before
    assert 'href="/reports"' in before
    assert 'href="/profile"' in before
    # il resto + Logout finiscono sotto "Altro"
    assert 'href="/invites"' in sheet
    assert 'href="/broadcast"' in sheet
    assert 'href="/audit"' in sheet
    assert 'action="/logout"' in sheet
    assert 'href="/reports"' not in sheet


def test_bottomnav_overflow_for_superadmin_includes_plans_and_settings(client, db_session, login_as):
    superadmin = db_session.exec(select(AppUser).where(AppUser.role == Role.superadmin)).first()
    login_as(client, superadmin.id)
    resp = client.get("/profile")
    assert resp.status_code == 200
    bottomnav = resp.text.split('id="bottomnav"')[1]
    before, sheet = bottomnav.split('class="bottomnav-more-sheet"', 1)
    assert 'href="/plans"' in sheet
    assert 'href="/settings"' in sheet
    assert 'href="/plans"' not in before
    assert 'href="/settings"' not in before


def test_sidebar_unchanged_for_admin(client, db_session, login_as):
    admin = _mk(db_session, Role.admin, "NavAdminSidebar")
    login_as(client, admin.id)
    resp = client.get("/profile")
    assert resp.status_code == 200
    sidebar = resp.text.split('id="sidebar-nav"')[1].split("</nav>")[0]
    assert "bottomnav-more" not in sidebar
    for href in ("/users", "/requests", "/invites", "/broadcast", "/reports", "/audit"):
        assert f'href="{href}"' in sidebar
    assert "logout-btn sidebar-link" in sidebar
