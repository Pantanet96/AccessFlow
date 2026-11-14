"""SMTP email delivery (stdlib smtplib). No-op when SMTP is not configured."""
import contextlib
import contextvars
import logging
import re
import smtplib
import ssl
import time
from email import policy as _policy
from email.headerregistry import HeaderRegistry, UnstructuredHeader
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from app import runtime_config

log = logging.getLogger("pum.mail")

# One connection shared by a bulk run (daily expiry scan, weekly digests,
# broadcast), set only inside bulk_session(). A ContextVar and not a module
# global: the scheduler runs the scan on its own thread, and a web request
# handling an invite on another must not end up writing into the scan's socket.
_bulk: contextvars.ContextVar = contextvars.ContextVar("smtp_bulk", default=None)

# Connection-level failures a reconnect can fix. NOT a bare OSError: every
# smtplib.SMTPException subclasses it, so catching OSError would silently retry
# an auth failure or a refused recipient too -- on a fresh connection those only
# fail again, and the retry hides the real reason from the caller's log.
_DROPPED = (
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
    ConnectionError,   # reset / broken pipe
    TimeoutError,      # socket timeout on a stale session
)

# Upper bound for send_delay. A setting meant as "seconds" but typed as
# "messages per minute" would otherwise stall the scheduler thread for hours
# while it holds the job lock.
_MAX_SEND_DELAY = 10.0

# A bare addr-spec. Deliberately strict rather than RFC-complete: the value is
# written into a header, and anything with whitespace, a comma or an angle
# bracket is far more likely to be a mistake than an address we must support.
_ADDR_RE = re.compile(
    r"^[^\s<>@,;:\\\"]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$"
)

# Conservative: a Message-ID domain is injected straight into a header, so only
# a plain hostname passes. Anything else falls back to make_msgid()'s default.
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")

# Bad values already reported, so a misconfiguration logs once per process and
# not once per message of every run.
_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


class _UnfoldedHeader(UnstructuredHeader):
    """A header written on one line, exactly as given.

    The stdlib folds headers at 78 columns, and a value it cannot break --
    a URL has no spaces -- it rewrites as RFC 2047 encoded words instead.
    For List-Unsubscribe that turns `<https://...>` into `=?utf-8?q?...?=`,
    which no mailbox provider parses: the header ships, the unsubscribe
    button never appears, and reading msg[...] in a test shows nothing
    wrong because the damage only happens on serialisation."""

    def fold(self, *, policy):
        return "%s: %s%s" % (self.name, str(self), policy.linesep)


_registry = HeaderRegistry()
_registry.map_to_type("list-unsubscribe", _UnfoldedHeader)
# Only that header is exempt from folding; every other one is handled as
# before. RFC 5322's hard limit is 998 characters a line, and the URL is
# capped well under it in _add_unsubscribe.
_MSG_POLICY = _policy.default.clone(header_factory=_registry)


def valid_address(value: str) -> bool:
    """True if `value` is safe to put in a From/Reply-To header.

    ASCII only: a non-ASCII local part ships as an RFC 2047 encoded word,
    which inside an addr-spec is not an address at all -- every reply would
    bounce, the exact failure this check exists to prevent.
    """
    value = (value or "").strip()
    return value.isascii() and bool(_ADDR_RE.match(value))


def _domain_of(addr: str) -> str:
    """Domain of the From address, for the Message-ID. Empty when unusable."""
    domain = parseaddr(addr or "")[1].rpartition("@")[2].strip()
    return domain if _DOMAIN_RE.match(domain) else ""


def from_address(c: dict) -> str:
    """The address messages actually go out as.

    Shared with sender_warning() on purpose: while the two disagreed, the
    Settings page judged `smtp_from` whereas the mail went out as `smtp_user`,
    so the one setup the warning exists to catch -- host + user + password and
    no From, the minimal configuration most relays ask for -- was the one it
    got wrong.
    """
    return (c.get("from_addr") or "").strip() or (c.get("user") or "").strip()


