"""Append-only audit trail for sensitive actions."""
import json

from sqlmodel import Session, select

from app.i18n import N_
from app.i18n import gettext as _
from app.models import (
    AppUser,
    AuditLog,
    Invite,
    NotificationLog,
    NotificationType,
    Plan,
    Subscription,
)

# Human-readable templates (English source strings; translated at render time).
_ACTION_LABELS = {
    "login": N_("signed in"),
    "login_lockout": N_("login locked out (too many failed attempts)"),
    "send_reminder": N_("manually sent an expiry reminder"),
    "update_profile": N_("updated their profile"),
    "change_password": N_("changed their password"),
    "telegram_sync_overseerr": N_("synced Telegram ID with Overseerr"),
    "create_subscription": N_("assigned a plan"),
    "change_plan": N_("changed the plan"),
    "create_renewal": N_("created a renewal"),
    "pay_renewal": N_("confirmed a payment / renewal"),
    "request_renewal": N_("requested a renewal"),
    "delete_renewal": N_("deleted a renewal request"),
    "suspend_access": N_("suspended access"),
    "reactivate_access": N_("reactivated access"),
    "remove_from_plex": N_("removed from Plex"),
    "set_libraries": N_("changed shared libraries"),
    "set_grace": N_("changed grace days"),
    "assign_manager": N_("assigned a manager"),
    "change_role": N_("changed the role"),
    "delete_user": N_("deleted a user"),
    "create_invite": N_("created an invite"),
    "delete_invite": N_("withdrew an invite"),
    "resend_invite": N_("resent an invite email"),
    "import_plex_users": N_("imported users from Plex"),
    "create_plan": N_("created a plan"),
    "edit_plan": N_("edited a plan"),
    "delete_plan": N_("deleted a plan"),
    "broadcast": N_("sent a broadcast"),
    "settings_smtp": N_("updated SMTP settings"),
    "settings_telegram": N_("updated Telegram settings"),
    "settings_overseerr": N_("updated Overseerr settings"),
    "settings_public_url": N_("updated the public address"),
    "settings_reminders": N_("updated the reminder schedule"),
    "settings_digest": N_("updated the manager digest window"),
    "settings_notification_retention": N_("updated the notification retention window"),
    "settings_color_theme": N_("changed the color theme"),
    "settings_template": N_("edited a notification template"),
    "settings_rotate_key": N_("rotated the encryption key"),
    "rename_user": N_("renamed a user"),
    "plex_connect": N_("connected Plex"),
    "plex_select_server": N_("selected the Plex server"),
    "plex_disconnect": N_("disconnected Plex"),
    "set_default_libraries": N_("set the default libraries"),
}


def record(
    session: Session,
    actor_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id=None,
    detail: dict | None = None,
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=json.dumps(detail) if detail else None,
        )
    )
    session.commit()


def _detail_summary(action: str, detail: dict | None) -> str:
    if not detail:
        return ""
    if action == "login":
        return detail.get("method", "")
    if action == "login_lockout":
        return f"{detail.get('username', '?')} @ {detail.get('ip', '?')}"
    if action == "send_reminder":
        return _("sent %(n)s message(s)") % {"n": detail.get("sent", 0)}
    if action == "update_profile":
        return ", ".join(detail.get("changed", []))
    if action in ("change_plan", "create_subscription"):
        return detail.get("plan", "")
    if action == "telegram_sync_overseerr":
        return detail.get("direction", "")
    if action == "set_grace":
        return str(detail.get("grace_days", ""))
    if action == "change_role":
        return detail.get("role", "")
    if action in ("create_invite", "delete_invite", "resend_invite"):
        return detail.get("email", "")
    if action in ("create_plan", "edit_plan"):
        return detail.get("name") or detail.get("type") or ""
    if action == "broadcast":
        return str(detail.get("recipients", ""))
    if action == "import_plex_users":
        return _("created %(c)s, skipped %(s)s") % {
            "c": detail.get("created", 0), "s": detail.get("skipped", 0)
        }
    # Fallback: compact key=value list
    return ", ".join(f"{k}={v}" for k, v in detail.items())


