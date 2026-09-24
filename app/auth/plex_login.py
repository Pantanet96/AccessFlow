"""Resolve a Plex account to an AppUser, or activate a pending invite."""
from sqlalchemy import func
from sqlmodel import Session, select

from app import runtime_config
from app.models import AppUser, Role
from app.services.invite_activation import activate_pending_invite, sync_plex_fields

_sync_plex_fields = sync_plex_fields


def _bound_elsewhere(user: AppUser, acc_id: str | None) -> bool:
    # plex.tv frees an email once its owner changes it, and anyone can then
    # sign up with it; the account id is never reused. Email only makes the
    # first link — after that, a different account id is someone else.
    return bool(user.plex_account_id) and user.plex_account_id != acc_id


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
        if sa is not None and sa.is_active and not _bound_elsewhere(sa, acc_id):
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
            if not user.is_active or _bound_elsewhere(user, acc_id):
                return None
            _sync_plex_fields(user, account)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    # 3. Pending invite -> activate (create the user).
    if email:
        return activate_pending_invite(session, account)

    return None