def _build(c: dict, to: str, subject: str, body: str, html: str | None,
           unsubscribe_url: str | None = None) -> EmailMessage:
    msg = EmailMessage(policy=_MSG_POLICY)
    addr = from_address(c)
    # formataddr(("", addr)) -> bare addr; with a name -> "Name <addr>"
    msg["From"] = formataddr((" ".join((c.get("from_name") or "").split()), addr))
    msg["To"] = to
    msg["Subject"] = subject
    # smtplib.send_message() adds neither Date nor Message-ID, and a relay
    # filling them in for us is not guaranteed. Both missing is a measurable
    # spam-score penalty on its own (SpamAssassin MISSING_DATE / MISSING_MID).
    msg["Date"] = formatdate(localtime=True)
    # domain= keeps the Message-ID on the sending domain. Without it make_msgid()
    # uses the local hostname, which in a container is a random Docker id -- a
    # Message-ID that matches no domain reads as forged to a receiving filter.
    domain = _domain_of(addr)
    msg["Message-ID"] = make_msgid(domain=domain) if domain else make_msgid()
    reply_to = (c.get("reply_to") or "").strip()
    if reply_to and valid_address(reply_to):
        msg["Reply-To"] = reply_to
    elif reply_to:
        # ENV can set this without ever passing the Settings form. EmailMessage
        # raises on a header holding CR/LF, which would turn one bad character
        # in one setting into "every notification fails" -- drop the header and
        # still deliver the message.
        _warn_once("reply_to:%s" % reply_to,
                   "ignoring malformed reply_to, header dropped: %r" % reply_to)
    # RFC 3834: keeps out-of-office autoresponders from answering a reminder
    # (and from looping, when the reply lands back on the sending mailbox).
    msg["Auto-Submitted"] = "auto-generated"
    _add_unsubscribe(msg, unsubscribe_url)
    msg.set_content(body)  # plain-text part (fallback)
    if html:
        msg.add_alternative(html, subtype="html")  # multipart/alternative
    return msg


# What may sit between the angle brackets of List-Unsubscribe. The URL is
# built from the admin's public address plus our own token, but it goes
# into a header, so it gets the same suspicion as every other value here.
_UNSUB_URL_RE = re.compile(r"^https?://[^\s<>\"]+$")


def _add_unsubscribe(msg: EmailMessage, url: str | None) -> None:
    """List-Unsubscribe, plus the one-click marker when the URL allows it.

    Gmail and Yahoo require both headers from bulk senders, and show their
    own unsubscribe button from them. The alternative a reader reaches
    for when there is no such button is "report spam", which costs the
    sending domain far more than a lost subscriber.

    List-Unsubscribe-Post only over https: RFC 8058 demands it, and a
    provider that sees the marker on a plain-http link ignores the pair.
    On http the plain header still goes out and is still honoured as a link."""
    url = (url or "").strip()
    # The length cap keeps the unfolded line far from the 998 limit.
    if not _UNSUB_URL_RE.match(url) or len(url) > 800:
        return
    msg["List-Unsubscribe"] = f"<{url}>"
    if url.lower().startswith("https://"):
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"


def _connect(c: dict):
    """Open an authenticated SMTP connection. Caller owns it (quit/close)."""
    ctx = ssl.create_default_context()
    # ponytail: port 465 = implicit TLS (SMTPS); a setting only if some server
    # ever needs SMTPS on another port.
    if c["port"] == 465:
        server = smtplib.SMTP_SSL(c["host"], c["port"], timeout=15, context=ctx)
    else:
        server = smtplib.SMTP(c["host"], c["port"], timeout=15)
    try:
        if c["tls"] and not isinstance(server, smtplib.SMTP_SSL):
            # Verify the server certificate + hostname: the bare starttls() uses an
            # unverified context, so an on-path attacker could MITM the connection
            # and capture the SMTP login credentials below.
            server.starttls(context=ctx)
        if c["user"]:
            server.login(c["user"], c["password"])
    except Exception:
        # Don't leak the socket when the handshake or the login fails: without
        # this the connection stays open until the GC gets to it.
        with contextlib.suppress(Exception):
            server.close()
        raise
    return server


