"""User management: scoped listing, manager assignment, soft-delete, profile."""
import re

from sqlmodel import Session, select

from app.models import (
    AppUser, Renewal, RenewalStatus, Role, Subscription, SubscriptionStatus,
)

_VALID_LOCALES = ("it", "en")
# A single, well-formed address: no spaces/commas, so it can't smuggle extra
# recipients into the To header (send_message parses it) or store junk that
# throws at send time. Deliberately simple, not a full RFC 5322 validator.
# The domain labels exclude "." so the pattern stays unambiguous: with "." in
# both classes around it, a long dotted non-match backtracks quadratically.
_EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;.]+(?:\.[^@\s,;.]+)+$")


def valid_notify_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value))


class OrphanError(Exception):
    """Raised when deleting a manager that still has active assigned users."""


def list_users_for(session: Session, viewer: AppUser) -> list[AppUser]:
    if viewer.role in (Role.superadmin, Role.admin):
        stmt = select(AppUser).order_by(AppUser.real_name)
    elif viewer.role == Role.moderator:
        stmt = (
            select(AppUser)
            .where(AppUser.manager_id == viewer.id)
            .order_by(AppUser.real_name)
        )
    else:
        return []
    return list(session.exec(stmt).all())


def manager_candidates(session: Session) -> list[AppUser]:
    stmt = (
        select(AppUser)
        .where(AppUser.role.in_([Role.admin, Role.moderator]))
        .where(AppUser.is_active.is_(True))
        .order_by(AppUser.real_name)
    )
    return list(session.exec(stmt).all())


def assigned_active_count(session: Session, manager_id: int) -> int:
    stmt = (
        select(AppUser)
        .where(AppUser.manager_id == manager_id)
        .where(AppUser.is_active.is_(True))
    )
    return len(session.exec(stmt).all())


def assign_manager(
    session: Session, user: AppUser, manager_id: int | None
) -> AppUser:
    user.manager_id = manager_id
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def soft_delete(session: Session, user: AppUser) -> None:
    if user.role in (Role.admin, Role.moderator):
        if assigned_active_count(session, user.id) > 0:
            raise OrphanError()
    user.is_active = False
    # Revoke live cookies now, not just while inactive: reactivating the row
    # would otherwise make every pre-deletion session valid again.
    user.session_gen = (user.session_gen or 0) + 1
    session.add(user)
    # Nothing left to bill or remind: the expiry scan, reports and /requests
    # key on the subscription, not on is_active.
    subs = session.exec(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .where(Subscription.status != SubscriptionStatus.cancelled)
    ).all()
    for sub in subs:
        sub.status = SubscriptionStatus.cancelled
        session.add(sub)
        pending = session.exec(
            select(Renewal)
            .where(Renewal.subscription_id == sub.id)
            .where(Renewal.status == RenewalStatus.pending)
        ).all()
        for renewal in pending:
            session.delete(renewal)
    session.commit()


def rename(session: Session, user: AppUser, real_name: str) -> AppUser:
    """Manager-side display-name edit. Blank is ignored (keeps the current name,
    which defaults to the imported Plex username)."""
    name = (real_name or "").strip()
    if name:
        user.real_name = name
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


def telegram_id_taken(session: Session, chat_id: str, user_id: int) -> bool:
    """Another account already holds this chat: the bot link flow refuses it
    (one chat must not receive or control two accounts), manual entry too."""
    return session.exec(
        select(AppUser).where(AppUser.telegram_id == chat_id, AppUser.id != user_id)
    ).first() is not None


def update_profile(
    session: Session,
    user: AppUser,
    *,
    real_name: str | None = None,
    notify_email: str | None = None,
    telegram_id: str | None = None,
    locale: str | None = None,
    notify_via_email: bool | None = None,
    notify_via_telegram: bool | None = None,
    digest_enabled: bool | None = None,
    digest_weekday: int | None = None,
) -> AppUser:
    if real_name:
        user.real_name = real_name
    # Validate the notify address: blank clears it, a well-formed address is
    # stored, anything else is rejected (keep the existing value) so we never
    # store junk or a multi-recipient string.
    if notify_email is not None:
        candidate = notify_email.strip()
        if not candidate:
            user.notify_email = None
        elif valid_notify_email(candidate):
            user.notify_email = candidate
        # else: invalid -> leave user.notify_email untouched
    # Telegram chat ids are integers only; drop anything else (Fix #9).
    _tid = (telegram_id or "").strip()
    user.telegram_id = _tid if _tid.lstrip("-").isdigit() else None
    if locale in _VALID_LOCALES:
        user.locale = locale
    if notify_via_email is not None:
        user.notify_via_email = notify_via_email
    if notify_via_telegram is not None:
        user.notify_via_telegram = notify_via_telegram
    if digest_enabled is not None:
        user.digest_enabled = digest_enabled
    if digest_weekday is not None and 0 <= digest_weekday <= 6:
        user.digest_weekday = digest_weekday
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def can_manage_user(viewer: AppUser, target: AppUser) -> bool:
    """Whether `viewer` may act on `target` (subscription, plan, delete, role).

    Hierarchy superadmin > admin > moderator > user: you act only on people
    strictly below you, never a peer, never a superior. Acting on yourself is
    reserved to the superadmin (owner) — so a manager/admin cannot set their own
    plan. Moderators are further scoped to the users assigned to them.
    """
    from app.permissions import outranks

    if viewer.id == target.id:
        return viewer.role == Role.superadmin
    if not outranks(viewer.role, target.role):
        return False
    if viewer.role == Role.moderator:
        return target.manager_id == viewer.id
    return True


def change_role(session: Session, user: AppUser, new_role: Role) -> AppUser:
    # Demoting a manager to a plain user: same guard as soft_delete. Otherwise
    # their users keep a manager_id pointing at a user (digests keep going to
    # them, reports drop that revenue) and demote-then-delete skips the check.
    if (
        user.role in (Role.admin, Role.moderator)
        and new_role == Role.user
        and assigned_active_count(session, user.id) > 0
    ):
        raise OrphanError()
    user.role = new_role
    session.add(user)
    session.commit()
    session.refresh(user)
    return user
