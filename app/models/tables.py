"""SQLModel table definitions for AccessFlow."""
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel

from app.models.enums import (
    InviteStatus,
    NotificationChannel,
    NotificationType,
    RenewalStatus,
    Role,
    SubscriptionStatus,
)


def utcnow() -> datetime:
    # Naive UTC: SQLite stores naive datetimes, so keeping everything naive
    # avoids aware/naive comparison errors when reading values back.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AppUser(SQLModel, table=True):
    __tablename__ = "app_user"

    id: int | None = Field(default=None, primary_key=True)
    role: Role = Field(index=True)
    real_name: str
    # Local-login username (SuperAdmin); Plex users may mirror plex_username.
    username: str | None = Field(default=None, unique=True, index=True)
    password_hash: str | None = None  # only set for local-login accounts

    plex_username: str | None = Field(default=None, index=True)
    plex_email: str | None = Field(default=None, index=True)
    plex_account_id: str | None = Field(default=None, unique=True, index=True)

    notify_email: str | None = None  # fallback -> plex_email
    telegram_id: str | None = Field(default=None, index=True)
    locale: str = Field(default="it")
    # Notification channel opt-in (a user may decline one of the two)
    notify_via_email: bool = Field(default=True)
    notify_via_telegram: bool = Field(default=True)
    # Manager-only: weekly "collect" digest. enabled + weekday (0=Mon..6=Sun).
    # Channels reuse notify_via_email / notify_via_telegram above.
    digest_enabled: bool = Field(default=True)
    digest_weekday: int = Field(default=0)

    manager_id: int | None = Field(default=None, foreign_key="app_user.id", index=True)
    is_active: bool = Field(default=True, index=True)
    # Bumped on logout / password change to invalidate all existing session
    # cookies server-side (the signed cookie carries the value it was issued at).
    session_gen: int = Field(default=0)

    # Access lifecycle / library sharing
    shared_libraries: str | None = None  # JSON list of section titles; null = global default
    access_suspended: bool = Field(default=False)  # libraries removed on Plex
    grace_days: int = Field(default=0)  # days after expiry before auto-suspend (0..15)
    overseerr_prev_permissions: int | None = None  # saved before disabling, restored on enable

    created_at: datetime = Field(default_factory=utcnow)
    # Last broadcast.id this user has dismissed the in-app banner for; a newer
    # broadcast (matching their role, if targeted) shows again until dismissed.
    dismissed_broadcast_id: int | None = Field(default=None)
    # Onboarding modal shown once per account; replay from /profile sets
    # ?tutorial=1 instead of flipping this back to False.
    tutorial_seen: bool = Field(default=False)

    @property
    def effective_notify_email(self) -> str | None:
        return self.notify_email or self.plex_email


class Plan(SQLModel, table=True):
    __tablename__ = "plan"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    # Duration template (None/None + is_unlimited for Family&Friends; Trial sets
    # duration per-subscription at apply time).
    duration_months: int | None = None
    duration_days: int | None = None
    is_unlimited: bool = False
    price_cents: int = 0
    is_paid: bool = False
    is_trial: bool = False
    active: bool = True
    libraries: str | None = None  # JSON list of section titles; null = global default


class Subscription(SQLModel, table=True):
    __tablename__ = "subscription"

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="app_user.id", index=True)
    plan_id: int = Field(foreign_key="plan.id")
    start_at: datetime = Field(default_factory=utcnow)
    expiry_at: datetime | None = None  # None = unlimited
    status: SubscriptionStatus = Field(
        default=SubscriptionStatus.active, index=True
    )
    created_at: datetime = Field(default_factory=utcnow)


class Renewal(SQLModel, table=True):
    __tablename__ = "renewal"

    id: int | None = Field(default=None, primary_key=True)
    subscription_id: int = Field(foreign_key="subscription.id", index=True)
    plan_id: int = Field(foreign_key="plan.id")  # price snapshot
    periods: int = Field(default=1)  # how many plan durations this renewal buys
    amount_cents: int = 0
    status: RenewalStatus = Field(default=RenewalStatus.pending, index=True)
    causale: str | None = None  # transaction text, set when marked paid
    due_at: datetime | None = None
    paid_at: datetime | None = Field(default=None, index=True)
    created_by: int | None = Field(default=None, foreign_key="app_user.id")
    collected_by: int | None = Field(default=None, foreign_key="app_user.id")
    created_at: datetime = Field(default_factory=utcnow)


class NotificationLog(SQLModel, table=True):
    __tablename__ = "notification_log"

    id: int | None = Field(default=None, primary_key=True)
    # Null for invite emails: the invitee has no AppUser until they first sign
    # in with Plex. Exactly one of user_id / invite_id is set.
    user_id: int | None = Field(default=None, foreign_key="app_user.id", index=True)
    # Plain column, not a FK: withdrawing an invite deletes the invite row and
    # the send history must survive that (see migration bab6915f4e8c).
    invite_id: int | None = Field(default=None, index=True)
    subscription_id: int | None = Field(
        default=None, foreign_key="subscription.id"
    )
    type: NotificationType
    channel: NotificationChannel
    # Successful sends use the real dedup key; failed attempts use "fail:<uuid>"
    # so they never collide and never block the real send's retry next run.
    dedup_key: str = Field(unique=True, index=True)
    status: str = Field(default="sent", index=True)  # "sent" | "failed"
    error: str | None = None  # human-readable reason when status == "failed"
    sent_at: datetime = Field(default_factory=utcnow)


class Broadcast(SQLModel, table=True):
    """Persisted broadcast text backing the in-app banner (dismissed per-user
    via AppUser.dismissed_broadcast_id). Independent of the Telegram/email push
    in NotificationLog, which is fire-and-forget."""
    __tablename__ = "broadcast"

    id: int | None = Field(default=None, primary_key=True)
    message: str
    only_role: Role | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)


class Invite(SQLModel, table=True):
    __tablename__ = "invite"

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    real_name: str
    intended_role: Role = Field(default=Role.user)
    manager_id: int | None = Field(default=None, foreign_key="app_user.id")
    plan_id: int | None = Field(default=None, foreign_key="plan.id")
    trial_days: int | None = None
    libraries: str | None = None  # JSON list of section titles; null = global default
    token: str = Field(unique=True, index=True)
    status: InviteStatus = Field(default=InviteStatus.pending, index=True)
    plex_invite_sent_at: datetime | None = None
    created_by: int | None = Field(default=None, foreign_key="app_user.id")
    created_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_setting"

    key: str = Field(primary_key=True)
    value: str  # Fernet-encrypted


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    actor_id: int | None = Field(default=None, foreign_key="app_user.id")
    action: str = Field(index=True)
    target_type: str | None = None
    target_id: str | None = None
    detail: str | None = None  # JSON-encoded
    created_at: datetime = Field(default_factory=utcnow)
