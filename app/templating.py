"""Shared Jinja2Templates instance (i18n installed)."""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import __version__
from app.i18n import gettext as _, install_jinja_i18n

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
install_jinja_i18n(templates)

# App version, available to every template as {{ app_version }} (see base.html footer).
templates.env.globals["app_version"] = __version__


def _currency() -> dict:
    # Lazy import: runtime_config -> db/settings_store, none of which import this
    # module, but keep it out of the top-level import graph to be safe.
    from app import runtime_config
    return runtime_config.currency()


def _color_theme() -> str:
    # Lazy import, same reasoning as _currency() above.
    from app import runtime_config
    return runtime_config.color_theme()


def _fmt_money(value: float) -> str:
    c = _currency()
    n = f"{value:.2f}"
    sym, pos = c["symbol"], c["position"]
    if not sym:  # "no currency" option: bare number
        return n
    return f"{sym}{n}" if pos == "prefix" else f"{n} {sym}"


def _euros(cents) -> str:  # name kept for existing `| euros` call sites
    if cents is None:
        return "—"
    return _fmt_money(cents / 100)


def _money(amount) -> str:  # value already in major units (e.g. amount_eur)
    if amount is None:
        return "—"
    return _fmt_money(float(amount))


def _date(value) -> str:
    return value.strftime("%Y-%m-%d") if value else "∞"


def _datetime(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def _active_broadcast(current_user):
    """Latest broadcast (role-matched) the given user hasn't dismissed yet, or
    None. Runs on every page render for a logged-in user, so it opens its own
    short-lived session rather than threading one through every route."""
    if current_user is None:
        return None
    from sqlalchemy import or_
    from sqlmodel import Session, select

    from app.db import engine
    from app.models import Broadcast

    with Session(engine) as session:
        stmt = (
            select(Broadcast)
            .where(Broadcast.id > (current_user.dismissed_broadcast_id or 0))
            .where(or_(Broadcast.only_role.is_(None), Broadcast.only_role == current_user.role))
            .order_by(Broadcast.id.desc())
        )
        return session.exec(stmt).first()


_BOTTOM_NAV_PRIMARY = {"/users", "/requests", "/reports", "/profile"}


def nav_items(u) -> list[tuple[str, str, str]]:
    """Ordered (href, icon, label) tuples for a user's nav — shared by the
    desktop sidebar (always flat) and the mobile bottom-nav (flat, or split
    into primary/overflow above a role-item-count threshold). Excludes
    Logout, which is a POST form rendered separately by both callers."""
    from app.models import Role

    items: list[tuple[str, str, str]] = []
    if u.role != Role.user:
        items.append(("/users", "group", _("Users")))
        items.append(("/requests", "inventory_2", _("Collect")))
        if u.role in (Role.superadmin, Role.admin):
            items += [
                ("/invites", "mail", _("Invites")),
                ("/broadcast", "campaign", _("Broadcast")),
                ("/reports", "bar_chart", _("Reports")),
                ("/audit", "history", _("Audit log")),
            ]
        if u.role == Role.superadmin:
            items.append(("/plans", "checklist", _("Plans")))
        if u.role == Role.moderator:
            items.append(("/audit?tab=notifications", "notifications", _("Sent notifications")))
    else:
        items.append((f"/users/{u.id}/subscription", "account_circle", _("My subscription")))
    items.append(("/profile", "person", _("Profile")))
    if u.role == Role.superadmin:
        items.append(("/settings", "settings", _("Settings")))
    return items


def nav_primary(u) -> list[tuple[str, str, str]]:
    """Bottom-nav only: items that stay directly visible even when the role
    has enough items to trigger the "Altro" overflow (see nav_overflow)."""
    return [it for it in nav_items(u) if it[0] in _BOTTOM_NAV_PRIMARY]


def nav_overflow(u) -> list[tuple[str, str, str]]:
    """Bottom-nav only: items that move under "Altro" for roles with more
    than 5 total nav items (4 primary + Logout counts as the 5th)."""
    return [it for it in nav_items(u) if it[0] not in _BOTTOM_NAV_PRIMARY]


templates.env.globals["nav_items"] = nav_items
templates.env.globals["nav_primary"] = nav_primary
templates.env.globals["nav_overflow"] = nav_overflow
templates.env.globals["active_broadcast"] = _active_broadcast
templates.env.filters["euros"] = _euros
templates.env.filters["money"] = _money
templates.env.filters["date"] = _date
templates.env.filters["datetime"] = _datetime
# Currency code (e.g. "USD", "" when none) for labels/chart/CSV headers.
templates.env.globals["currency_code"] = lambda: _currency()["code"]
# Selected color theme ("rame"/"inchiostro"/"muschio"), written onto
# <html data-theme="..."> in base.html so every page (not just Settings)
# picks up the SuperAdmin's chosen palette.
templates.env.globals["color_theme"] = _color_theme
