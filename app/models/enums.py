"""Enums shared across tables. Each member's name == value (stored as that string)."""
import enum


class Role(str, enum.Enum):
    superadmin = "superadmin"
    admin = "admin"
    moderator = "moderator"
    user = "user"


class SubscriptionStatus(str, enum.Enum):
    active = "active"
    expired = "expired"
    cancelled = "cancelled"


class RenewalStatus(str, enum.Enum):
    pending = "pending"
    paid = "paid"


class NotificationType(str, enum.Enum):
    expiry_reminder = "expiry_reminder"    # any pre-expiry / day-0 reminder
    overdue_reminder = "overdue_reminder"  # any post-expiry dunning notice
    # Legacy day-specific members kept so historical notification_log rows still
    # deserialize (name==value, TEXT column, no DB enum constraint).
    expiry_7d = "expiry_7d"
    expiry_3d = "expiry_3d"
    expiry_1d = "expiry_1d"
    expiry_0d = "expiry_0d"
    overdue_1d = "overdue_1d"
    overdue_3d = "overdue_3d"
    manager_collect = "manager_collect"
    manager_digest = "manager_digest"  # weekly cumulative collect digest
    invite = "invite"                  # invite email (no AppUser yet)
    broadcast = "broadcast"
    welcome = "welcome"


class NotificationChannel(str, enum.Enum):
    email = "email"
    telegram = "telegram"


class InviteStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
