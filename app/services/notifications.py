"""Expiry-reminder engine: build context, render per-channel templates, send, dedup, scan.

Message COPY lives in app/services/notification_templates.py (channel-split,
per-locale, admin-overridable). This module only builds the context dict, picks
the template type, renders per channel, and handles dedup + the daily scan.
"""
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlmodel import Session, select

from app import runtime_config
from app.config import get_settings
from app.models import (
    AppUser,
    NotificationChannel,
    NotificationLog,
    NotificationType,
    Plan,
    Subscription,
    SubscriptionStatus,
    utcnow,
)
from app.services import mail_service, telegram_service
from app.services.notification_templates import render_email, render_telegram

_TG = "MarkdownV2"  # templates emit MarkdownV2; dynamic values escaped via |tg
log = logging.getLogger("pum.notifications")


def _ntype_for(days: int) -> NotificationType:
    """Generic, day-agnostic category. The exact day is preserved in the
    dedup_key, so the enum only marks pre-expiry vs overdue."""
    return (
        NotificationType.overdue_reminder if days <= 0
        else NotificationType.expiry_reminder
    )


def _amount_eur(plan: Plan) -> str:
    return f"{plan.price_cents / 100:.2f}"


def _log_failure(
    session: Session,
    recipient_id: int | None,
    sub_id: int | None,
    ntype: NotificationType,
    channel: NotificationChannel,
    error: str,
    invite_id: int | None = None,
) -> None:
    """Record a failed send for the history view. Uses a synthetic dedup_key so it
    never collides and never blocks the real send's retry on the next run."""
    session.add(
        NotificationLog(
            user_id=recipient_id,
            invite_id=invite_id,
            subscription_id=sub_id,
            type=ntype,
            channel=channel,
            dedup_key=f"fail:{uuid.uuid4().hex}",
            status="failed",
            error=error[:250],
        )
    )
    session.commit()


def _send_deduped(
    session: Session,
    *,
    recipient_id: int | None,
    sub_id: int | None,
    ntype: NotificationType,
    channel: NotificationChannel,
    dedup_key: str,
    sender,
    invite_id: int | None = None,
) -> bool:
    existing = session.exec(
        select(NotificationLog).where(NotificationLog.dedup_key == dedup_key)
    ).first()
    if existing is not None:
        return False
    try:
        sent = sender()
    except Exception as exc:  # noqa: BLE001 - one bad recipient must not abort the scan
        # Real runtime failure (only email raises: SMTP down, no network, timeout,
        # auth). The SMTP exception text is safe (no secrets). Log it for the
        # history view and treat as not-sent so the next run retries.
        log.warning("notification send failed (recipient=%s, key=%s): %s",
                    recipient_id, dedup_key, exc)
        _log_failure(session, recipient_id, sub_id, ntype, channel,
                     f"{type(exc).__name__}: {exc}", invite_id=invite_id)
        return False
    if not sent:
        # Telegram never raises; it returns False on any failure (bot blocked,
        # network, invalid token). Record it WITHOUT the raw reason: the Telegram
        # error URL embeds the bot token and must never be persisted/logged.
        # Email returning False means SMTP is simply unconfigured (a choice, not a
        # fault) -> no failure row.
        if channel == NotificationChannel.telegram:
            _log_failure(session, recipient_id, sub_id, ntype, channel,
                         "Telegram: invio rifiutato (bot bloccato, rete o token)",
                         invite_id=invite_id)
        return False  # retry next run
    session.add(
        NotificationLog(
            user_id=recipient_id,
            invite_id=invite_id,
            subscription_id=sub_id,
            type=ntype,
            channel=channel,
            dedup_key=dedup_key,
        )
    )
    session.commit()
    return True


def _send_email(session, *, recipient, sub_id, ntype, dedup_key, type_, ctx) -> bool:
    if not (recipient.notify_via_email and recipient.effective_notify_email):
        return False
    subject, html, text = render_email(session, type_, recipient.locale, ctx)
    if not (subject or html or text):
        return False  # template empty for this type/locale -> nothing to send
    return _send_deduped(
        session,
        recipient_id=recipient.id,
        sub_id=sub_id,
        ntype=ntype,
        channel=NotificationChannel.email,
        dedup_key=dedup_key,
        sender=lambda: mail_service.send_email(
            recipient.effective_notify_email, subject, text, html=html or None
        ),
    )