def list_recent(session: Session, limit: int = 200, actor_id: int | None = None) -> list[dict]:
    """Enriched, human-readable audit entries (newest first)."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if actor_id is not None:
        stmt = select(AuditLog).where(AuditLog.actor_id == actor_id).order_by(
            AuditLog.id.desc()
        ).limit(limit)
    rows = session.exec(stmt).all()

    # Resolve actor + (app_user) target names in one pass.
    ids: set[int] = set()
    for r in rows:
        if r.actor_id:
            ids.add(r.actor_id)
        if r.target_type == "app_user" and r.target_id and r.target_id.isdigit():
            ids.add(int(r.target_id))
    names: dict[int, str] = {}
    if ids:
        for u in session.exec(select(AppUser).where(AppUser.id.in_(ids))).all():
            names[u.id] = u.real_name

    out = []
    for r in rows:
        actor = names.get(r.actor_id) or (f"#{r.actor_id}" if r.actor_id else _("system"))
        target = None
        if r.target_type == "app_user" and r.target_id and r.target_id.isdigit():
            target = names.get(int(r.target_id))
        detail = json.loads(r.detail) if r.detail else None
        out.append({
            "id": r.id,
            "when": r.created_at,
            "actor": actor,
            "action": _(_ACTION_LABELS.get(r.action, r.action)),
            "action_key": r.action,
            "target": target,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "detail": _detail_summary(r.action, detail),
            "raw": detail,
        })
    return out


# ---- Sent-notifications history (reads NotificationLog) ----

# English source strings; translated at render time. Legacy day-specific types map
# onto the same two human labels as the current generic ones.
_NTYPE_LABELS = {
    "expiry_reminder": N_("expiry reminder"),
    "overdue_reminder": N_("overdue notice"),
    "welcome": N_("welcome"),
    "manager_digest": N_("manager digest"),
    "manager_collect": N_("manager collect"),
    "broadcast": N_("broadcast"),
    "invite": N_("invite"),
    "expiry_7d": N_("expiry reminder"),
    "expiry_3d": N_("expiry reminder"),
    "expiry_1d": N_("expiry reminder"),
    "expiry_0d": N_("expiry reminder"),
    "overdue_1d": N_("overdue notice"),
    "overdue_3d": N_("overdue notice"),
}


# Types the app emits today — the type filter dropdown. Legacy day-specific
# members are intentionally omitted (they only exist in historical rows).
_FILTER_NTYPES = (
    NotificationType.expiry_reminder,
    NotificationType.overdue_reminder,
    NotificationType.welcome,
    NotificationType.manager_digest,
    NotificationType.broadcast,
    NotificationType.invite,
)


def notification_type_options() -> list[tuple[str, str]]:
    """[(value, label)] for the notification-type filter dropdown."""
    return [(t.value, _(_NTYPE_LABELS[t.value])) for t in _FILTER_NTYPES]


def _managed_ids(session: Session, manager_id: int) -> set[int]:
    """A manager's users + the manager themselves (digests are addressed to them)."""
    ids = set(
        session.exec(select(AppUser.id).where(AppUser.manager_id == manager_id)).all()
    )
    ids.add(manager_id)
    return ids


def notification_recipients(
    session: Session, for_manager_id: int | None = None
) -> dict[int, str]:
    """{user_id: real_name} for users who appear in the notification log — powers the
    recipient filter dropdown. Scoped to a manager's users (+ self) when set."""
    ids = {
        uid
        for uid in session.exec(select(NotificationLog.user_id).distinct()).all()
        if uid is not None  # invite emails predate the AppUser
    }
    if for_manager_id is not None:
        ids &= _managed_ids(session, for_manager_id)
    if not ids:
        return {}
    users = session.exec(select(AppUser).where(AppUser.id.in_(ids))).all()
    return dict(
        sorted(((u.id, u.real_name) for u in users), key=lambda kv: kv[1].lower())
    )


def _recipient_label(row, names: dict, invite_names: dict) -> str:
    if row.user_id is not None:
        return names.get(row.user_id) or f"#{row.user_id}"
    if row.invite_id is not None:
        return invite_names.get(row.invite_id) or _("invite no longer pending")
    return "—"


def list_notifications(
    session: Session,
    limit: int = 100,
    for_manager_id: int | None = None,
    user_id: int | None = None,
    ntype: NotificationType | None = None,
    status: str | None = None,
) -> list[dict]:
    """Enriched sent/failed notification rows (newest first).

    for_manager_id -> restrict to that manager's users (+ self); None = all.
    user_id / ntype / status -> optional filters. status in {"sent", "failed"}.
    """
    stmt = select(NotificationLog)
    if for_manager_id is not None:
        managed = _managed_ids(session, for_manager_id)
        stmt = stmt.where(NotificationLog.user_id.in_(managed))
    if user_id is not None:
        stmt = stmt.where(NotificationLog.user_id == user_id)
    if ntype is not None:
        stmt = stmt.where(NotificationLog.type == ntype)
    if status in ("sent", "failed"):
        stmt = stmt.where(NotificationLog.status == status)
    rows = session.exec(
        stmt.order_by(NotificationLog.id.desc()).limit(limit)
    ).all()

    # Resolve recipient names + plan names in one pass each.
    names: dict[int, str] = {}
    uids = {r.user_id for r in rows if r.user_id is not None}
    if uids:
        for u in session.exec(select(AppUser).where(AppUser.id.in_(uids))).all():
            names[u.id] = u.real_name
    # Invite emails have no AppUser: name them by the address invited. The invite
    # row is gone once withdrawn or accepted, hence the fallback.
    invite_names: dict[int, str] = {}
    inv_ids = {r.invite_id for r in rows if r.invite_id is not None}
    if inv_ids:
        for inv in session.exec(select(Invite).where(Invite.id.in_(inv_ids))).all():
            invite_names[inv.id] = inv.email
    plan_by_sub: dict[int, str | None] = {}
    sub_ids = {r.subscription_id for r in rows if r.subscription_id is not None}
    if sub_ids:
        subs = session.exec(
            select(Subscription).where(Subscription.id.in_(sub_ids))
        ).all()
        plan_names: dict[int, str] = {}
        plan_ids = {s.plan_id for s in subs}
        if plan_ids:
            for p in session.exec(select(Plan).where(Plan.id.in_(plan_ids))).all():
                plan_names[p.id] = p.name
        plan_by_sub = {s.id: plan_names.get(s.plan_id) for s in subs}

    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "when": r.sent_at,
            "recipient": _recipient_label(r, names, invite_names),
            "type_label": _(_NTYPE_LABELS.get(r.type.value, r.type.value)),
            "type_key": r.type.value,
            "channel": r.channel.value,
            "plan": plan_by_sub.get(r.subscription_id),
            "status": r.status,
            "error": r.error,
        })
    return out
