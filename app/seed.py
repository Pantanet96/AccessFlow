"""Idempotent seeding: default plans + the SuperAdmin local account."""
import logging
import secrets

from sqlmodel import Session, select

from app.config import _WEAK_SECRETS, get_settings
from app.models import AppUser, Plan, Role
from app.security import hash_password

logger = logging.getLogger("pum.seed")

# Only the two special built-in plans are seeded. Paid plans (price + custom
# duration) are created by the SuperAdmin from the Plans page.
DEFAULT_PLANS = [
    dict(
        slug="family_friends",
        name="Family & Friends",
        is_unlimited=True,
        price_cents=0,
        is_paid=False,
        is_trial=False,
    ),
    dict(
        slug="trial",
        name="Trial",
        is_trial=True,
        price_cents=0,
        is_paid=False,
    ),
]


def seed_plans(session: Session) -> None:
    for tpl in DEFAULT_PLANS:
        existing = session.exec(
            select(Plan).where(Plan.slug == tpl["slug"])
        ).first()
        if existing is None:
            session.add(Plan(**tpl))
    session.commit()


def _surface_generated_password(username: str, password: str) -> None:
    """Make the one-time generated SuperAdmin password available to the operator
    without printing it into the app logs (which often ship to aggregators). Write
    it to a 0600 file in the data dir and log only that path. Falls back to logging
    the password if the file can't be written, so bootstrap never leaves the
    operator locked out."""
    import os
    from pathlib import Path

    try:
        data_dir = Path(get_settings().database_path).parent
        data_dir.mkdir(parents=True, exist_ok=True)
        cred_path = data_dir / "INITIAL_SUPERADMIN_PASSWORD.txt"
        fd = os.open(cred_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(
                f"username: {username}\npassword: {password}\n"
                "Delete this file after logging in and changing the password.\n"
            )
        logger.warning(
            "SUPERADMIN_PASSWORD non impostata: password temporanea per '%s' "
            "scritta in %s — accedi, cambiala ed elimina il file.",
            username, cred_path,
        )
    except OSError:
        logger.warning(
            "SUPERADMIN_PASSWORD non impostata: generata password temporanea per "
            "'%s': %s  — accedi e cambiala subito.",
            username, password,
        )


def seed_superadmin(session: Session) -> None:
    settings = get_settings()
    existing = session.exec(
        select(AppUser).where(AppUser.role == Role.superadmin)
    ).first()
    if existing is not None:
        # Normalize the old default display name without requiring a DB wipe.
        if existing.real_name == "Super Admin":
            existing.real_name = "Admin"
            session.add(existing)
            session.commit()
        return
    password = settings.superadmin_password
    if password in _WEAK_SECRETS:
        password = secrets.token_urlsafe(16)
        _surface_generated_password(settings.superadmin_username, password)
    session.add(
        AppUser(
            role=Role.superadmin,
            real_name="Admin",
            username=settings.superadmin_username,
            password_hash=hash_password(password),
            locale=settings.default_locale,
            is_active=True,
        )
    )
    session.commit()


def seed_all(session: Session) -> None:
    seed_plans(session)
    seed_superadmin(session)
