"""Resolve a Plex account to an AppUser, or activate a pending invite."""
from sqlalchemy import func
from sqlmodel import Session, select

from app import runtime_config
from app.config import get_settings
from app.models import AppUser, Invite, InviteStatus, Role
from app.services.activation import on_user_activated


def _sync_plex_fields(user: AppUser, account: dict) -> None:
    acc_id = account.get("id")
    if acc_id is not None:
        user.plex_account_id = str(acc_id)
    if account.get("username"):
        user.plex_username = account["username"]
    if account.get("email"):
        user.plex_email = account["email"]


def resolve_or_activate_user(session: Session, account: dict) -> AppUser | None:
    acc_id = account.get("id")
    acc_id = str(acc_id) if acc_id is not None else None
    email = (account.get("email") or "").strip().lower()

    # 0. The Plex server owner (account used to connect the server) logs in as
    #    the SuperAdmin. Bind the Plex identity to the local SuperAdmin account.
    owner_email = (runtime_config.plex_config().get("account_email") or "").strip().lower()
    if email and owner_email and email == owner_email:
        sa = session.exec(
            select(AppUser).where(AppUser.role == Role.superadmin)
        ).first()
        if sa is not None and sa.is_active:
            _sync_plex_fields(sa, account)
            session.add(sa)
            session.commit()
            session.refresh(sa)
            return sa

    # 1. Known Plex account id.
    if acc_id is not None:
        user = session.exec(
            select(AppUser).where(AppUser.plex_account_id == acc_id)
        ).first()
        if user is not None:
            if not user.is_active:
                return None
            _sync_plex_fields(user, account)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    # 2. Existing user matched by Plex email -> link the account id.
    if email:
        user = session.exec(
            select(AppUser).where(func.lower(AppUser.plex_email) == email)
        ).first()
        if user is not None:
            if not user.is_active:
                return None
            _sync_plex_fields(user, account)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    # 3. Pending invite -> activate (create the user).
    if email:
        invite = session.exec(
            select(Invite).where(
                func.lower(Invite.email) == email,
                Invite.status == InviteStatus.pending,
            )
        ).first()
        if invite is not None:
            user = AppUser(
                role=invite.intended_role,
                real_name=invite.real_name,
                manager_id=invite.manager_id,
                locale=get_settings().default_locale,
                is_active=True,
            )
            _sync_plex_fields(user, account)
            session.add(user)
            invite.status = InviteStatus.accepted
            session.add(invite)
            session.commit()
            session.refresh(user)
            on_user_activated(session, user, invite)
            return user

    return None