def _send_telegram(session, *, recipient, sub_id, ntype, dedup_key, type_, ctx) -> bool:
    if not (recipient.notify_via_telegram and recipient.telegram_id):
        return False
    body = render_telegram(session, type_, recipient.locale, ctx)
    if not body:
        return False
    return _send_deduped(
        session,
        recipient_id=recipient.id,
        sub_id=sub_id,
        ntype=ntype,
        channel=NotificationChannel.telegram,
        dedup_key=dedup_key,
        sender=lambda: telegram_service.send_message(recipient.telegram_id, body, parse_mode=_TG),
    )


def notify_expiry(session: Session, sub: Subscription, days: int) -> None:
    user = session.get(AppUser, sub.user_id)
    plan = session.get(Plan, sub.plan_id)
    if user is None or plan is None or sub.expiry_at is None:
        return
    overdue = days <= 0
    ntype = _ntype_for(days)
    exp = sub.expiry_at.date().isoformat()
    tag = "ovd" if overdue else "exp"      # dedup namespace per phase
    grace_left = user.grace_days + days    # days <= 0 when overdue
    suspended = overdue and grace_left <= 0

    user_type = "user_overdue" if overdue else "user_expiry"
    uctx = {
        "name": user.real_name,
        "plan_name": plan.name,
        "expiry_date": exp,
        "days": days,
        "grace_left": grace_left,
        "suspended": suspended,
    }
    _send_email(
        session, recipient=user, sub_id=sub.id, ntype=ntype, type_=user_type,
        dedup_key=f"{tag}:{sub.id}:{days}:{exp}:user:email", ctx=uctx,
    )
    _send_telegram(
        session, recipient=user, sub_id=sub.id, ntype=ntype, type_=user_type,
        dedup_key=f"{tag}:{sub.id}:{days}:{exp}:user:telegram", ctx=uctx,
    )
    # NB: managers are NOT pinged per-user here anymore — that spammed them once
    # per user per reminder day. They get a consolidated weekly digest instead
    # (run_manager_digests below).


def run_expiry_scan(session: Session, today: datetime | None = None) -> dict:
    now = today or utcnow()
    counts = {"notified": 0, "expired": 0}
    sched = runtime_config.reminder_schedule()
    fire_days = set(sched["before"]) | set(sched["after"])
    subs = session.exec(
        select(Subscription).where(
            Subscription.status.in_(
                (SubscriptionStatus.active, SubscriptionStatus.expired)
            )
        )
    ).all()
    for sub in subs:
        if sub.expiry_at is None:          # unlimited / F&F -> never dunned
            continue
        days_left = (sub.expiry_at.date() - now.date()).days
        if days_left in fire_days:
            notify_expiry(session, sub, days_left)
            counts["notified"] += 1
        # Flip to expired once; expired subs stay scanned so overdue buckets fire.
        if days_left < 0 and sub.status != SubscriptionStatus.expired:
            sub.status = SubscriptionStatus.expired
            session.add(sub)
            session.commit()
            counts["expired"] += 1
    return counts


# ---- Manager weekly "collect" digest (cumulative, anti-spam) ----

def collectables(session, *, manager_id=None, lookahead, today=None):
    """Rows of paid subs expiring within `lookahead` days (overdue included) with
    NO pending renewal yet. manager_id=None -> all managed users (admin dashboard);
    set -> only that manager's users (digest + moderator dashboard). Sorted by
    days_left asc (most urgent / most overdue first)."""
    from app.services.subscriptions import get_current_subscription, has_pending_renewal

    today = (today or utcnow()).date()
    stmt = select(AppUser).where(AppUser.is_active.is_(True))
    if manager_id is not None:
        stmt = stmt.where(AppUser.manager_id == manager_id)
    else:
        stmt = stmt.where(AppUser.manager_id.is_not(None))
    rows = []
    for u in session.exec(stmt).all():
        # active OR expired: overdue subs are flipped to `expired` by the daily
        # scan; they are the MOST important to collect, so must not be dropped.
        sub = get_current_subscription(session, u.id)
        if sub is None or sub.expiry_at is None:
            continue
        plan = session.get(Plan, sub.plan_id)
        if plan is None or not plan.is_paid:
            continue
        days_left = (sub.expiry_at.date() - today).days
        if days_left > lookahead:
            continue
        if has_pending_renewal(session, sub.id):
            continue
        rows.append({
            "user_id": u.id,
            "user_name": u.real_name,
            "plan_name": plan.name,
            "expiry_date": sub.expiry_at.date().isoformat(),
            "days_left": days_left,
            "amount_cents": plan.price_cents,
            "amount_eur": _amount_eur(plan),
        })
    rows.sort(key=lambda r: r["days_left"])
    return rows


