"""One-click email unsubscribe: the signed link behind List-Unsubscribe.

The link is a capability, not a session: whoever holds it can switch off email
notifications for the one user it names, and nothing else. That is the whole
point of RFC 8058 -- the mailbox provider POSTs it from its own servers, with
no cookie and no browser -- so the token has to carry all the authority and as
little of it as possible.

Not time-limited: a reminder sits in a mailbox for months and its unsubscribe
button has to keep working. Bound to the address it was sent to instead, so a
link from a forwarded or leaked message stops working once the user moves
their notifications elsewhere.
"""
import hashlib

from itsdangerous import BadSignature, URLSafeSerializer
from sqlmodel import Session

from app.config import get_settings
from app.models import AppUser

_SALT = "email-unsubscribe"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().app_secret_key, salt=_SALT)


def _address_tag(user: AppUser) -> str:
    """Short fingerprint of where the user's notifications currently go."""
    addr = (user.effective_notify_email or "").strip().lower()
    return hashlib.sha256(addr.encode()).hexdigest()[:10]


def make_token(user: AppUser) -> str:
    return _serializer().dumps({"u": user.id, "a": _address_tag(user)})


def read_token(session: Session, token: str) -> AppUser | None:
    """The user a token names, or None if it is forged, garbled or stale."""
    try:
        data = _serializer().loads(token)
        user_id, tag = int(data["u"]), str(data["a"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return None
    user = session.get(AppUser, user_id)
    if user is None or _address_tag(user) != tag:
        return None
    return user


def url_for(user: AppUser) -> str:
    """Absolute unsubscribe URL for `user`, or "" when there is none to give.

    Empty while the public URL is still the local default: a List-Unsubscribe
    pointing at localhost is worse than no header, since a provider that
    cannot reach it counts the failure against the sender.
    """
    from app.services.notification_templates import public_site_url  # lazy: cycle

    base = public_site_url()
    if not base or user.id is None:
        return ""
    return f"{base}/unsubscribe/{make_token(user)}"


def label_for(locale: str | None) -> str:
    """The visible wording of the link, in the reader's language."""
    from app.services.notification_templates import _LAYOUT_STRINGS  # lazy

    return (_LAYOUT_STRINGS.get(locale) or _LAYOUT_STRINGS["en"])["unsubscribe"]


def apply(session: Session, user: AppUser) -> bool:
    """Switch email notifications off. Returns False when they already were,
    so a repeated click (or a provider retrying its POST) is not logged twice."""
    if not user.notify_via_email:
        return False
    user.notify_via_email = False
    session.add(user)
    session.commit()
    return True
