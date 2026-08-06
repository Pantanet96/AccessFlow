"""Import existing Plex-shared users as AppUser records (role=User)."""
import json

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import AppUser, Role
from app.services import overseerr_service, plex_service
from app.services.subscriptions import get_active_subscription


def import_plex_users(session: Session) -> dict:
    users = plex_service.list_shared_users()
    created = 0
    skipped = 0
    new_plex_ids = []
    # Build the set currently shared on Plex, to detect stale app users below.
    shared_ids = {str(u["id"]) for u in users if u.get("id")}
    shared_emails = {(u.get("email") or "").lower() for u in users if u.get("email")}
    for u in users:
        acc_id = u.get("id")
        email = (u.get("email") or "").strip()

        existing = None
        if acc_id:
            existing = session.exec(
                select(AppUser).where(AppUser.plex_account_id == str(acc_id))
            ).first()
        if existing is None and email:
            existing = session.exec(
                select(AppUser).where(func.lower(AppUser.plex_email) == email.lower())
            ).first()
        if existing is not None:
            skipped += 1
            continue

        # Keep their current Plex sharing: snapshot the libraries they already have.
        current_libs = None
        if email:
            try:
                titles = plex_service.get_user_sections(email)
                if titles:
                    current_libs = json.dumps(titles)
            except Exception:  # noqa: BLE001
                current_libs = None

        session.add(
            AppUser(
                role=Role.user,
                real_name=u.get("username") or email or "Plex user",
                plex_username=u.get("username"),
                plex_email=email or None,
                plex_account_id=str(acc_id) if acc_id else None,
                shared_libraries=current_libs,
                is_active=True,
            )
        )
        created += 1
        if acc_id:
            new_plex_ids.append(str(acc_id))
    session.commit()

    # Let Overseerr pull the same users in (best-effort).
    if new_plex_ids:
        try:
            overseerr_service.import_from_plex(new_plex_ids)
        except Exception:  # noqa: BLE001
            pass

    # Inverse detection: active app users no longer shared on Plex (removed
    # outside the app). Flagged only — never auto-deleted.
    stale = []
    active_users = session.exec(
        select(AppUser)
        .where(AppUser.is_active.is_(True))
        .where(AppUser.role == Role.user)
    ).all()
    for au in active_users:
        if au.access_suspended:
            continue  # suspended on purpose -> not "stale"
        aid = (au.plex_account_id or "").strip()
        amail = (au.plex_email or "").lower()
        if not aid and not amail:
            continue
        if (aid and aid in shared_ids) or (amail and amail in shared_emails):
            continue
        stale.append(au.real_name or amail or aid)

    return {"created": created, "skipped": skipped, "stale": stale}


def users_without_active_subscription(session: Session) -> list[AppUser]:
    """Active end-users that have no active subscription (need a plan assigned)."""
    rows = session.exec(
        select(AppUser)
        .where(AppUser.is_active.is_(True))
        .where(AppUser.role == Role.user)
        .order_by(AppUser.real_name)
    ).all()
    return [u for u in rows if get_active_subscription(session, u.id) is None]
