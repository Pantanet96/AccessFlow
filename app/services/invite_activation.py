"""Activate a pending Invite into an AppUser, given a Plex account dict.

Shared by the interactive Plex-login path (plex_login.resolve_or_activate_user,
block 3) and the reconciliation path (plex_import.import_plex_users + the
/invites/reconcile "Verify now" button) — both need to turn a Plex-accepted
share into a local AppUser the moment the invitee's email shows up.
"""
from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.models import AppUser, Invite, InviteStatus
from app.services.activation import on_user_activated


def sync_plex_fields(user: AppUser, account: dict) -> None:
    acc_id = account.get("id")
    if acc_id is not None:
        user.plex_account_id = str(acc_id)
    if account.get("username"):
        user.plex_username = account["username"]
    if account.get("email"):
        user.plex_email = account["email"]


def activate_pending_invite(session: Session, account: dict) -> AppUser | None:
    """Create the AppUser for the pending Invite matching `account`'s email, if any."""
    email = (account.get("email") or "").strip().lower()
    if not email:
        return None
    invite = session.exec(
        select(Invite).where(
            func.lower(Invite.email) == email,
            Invite.status == InviteStatus.pending,
        )
    ).first()
    if invite is None:
        return None

    user = AppUser(
        role=invite.intended_role,
        real_name=invite.real_name,
        manager_id=invite.manager_id,
        locale=get_settings().default_locale,
        is_active=True,
    )
    sync_plex_fields(user, account)
    session.add(user)
    invite.status = InviteStatus.accepted
    session.add(invite)
    session.commit()
    session.refresh(user)
    on_user_activated(session, user, invite)
    return user