def run_manager_digests(session, today=None) -> int:
    """Weekly per-manager collect digest. For each manager with digest_enabled and
    digest_weekday == today's weekday, send ONE email/Telegram listing their
    collectable subs within the lookahead window. Idempotent per ISO week.
    Channels reuse the manager's notify_via_email / notify_via_telegram prefs."""
    now = today or utcnow()
    weekday = now.weekday()
    lookahead = runtime_config.digest_lookahead()
    period = now.strftime("%G-W%V")
    mgr_ids = {
        u.manager_id for u in session.exec(
            select(AppUser).where(AppUser.manager_id.is_not(None))
        ).all()
    }
    sent = 0
    for mid in mgr_ids:
        mgr = session.get(AppUser, mid)
        if mgr is None or not mgr.is_active or not mgr.digest_enabled:
            continue
        if mgr.digest_weekday != weekday:
            continue
        items = collectables(session, manager_id=mid, lookahead=lookahead, today=now)
        if not items:
            continue
        total_cents = sum(r["amount_cents"] for r in items)
        ctx = {
            "name": mgr.real_name,
            "items": items,
            "count": len(items),
            "window_days": lookahead,
            "total_eur": f"{total_cents / 100:.2f}",
        }
        sent += _send_email(
            session, recipient=mgr, sub_id=None,
            ntype=NotificationType.manager_digest, type_="manager_digest",
            dedup_key=f"digest:{mid}:{period}:email", ctx=ctx,
        )
        sent += _send_telegram(
            session, recipient=mgr, sub_id=None,
            ntype=NotificationType.manager_digest, type_="manager_digest",
            dedup_key=f"digest:{mid}:{period}:telegram", ctx=ctx,
        )
    return sent


# ---- Invite email (no AppUser yet: the invitee signs up on Plex first) ----

def notify_invite(session: Session, invite, *, resend: bool = False) -> bool:
    """Email an invitee the two ways in to the shared server. Returns True if the
    message went out.

    Not routed through `_send_email`: that needs an AppUser for the recipient's
    locale and channel preferences, and an invitee has neither yet. Locale is the
    instance default; the log row carries `invite_id` instead of `user_id`.
    `resend` gives the send its own dedup key so an admin can retry a bounced
    invite without the original log row swallowing it.

    The dedup key is built from `invite.token`, not `invite.id`: SQLite reuses
    row ids after a DELETE, and withdrawing an invite deletes it — an id-keyed
    send would be silently deduped away for whoever inherited the id next."""
    import json

    base = runtime_config.public_base_url()
    try:
        libraries = json.loads(invite.libraries) if invite.libraries else []
    except (ValueError, TypeError):
        libraries = []
    if not libraries:
        libraries = runtime_config.plex_default_sections()
    plan = session.get(Plan, invite.plan_id) if invite.plan_id else None
    inviter = session.get(AppUser, invite.created_by) if invite.created_by else None
    ctx = {
        "name": invite.real_name,
        "email": invite.email,
        "login_url": f"{base}/login" if base else "",
        "libraries": libraries,
        "plan_name": plan.name if plan else "",
        "inviter_name": (inviter.real_name if inviter else "") or "AccessFlow",
    }
    locale = get_settings().default_locale
    subject, html, text = render_email(session, "invite", locale, ctx)
    if not (subject or html or text):
        return False  # template blanked out by an admin -> nothing to send
    attempt = f":resend:{uuid.uuid4().hex}" if resend else ""
    return _send_deduped(
        session,
        recipient_id=None,
        invite_id=invite.id,
        sub_id=None,
        ntype=NotificationType.invite,
        channel=NotificationChannel.email,
        dedup_key=f"invite:{invite.token}:email{attempt}",
        sender=lambda: mail_service.send_email(
            invite.email, subject, text, html=html or None
        ),
    )


