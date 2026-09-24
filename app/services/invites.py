"""Invite lifetime: a pending invite lasts INVITE_DAYS, then expires.

Expiring withdraws the Plex share too: left in place, the invitee could still
accept it on plex.tv and walk in past the deadline.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import AppUser, Invite, InviteStatus, Role, local_date, utcnow
from app.permissions import Capability, has_capability
from app.services import audit, plex_import, plex_service

INVITE_DAYS = 30

log = logging.getLogger("pum.invites")


def new_expiry(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(days=INVITE_DAYS)


def days_left(invite: Invite) -> int | None:
    if invite.expires_at is None:
        return None
    return (local_date(invite.expires_at) - local_date(utcnow())).days


def can_extend(viewer: AppUser, invite: Invite) -> bool:
    """Inviters extend any invite; a manager only those of their own users."""
    if has_capability(viewer, Capability.invite_user):
        return True
    return viewer.role == Role.moderator and invite.manager_id == viewer.id


def pending_for(session: Session, viewer: AppUser) -> list[dict]:
    """Pending invites the viewer should chase, soonest to expire first."""
    stmt = select(Invite).where(Invite.status == InviteStatus.pending)
    if not has_capability(viewer, Capability.invite_user):
        stmt = stmt.where(Invite.manager_id == viewer.id)
    rows = [
        {"invite": i, "days_left": days_left(i), "can_extend": can_extend(viewer, i)}
        for i in session.exec(stmt).all()
    ]
    rows.sort(key=lambda r: r["days_left"] if r["days_left"] is not None else 9999)
    return rows


def extend(session: Session, invite: Invite, actor_id: int | None) -> None:
    """Another INVITE_DAYS counted from today (not from the old deadline)."""
    invite.expires_at = new_expiry()
    session.add(invite)
    session.commit()
    audit.record(session, actor_id, "extend_invite", "invite", invite.id,
                 {"email": invite.email})


def expire_invites(session: Session) -> int:
    """Expire pending invites past their deadline and withdraw their Plex share.
    Returns how many expired. Run by the daily scan."""
    now = utcnow()
    due = session.exec(
        select(Invite)
        .where(Invite.status == InviteStatus.pending)
        .where(Invite.expires_at.is_not(None))
        .where(Invite.expires_at <= now)
    ).all()
    if not due:
        return 0
    # cancel_invite also revokes a share that was already ACCEPTED. Activate
    # whoever accepted since the last reconciliation first, so they're no
    # longer pending here. Plex unreachable -> raise, retry on the next run.
    try:
        plex_import.import_plex_users(session)
    except plex_service.PlexNotConnected:
        pass  # no Plex: nobody could have accepted, nothing to withdraw
    expired = 0
    for inv in due:
        session.refresh(inv)
        if inv.status != InviteStatus.pending:
            continue  # activated by the reconciliation above
        # The address already belongs to a user here (invite for an existing
        # account): never touch their live access, just close the invite.
        known = session.exec(
            select(AppUser).where(func.lower(AppUser.plex_email) == inv.email.lower())
        ).first()
        if known is None:
            try:
                plex_service.cancel_invite(inv.email)
            except (plex_service.PlexShareNotFound, plex_service.PlexNotConnected):
                pass
            except Exception:  # noqa: BLE001 - share may still be live: retry tomorrow
                log.warning("Plex withdraw failed for expired invite %s", inv.email,
                            exc_info=True)
                continue
        inv.status = InviteStatus.expired
        session.add(inv)
        session.commit()
        audit.record(session, None, "invite_expired", "invite", inv.id,
                     {"email": inv.email})
        expired += 1
    return expired
