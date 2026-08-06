"""Manual broadcast (Telegram + email) to active users with a linked channel."""
import logging
import secrets

from sqlmodel import Session, select

from app.models import (
    AppUser,
    Broadcast,
    NotificationChannel,
    NotificationLog,
    NotificationType,
    Role,
)
from app.services import mail_service, telegram_service

log = logging.getLogger("pum.broadcast")

_EMAIL_SUBJECT = {"it": "Comunicazione", "en": "Announcement"}


def _log_result(session, user_id, channel, nonce, *, error=None) -> bool:
    session.add(
        NotificationLog(
            user_id=user_id,
            type=NotificationType.broadcast,
            channel=channel,
            dedup_key=(
                f"fail:{secrets.token_hex(8)}" if error
                else f"broadcast:{nonce}:{user_id}:{channel.value}"
            ),
            status="failed" if error else "sent",
            error=error,
        )
    )
    return error is None


def broadcast(session: Session, message: str, *, only_role: Role | None = None) -> int:
    # Persisted independently of the push sends below: it backs the in-app
    # banner (base.html), which every matching user sees until dismissed,
    # regardless of whether their Telegram/email push succeeded.
    session.add(Broadcast(message=message, only_role=only_role))

    stmt = select(AppUser).where(AppUser.is_active.is_(True))
    if only_role is not None:
        stmt = stmt.where(AppUser.role == only_role)
    recipients = session.exec(stmt).all()

    nonce = secrets.token_hex(6)
    sent = 0
    for user in recipients:
        if user.notify_via_telegram and user.telegram_id:
            if telegram_service.send_message(user.telegram_id, message):
                sent += _log_result(session, user.id, NotificationChannel.telegram, nonce)
            else:
                # Unreachable recipient (bot blocked, network, invalid token).
                # Generic reason only: the Telegram error URL embeds the bot token.
                _log_result(
                    session, user.id, NotificationChannel.telegram, nonce,
                    error="Telegram: invio rifiutato (bot bloccato, rete o token)",
                )
        if user.notify_via_email and user.effective_notify_email:
            subject = _EMAIL_SUBJECT.get(user.locale, _EMAIL_SUBJECT["it"])
            try:
                ok = mail_service.send_email(user.effective_notify_email, subject, message)
            except Exception as exc:  # noqa: BLE001 - one bad recipient must not abort the broadcast
                log.warning("broadcast email failed (user=%s): %s", user.id, exc)
                _log_result(
                    session, user.id, NotificationChannel.email, nonce,
                    error=f"{type(exc).__name__}: {exc}"[:250],
                )
            else:
                if ok:
                    sent += _log_result(session, user.id, NotificationChannel.email, nonce)
    session.commit()
    return sent