# ---- Welcome / onboarding (one-time, on activation) ----

def _welcome_ctx(session: Session, user: AppUser, plan: Plan, expiry: datetime | None) -> dict:
    from app.services.telegram_link import make_link_token

    if expiry is not None:
        exp_str = expiry.date().isoformat()
    else:
        exp_str = "illimitato" if user.locale == "it" else "unlimited"
    public_url = runtime_config.overseerr_config()["public_url"]
    username = runtime_config.telegram_config()["username"]
    tg_link = (
        f"https://t.me/{username}?start={make_link_token(user.id, bind=user.telegram_id or '')}"
        if username else ""
    )
    return {
        "name": user.real_name,
        "plan_name": plan.name,
        "expiry_date": exp_str,
        "public_url": public_url,
        "telegram_link": tg_link,
    }


def notify_welcome(session: Session, user: AppUser, plan: Plan,
                   sub: Subscription) -> None:
    """One-time onboarding message on each available channel (idempotent)."""
    ctx = _welcome_ctx(session, user, plan, sub.expiry_at)
    _send_email(
        session, recipient=user, sub_id=sub.id, ntype=NotificationType.welcome,
        type_="welcome", dedup_key=f"welcome:{user.id}:email", ctx=ctx,
    )
    _send_telegram(
        session, recipient=user, sub_id=sub.id, ntype=NotificationType.welcome,
        type_="welcome", dedup_key=f"welcome:{user.id}:telegram", ctx=ctx,
    )


# ---- Manual "send reminder now" (off the daily cycle, fresh dedup key) ----

def notify_expiry_manual(session: Session, sub: Subscription) -> int:
    """Re-fire the current expiry reminder NOW. Each click gets a unique dedup
    key (full timestamp) so a manual send is never blocked by a daily bucket or
    an earlier click — managers test/resend on demand. Returns messages sent."""
    user = session.get(AppUser, sub.user_id)
    plan = session.get(Plan, sub.plan_id)
    if user is None or plan is None or sub.expiry_at is None:
        return 0
    today = utcnow().date()
    # Real (possibly negative) days so an already-overdue user gets the overdue /
    # suspended copy, not a wrong "expires in 0 days". Mirrors notify_expiry.
    days = (sub.expiry_at.date() - today).days
    overdue = days <= 0
    ntype = _ntype_for(days)
    exp = sub.expiry_at.date().isoformat()
    tag = "ovd" if overdue else "exp"
    grace_left = user.grace_days + days
    user_type = "user_overdue" if overdue else "user_expiry"
    stamp = uuid.uuid4().hex  # unique per click -> every manual send fires, none deduped
    ctx = {
        "name": user.real_name,
        "plan_name": plan.name,
        "expiry_date": exp,
        "days": days,
        "grace_left": grace_left,
        "suspended": overdue and grace_left <= 0,
    }
    sent = 0
    sent += _send_email(
        session, recipient=user, sub_id=sub.id, ntype=ntype, type_=user_type,
        dedup_key=f"{tag}:{sub.id}:manual:{stamp}:{exp}:user:email", ctx=ctx,
    )
    sent += _send_telegram(
        session, recipient=user, sub_id=sub.id, ntype=ntype, type_=user_type,
        dedup_key=f"{tag}:{sub.id}:manual:{stamp}:{exp}:user:telegram", ctx=ctx,
    )
    return sent


# ---- Retention: prune old notification_log rows (admin-configured) ----

def prune_old_notifications(session: Session, today: datetime | None = None) -> int:
    """Delete notification_log rows older than the configured retention window.
    0 = keep forever (unset defaults to 30 days). Returns the number of rows deleted.

    Welcome rows are never pruned. `welcome:{user_id}` is the only dedup_key with
    no date in it, so deleting one would let a re-activated user receive a second
    welcome message. Every other key embeds a day offset, an expiry date, an ISO
    week or a per-send nonce, which is what makes a 1-day retention safe."""
    days = runtime_config.notification_retention_days()
    if days <= 0:
        return 0
    cutoff = (today or utcnow()) - timedelta(days=days)
    result = session.exec(
        delete(NotificationLog)
        .where(NotificationLog.sent_at < cutoff)
        .where(NotificationLog.type != NotificationType.welcome)
    )
    session.commit()
    return result.rowcount or 0
