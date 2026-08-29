"""Orchestrates access state across Plex + Overseerr.

State mirrors AccessFlow:
- active   -> Plex libraries shared,   Overseerr permissions restored
- suspended-> Plex libraries removed,  Overseerr permissions = 0 (history kept)
- removed  -> removeFriend on Plex,    Overseerr user deleted
External calls are best-effort: failures are swallowed so one outage doesn't
break the flow (use the Sync action / next reconcile to retry).
"""
import json
import logging
from datetime import datetime, timedelta

from sqlmodel import Session, select

from app import runtime_config
from app.models import AppUser, Plan, utcnow
from app.services import overseerr_service, plex_service, settings_store
from app.services.subscriptions import (
    get_active_subscription,
    get_current_subscription,
)

log = logging.getLogger("pum.access")

# Trial users may browse Overseerr but never request: permission 0 = login +
# view only. ponytail: hard-coded; promote to an `overseerr_trial_permissions`
# setting if a trial should ever be allowed to vote / create issues.
TRIAL_OV_PERMISSIONS = 0


def _parse(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else None
    except (ValueError, TypeError):
        return None


def libraries_for(session: Session, user: AppUser) -> list[str]:
    """Precedence: user override -> active plan's libraries -> global default."""
    user_libs = _parse(user.shared_libraries)
    if user_libs is not None:
        return user_libs
    sub = get_active_subscription(session, user.id)
    if sub is not None:
        plan = session.get(Plan, sub.plan_id)
        plan_libs = _parse(plan.libraries) if plan else None
        if plan_libs is not None:
            return plan_libs
    return runtime_config.plex_default_sections()


def _safe(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("access action failed: %s", exc)
        return False


# ---- Overseerr helpers ----

def _ov_disable(session: Session, user: AppUser) -> None:
    if not overseerr_service.enabled() or not user.plex_account_id:
        return
    try:
        ou = overseerr_service.find_user(plex_id=user.plex_account_id, email=user.plex_email)
    except Exception as exc:  # noqa: BLE001
        log.warning("overseerr find failed: %s", exc)
        return
    if not ou:
        return
    current = ou.get("permissions") or 0
    if current and current != 0:
        user.overseerr_prev_permissions = current
        session.add(user)
        session.commit()
    _safe(overseerr_service.set_permissions, ou["id"], 0)


def _desired_ov_permissions(session: Session, user: AppUser) -> int:
    """Overseerr permission bitmask the user should hold, decided by their active
    plan: a trial is view-only (no requests); everyone else gets their saved
    permissions, or the configured default."""
    sub = get_active_subscription(session, user.id)
    if sub is not None:
        plan = session.get(Plan, sub.plan_id)
        if plan is not None and plan.is_trial:
            return TRIAL_OV_PERMISSIONS
    return (
        user.overseerr_prev_permissions
        or runtime_config.overseerr_config()["default_permissions"]
    )


def _ov_enable(session: Session, user: AppUser) -> None:
    if not overseerr_service.enabled() or not user.plex_account_id:
        return
    try:
        ou = overseerr_service.find_user(plex_id=user.plex_account_id, email=user.plex_email)
    except Exception as exc:  # noqa: BLE001
        log.warning("overseerr find failed: %s", exc)
        return
    if not ou:
        return
    _safe(overseerr_service.set_permissions, ou["id"], _desired_ov_permissions(session, user))


def _ov_delete(user: AppUser) -> None:
    if not overseerr_service.enabled() or not user.plex_account_id:
        return
    try:
        ou = overseerr_service.find_user(plex_id=user.plex_account_id, email=user.plex_email)
    except Exception as exc:  # noqa: BLE001
        log.warning("overseerr find failed: %s", exc)
        return
    if ou:
        _safe(overseerr_service.delete_user, ou["id"])


# ---- Public actions ----

def suspend(session: Session, user: AppUser) -> None:
    user.access_suspended = True
    session.add(user)
    session.commit()
    if user.plex_email:
        _safe(plex_service.unshare, user.plex_email)
    _ov_disable(session, user)


def reactivate(session: Session, user: AppUser) -> None:
    user.access_suspended = False
    session.add(user)
    session.commit()
    if user.plex_email:
        _safe(plex_service.share, user.plex_email, libraries_for(session, user))
    _ov_enable(session, user)


def sync_overseerr_permissions(session: Session, user: AppUser) -> None:
    """Re-assert the user's Overseerr permissions from their plan (trial =
    view-only). Best-effort; skips suspended users (they stay at 0) and no-ops
    when Overseerr is off or the user isn't in Overseerr yet."""
    if user.access_suspended:
        return
    _ov_enable(session, user)


def remove_from_plex(session: Session, user: AppUser) -> None:
    if user.plex_email:
        _safe(plex_service.remove_friend, user.plex_email)
    _ov_delete(user)


def grant_overseerr(session: Session, user: AppUser) -> None:
    """Ensure the user exists in Overseerr and holds their plan's permissions
    (trial = view-only, paid/F&F = full). Called on invite-accept / activation so
    a new user can use Overseerr immediately. Best-effort; no-op when Overseerr is
    off or the user has no Plex account id yet."""
    if not overseerr_service.enabled() or not user.plex_account_id:
        return
    _safe(overseerr_service.import_from_plex, [user.plex_account_id])
    _ov_enable(session, user)


def remove_from_overseerr(user: AppUser) -> None:
    """Delete the user from Overseerr, revoking all access. Called when a user is
    removed from AccessFlow. Best-effort; no-op when Overseerr is off."""
    _ov_delete(user)


def apply_libraries(session: Session, user: AppUser, titles: list[str]) -> None:
    user.shared_libraries = json.dumps(titles)
    session.add(user)
    session.commit()
    if user.is_active and not user.access_suspended and user.plex_email:
        _safe(plex_service.share, user.plex_email, titles)


def library_drift(session: Session) -> dict:
    """Warn-only diff between the live Plex libraries and what the app references.

    Returns {"new_on_plex": [...], "stale_refs": [...]} — empty dict when there's
    nothing to report or Plex is unreachable. `new_on_plex` = libraries that exist
    on Plex but no default/plan/override shares yet (you added them on Plex — add
    them to a plan/default if you want them shared). `stale_refs` = titles still
    referenced in config that no longer exist on Plex (you removed/renamed them).
    Never changes any share — the admin decides.
    """
    try:
        live = {s["title"] for s in plex_service.list_sections()}
    except Exception as exc:  # noqa: BLE001  (Plex off / unreachable)
        log.debug("library_drift skipped: %s", exc)
        return {}
    referenced: set[str] = set(runtime_config.plex_default_sections())
    for plan in session.exec(select(Plan)).all():
        referenced |= set(_parse(plan.libraries) or [])
    for u in session.exec(select(AppUser).where(AppUser.is_active.is_(True))).all():
        referenced |= set(_parse(u.shared_libraries) or [])
    # Only flag "new" libraries when SOME explicit config exists; with nothing
    # configured everything is the implicit "all", so there's nothing to add.
    new_on_plex = sorted(live - referenced) if referenced else []
    stale_refs = sorted(referenced - live)
    if not new_on_plex and not stale_refs:
        return {}
    return {"new_on_plex": new_on_plex, "stale_refs": stale_refs}


def prune_stale_refs(session: Session, live: set[str]) -> int:
    """Drop config references to libraries that no longer exist on Plex (global
    default + every plan + every user override). Clears the library_drift warning.

    Behaviour-preserving: share()/invite already ignore dead titles, so removing
    them changes no real access.
    ponytail: if pruning empties the *global default*, new invites then share ALL
    libraries — re-pick a restricted default in Settings if that matters.
    """
    removed = 0

    def _clean(libs: list[str] | None) -> tuple[list[str], int] | None:
        if libs is None:
            return None
        kept = [t for t in libs if t in live]
        return (kept, len(libs) - len(kept)) if len(kept) != len(libs) else None

    res = _clean(runtime_config.plex_default_sections())
    if res is not None:
        settings_store.set_value(session, "plex_default_sections", json.dumps(res[0]))
        removed += res[1]
    for plan in session.exec(select(Plan)).all():
        res = _clean(_parse(plan.libraries))
        if res is not None:
            plan.libraries = json.dumps(res[0])
            session.add(plan)
            removed += res[1]
    for u in session.exec(select(AppUser)).all():
        res = _clean(_parse(u.shared_libraries))
        if res is not None:
            u.shared_libraries = json.dumps(res[0])
            session.add(u)
            removed += res[1]
    if removed:
        session.commit()
    return removed


def resync_libraries(session: Session, *, only_user_id: int | None = None) -> dict:
    """Re-apply each managed user's *configured* libraries to Plex, so a change to
    a plan/default/override propagates without a manual reactivation. Also prunes
    config references to libraries deleted on Plex (clears the drift warning).

    Users on the implicit "all" (no explicit library list) are left untouched —
    we never auto-share newly added Plex libraries (warn-only, see library_drift).
    Dead titles are dropped. Best-effort; no-op when Plex is unreachable.
    Returns {"updated", "skipped", "pruned"}.
    """
    try:
        live = {s["title"] for s in plex_service.list_sections()}
    except Exception as exc:  # noqa: BLE001
        log.debug("resync_libraries skipped: %s", exc)
        return {"updated": 0, "skipped": 0, "pruned": 0}
    pruned = prune_stale_refs(session, live)
    stmt = select(AppUser).where(AppUser.is_active.is_(True))
    if only_user_id is not None:
        stmt = stmt.where(AppUser.id == only_user_id)
    updated = skipped = 0
    for u in session.exec(stmt).all():
        if u.access_suspended or not u.plex_email:
            skipped += 1
            continue
        desired = [t for t in libraries_for(session, u) if t in live]
        if not desired:  # empty config (=all) or all titles stale -> leave alone
            skipped += 1
            continue
        try:
            current = set(plex_service.get_user_sections(u.plex_email))
        except Exception:  # noqa: BLE001
            current = None
        if current is not None and set(desired) == current:
            skipped += 1
            continue
        if _safe(plex_service.share, u.plex_email, desired):
            updated += 1
        else:
            skipped += 1
    return {"updated": updated, "skipped": skipped, "pruned": pruned}


def reconcile_all(session: Session, now: datetime | None = None) -> int:
    """Auto-suspend users whose subscription expired beyond their grace_days."""
    now = now or utcnow()
    users = session.exec(
        select(AppUser).where(AppUser.is_active.is_(True))
    ).all()
    suspended = 0
    for user in users:
        if user.access_suspended or not user.plex_email:
            continue
        # active OR expired: run_expiry_scan flips a just-lapsed sub to `expired`
        # the same day, so an active-only lookup would never see it here and the
        # user would keep full access forever. See get_current_subscription.
        sub = get_current_subscription(session, user.id)
        if sub is None:
            continue
        plan = session.get(Plan, sub.plan_id)
        # Keep trial users view-only in Overseerr: they may have picked up
        # request permissions on their first Overseerr login, so re-assert daily.
        # ponytail: O(trial users) API calls/day, fine for a personal server.
        if plan is not None and plan.is_trial:
            _ov_enable(session, user)
        if sub.expiry_at is None:
            continue
        if now > sub.expiry_at + timedelta(days=user.grace_days):
            suspend(session, user)
            suspended += 1
    return suspended