def _is_dropped(exc: BaseException) -> bool:
    """True when `exc` is a dead connection a reconnect can recover."""
    if isinstance(exc, _DROPPED):
        return True
    # "421 Too many messages" is how most relays enforce a per-connection cap.
    # smtplib closes the socket and raises a plain response error, so without
    # this the message that hit the cap is lost and only the NEXT one
    # reconnects -- one dropped email every time the cap is reached.
    return isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code == 421


class _BulkConn:
    """The connection a bulk_session() reuses, reopened if the server drops it."""

    def __init__(self) -> None:
        self._server = None
        self._last = None  # not 0.0: that is a valid monotonic() reading

    def _server_for(self, c: dict):
        if self._server is None:
            self._server = _connect(c)
        return self._server

    def send(self, c: dict, msg: EmailMessage) -> None:
        self._throttle(c)
        # Outside the try on purpose: a connection that never opened is not
        # something a reconnect fixes. Retrying it here would double every
        # timeout of a run whose relay is simply unreachable -- the run that is
        # already the slowest, with the scheduler's job lock held throughout.
        server = self._server_for(c)
        try:
            try:
                server.send_message(msg)
            except Exception as exc:
                if not _is_dropped(exc):
                    raise  # auth / refused recipient: a retry only repeats it
                # Expected on a long run: servers close idle sessions and cap
                # the messages per connection. One reconnect, one retry -- a
                # second failure propagates to the caller, which logs it and
                # retries on the next run.
                log.info("SMTP connection dropped mid-run, reconnecting: %s", exc)
                self.close()
                self._server_for(c).send_message(msg)
        finally:
            # Updated after a failure too: the next message should keep the
            # pace, not fire immediately because this one never counted.
            self._last = time.monotonic()

    def _throttle(self, c: dict) -> None:
        """Keep at least `send_delay` seconds between two messages. 0 = off."""
        delay = min(float(c.get("send_delay") or 0.0), _MAX_SEND_DELAY)
        if delay <= 0 or self._last is None:
            return  # nothing sent yet on this connection
        remaining = delay - (time.monotonic() - self._last)
        if remaining > 0:
            time.sleep(remaining)

    def close(self) -> None:
        if self._server is None:
            return
        server, self._server = self._server, None
        try:
            server.quit()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
        finally:
            # quit() sends QUIT and only then closes, so a raising command
            # leaves the socket open. The stdlib's own __exit__ closes in a
            # finally for the same reason.
            with contextlib.suppress(Exception):
                server.close()


@contextlib.contextmanager
def bulk_session():
    """Reuse one SMTP connection for everything sent inside the block.

    A TLS handshake per message is what a provider's abuse heuristics read as a
    burst, and it costs a full round-trip each time. Outside this block nothing
    changes: send_email() still opens and closes its own connection, so a web
    request is never blocked on a connection the scheduler is using.

    The connection is opened lazily on the first actual send, so wrapping a run
    that sends nothing (or that runs with SMTP unconfigured) costs nothing.
    """
    conn = _BulkConn()
    token = _bulk.set(conn)
    try:
        yield
    finally:
        _bulk.reset(token)
        conn.close()


def send_email(to: str, subject: str, body: str, html: str | None = None, *,
               unsubscribe_url: str | None = None) -> bool:
    c = runtime_config.smtp_config()
    to = (to or "").strip()
    if not c["host"] or not to:
        return False
    msg = _build(c, to, subject, body, html, unsubscribe_url)
    conn = _bulk.get()
    if conn is not None:
        conn.send(c, msg)
        return True
    with _connect(c) as server:
        server.send_message(msg)
    return True


# ---- Sender alignment (a Settings-page hint; never blocks a send) ----

# Domains whose published DMARC policy rejects or quarantines mail that did not
# leave their own servers. Sending "from" one of these through an unrelated
# relay is the most common reason a self-hosted instance lands in spam: SPF and
# DKIM both fail, and the policy says what to do about it.
#
# The value lists the host suffixes that ARE those servers. Checking that the
# SMTP host merely sits under the mailbox domain is not enough: Microsoft
# serves hotmail.com from smtp-mail.outlook.com and Apple icloud.com from
# smtp.mail.me.com, so that shortcut calls correct setups broken -- and a
# warning that cries wolf gets ignored on the one occasion it is right.
_GOOGLE = ("gmail.com", "googlemail.com")
_MICROSOFT = ("outlook.com", "hotmail.com", "live.com", "msn.com", "office365.com")
_YAHOO = ("yahoo.com", "yahoodns.net", "ymail.com")
_APPLE = ("icloud.com", "me.com", "apple.com")
_GMX = ("gmx.com", "gmx.net", "gmx.de", "gmx.at", "gmx.it")
_PROTON = ("proton.me", "protonmail.com", "protonmail.ch")
_LIBERO = ("libero.it", "iol.it", "inwind.it")
_TELECOM = ("alice.it", "tin.it", "telecomitalia.it")

_FREEMAIL: dict[str, tuple[str, ...]] = {
    **{d: _GOOGLE for d in _GOOGLE},
    **{d: _MICROSOFT for d in ("outlook.com", "outlook.it", "hotmail.com",
                               "hotmail.it", "live.com", "live.it", "msn.com")},
    **{d: _YAHOO for d in ("yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr",
                           "yahoo.de", "yahoo.es", "ymail.com")},
    **{d: _APPLE for d in ("icloud.com", "me.com", "mac.com")},
    **{d: _GMX for d in _GMX},
    **{d: _PROTON for d in ("proton.me", "protonmail.com")},
    **{d: _LIBERO for d in _LIBERO},
    **{d: _TELECOM for d in ("alice.it", "tin.it")},
    "aol.com": ("aol.com", *_YAHOO),
    "web.de": ("web.de",),
    "t-online.de": ("t-online.de",),
    "zoho.com": ("zoho.com", "zoho.eu"),
    "fastmail.com": ("fastmail.com", "messagingengine.com"),
    "mail.ru": ("mail.ru",),
    "yandex.ru": ("yandex.ru", "yandex.net"),
    "laposte.net": ("laposte.net",),
    "free.fr": ("free.fr",),
    "orange.fr": ("orange.fr", "wanadoo.fr"),
    # Italian consumer providers: this instance's own audience.
    "virgilio.it": ("virgilio.it",),
    "tiscali.it": ("tiscali.it",),
    "fastwebnet.it": ("fastwebnet.it", "fastweb.it"),
    "poste.it": ("poste.it", "posteitaliane.it"),
    "email.it": ("email.it",),
}


def sender_warning(c: dict | None = None) -> str | None:
    """Code for a From/relay mismatch that costs deliverability, else None.

    Surfaced on the Settings page only: the configuration is the admin's call
    and a send is never blocked by this.
    """
    c = c or runtime_config.smtp_config()
    if not c["host"]:
        return None  # SMTP off; nothing to warn about yet
    # from_address(), not c["from_addr"]: with no From set the mail goes out as
    # the SMTP username, and that is the address a receiver judges.
    addr = parseaddr(from_address(c))[1]
    if "@" not in addr:
        # Neither a From address nor a username shaped like one -- plenty of
        # relays take a bare login as the username. The header would carry
        # something no receiver can deliver to.
        return "no_from"
    domain = addr.rpartition("@")[2].lower()
    host = (c["host"] or "").strip().lower()
    if host in ("localhost", "127.0.0.1", "::1", "[::1]"):
        # Local bridge or relay (Proton Bridge, a system Postfix): it
        # forwards under its own identity, so there is nothing to compare.
        return None
    own = _FREEMAIL.get(domain)
    # `domain` itself is always acceptable: the map lists a provider's shared
    # mail hosts, and the per-country aliases (yahoo.co.uk, hotmail.it) are not
    # among them -- without this, their own SMTP host reads as a mismatch.
    if own and not any(host == s or host.endswith("." + s) for s in (domain, *own)):
        return "freemail"
    return None
