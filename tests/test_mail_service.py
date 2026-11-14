import smtplib

import pytest

from app import runtime_config
from app.services import mail_service


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.starttls_called = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False
        self.closed = False
        self.quit_raises = None  # exception quit() raises, for the close path
        self.fail_next = None  # exception the next send_message() raises
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()  # stdlib SMTP.__exit__ quits, and quit() closes
        return False

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, u, p):
        self.logged_in = (u, p)

    def send_message(self, m):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.sent.append(m)

    def quit(self):
        self.quit_called = True
        if self.quit_raises is not None:
            raise self.quit_raises

    def close(self):
        self.closed = True


class _FakeSSL(_FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    """Fake transport. Returns the list of connections opened by the test."""
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    _FakeSMTP.instances.clear()
    mail_service._warned.clear()  # module-level "log this once" state
    return _FakeSMTP.instances


def _cfg(monkeypatch, **over):
    c = dict(host="mx.example.com", port=587, user="", password="",
             from_addr="noreply@accessflow.test", from_name="", tls=True)
    c.update(over)
    monkeypatch.setattr(runtime_config, "smtp_config", lambda: c)
    return c


def test_port_465_uses_implicit_tls(monkeypatch):
    # SMTP() on 465 hangs/fails: the server expects TLS from the first byte.
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSSL)
    base = dict(host="mx", user="", password="", from_addr="a@b.c", from_name="", tls=True)
    for port, cls, starttls in ((465, _FakeSSL, False), (587, _FakeSMTP, True)):
        monkeypatch.setattr(runtime_config, "smtp_config", lambda: dict(base, port=port))
        _FakeSMTP.instances.clear()
        assert mail_service.send_email("x@y.z", "s", "b")
        (srv,) = _FakeSMTP.instances
        assert type(srv) is cls and srv.starttls_called is starttls


# ---- headers ----

def test_sends_date_message_id_and_auto_submitted(smtp, monkeypatch):
    # smtplib.send_message() adds neither Date nor Message-ID, and a relay
    # filling them in is not guaranteed: both missing is a spam-score penalty.
    _cfg(monkeypatch)
    mail_service.send_email("user@x.test", "Subject", "body")
    (msg,) = smtp[0].sent
    assert msg["Date"]
    # On the sending domain, not the container's random hostname.
    assert msg["Message-ID"].endswith("@accessflow.test>")
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Reply-To"] is None  # absent unless configured


def test_reply_to_only_when_configured(smtp, monkeypatch):
    _cfg(monkeypatch, reply_to="support@accessflow.test")
    mail_service.send_email("user@x.test", "s", "b")
    assert smtp[0].sent[0]["Reply-To"] == "support@accessflow.test"


def test_from_falls_back_to_the_smtp_username(smtp, monkeypatch):
    # Host + user + password with no From is the minimal setup most relays ask
    # for, and it is the username that then reaches the header.
    _cfg(monkeypatch, from_addr="", user="relay@accessflow.test")
    mail_service.send_email("user@x.test", "s", "b")
    msg = smtp[0].sent[0]
    assert msg["From"] == "relay@accessflow.test"
    assert msg["Message-ID"].endswith("@accessflow.test>")


@pytest.mark.parametrize("value, ok", [
    ("support@accessflow.test", True),
    ("first.last+tag@sub.accessflow.test", True),
    ("Name <a@b.test>", False),  # bare addr-spec only
    ("a@b.test\r\nBcc: victim@x.test", False),
    ("two addresses@b.test", False),
    ("nonsense", False),
    ("a@local", False),
    ("", False),
])
def test_valid_address(value, ok):
    assert mail_service.valid_address(value) is ok


def test_a_malformed_reply_to_is_dropped_not_fatal(smtp, monkeypatch):
    # EmailMessage raises on CR/LF in a header value. ENV sets this without ever
    # passing the Settings form, and one bad character in one setting must not
    # take down every notification the instance sends.
    _cfg(monkeypatch, reply_to="a@b.test\r\nBcc: victim@x.test")
    assert mail_service.send_email("user@x.test", "s", "b") is True
    msg = smtp[0].sent[0]
    assert msg["Reply-To"] is None
    assert "victim@x.test" not in msg.as_string()


def test_a_malformed_reply_to_logs_once_not_per_message(smtp, monkeypatch, caplog):
    # A run of 500 reminders must not write 500 identical warnings.
    _cfg(monkeypatch, reply_to="broken")
    with caplog.at_level("WARNING", logger="pum.mail"):
        for i in range(3):
            mail_service.send_email("u%d@x.test" % i, "s", "b")
    assert sum("reply_to" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("addr, expected", [
    ("noreply@accessflow.test", "accessflow.test"),
    ("Name <noreply@accessflow.test>", "accessflow.test"),
    ("noreply@sub.accessflow.test", "sub.accessflow.test"),
    ("noreply@local", ""),  # no TLD: not a routable domain
    ("", ""),
])
def test_domain_of(addr, expected):
    assert mail_service._domain_of(addr) == expected


def test_message_id_falls_back_when_the_from_domain_is_unusable(smtp, monkeypatch):
    # The value goes straight into a header, so anything but a plain hostname
    # must fall back instead of being interpolated.
    _cfg(monkeypatch, from_addr="noreply@local")
    mail_service.send_email("user@x.test", "s", "b")
    mid = smtp[0].sent[0]["Message-ID"]
    assert mid.startswith("<") and mid.endswith(">") and "@local>" not in mid


def test_html_is_sent_as_the_alternative_part(smtp, monkeypatch):
    _cfg(monkeypatch)
    mail_service.send_email("user@x.test", "s", "plain body", html="<p>rich</p>")
    msg = smtp[0].sent[0]
    assert msg.get_content_type() == "multipart/alternative"
    assert msg.get_body(("plain",)).get_content().strip() == "plain body"
    assert "<p>rich</p>" in msg.get_body(("html",)).get_content()


# ---- bulk_session: one connection per run instead of one per message ----

def test_each_send_opens_its_own_connection_outside_a_bulk_session(smtp, monkeypatch):
    _cfg(monkeypatch)
    for i in range(3):
        mail_service.send_email("u%d@x.test" % i, "s", "b")
    assert len(smtp) == 3
    assert all(s.closed for s in smtp), "the context manager must close each one"


def test_bulk_session_reuses_one_connection_and_closes_it(smtp, monkeypatch):
    _cfg(monkeypatch)
    with mail_service.bulk_session():
        for i in range(3):
            mail_service.send_email("u%d@x.test" % i, "s", "b")
        assert not smtp[0].quit_called  # still open mid-run
    assert len(smtp) == 1 and len(smtp[0].sent) == 3
    assert smtp[0].quit_called


def test_the_socket_is_closed_even_when_quit_raises(smtp, monkeypatch):
    # quit() sends QUIT and only then closes: a raising command would otherwise
    # leave the socket open for the GC to find.
    _cfg(monkeypatch)
    with mail_service.bulk_session():
        mail_service.send_email("a@x.test", "s", "b")
        smtp[0].quit_raises = smtplib.SMTPServerDisconnected("already gone")
    assert smtp[0].closed


def test_bulk_session_sending_nothing_opens_no_connection(smtp, monkeypatch):
    # The daily scan wraps itself unconditionally; a quiet day must stay silent.
    _cfg(monkeypatch)
    with mail_service.bulk_session():
        pass
    assert smtp == []


def test_bulk_session_opens_no_connection_when_smtp_is_unconfigured(smtp, monkeypatch):
    _cfg(monkeypatch, host="")
    with mail_service.bulk_session():
        assert mail_service.send_email("u@x.test", "s", "b") is False
    assert smtp == []


@pytest.mark.parametrize("dropped", [
    smtplib.SMTPServerDisconnected("idle timeout"),
    ConnectionResetError("reset by peer"),
    TimeoutError("stale socket"),
    smtplib.SMTPSenderRefused(421, b"Too many messages", "a@b.test"),
])
def test_bulk_session_reconnects_after_a_dropped_connection(smtp, monkeypatch, dropped):
    # Servers close idle sessions and cap messages per connection (the 421):
    # a long run must not lose the message that lands on the dead socket.
    _cfg(monkeypatch)
    with mail_service.bulk_session():
        mail_service.send_email("a@x.test", "s", "b")
        smtp[0].fail_next = dropped
        mail_service.send_email("b@x.test", "s", "b")
    assert len(smtp) == 2
    assert len(smtp[0].sent) == 1 and len(smtp[1].sent) == 1


def test_bulk_session_does_not_retry_a_refused_recipient(smtp, monkeypatch):
    # Every smtplib.SMTPException subclasses OSError, so a too-broad except here
    # would reconnect and repeat the failure instead of surfacing it to the
    # caller, which logs it and retries on the next run.
    _cfg(monkeypatch)
    with pytest.raises(smtplib.SMTPRecipientsRefused):
        with mail_service.bulk_session():
            mail_service.send_email("a@x.test", "s", "b")
            smtp[0].fail_next = smtplib.SMTPRecipientsRefused({"b@x.test": (550, b"no")})
            mail_service.send_email("b@x.test", "s", "b")
    assert len(smtp) == 1  # no reconnect


def test_a_failure_to_connect_is_not_retried(smtp, monkeypatch):
    # A relay that is simply down raises TimeoutError from connect(), which is
    # also what a dropped socket raises. Retrying it here would pay the 15s
    # timeout twice for every message of a run that is already the slowest --
    # with the scheduler's job lock held throughout.
    _cfg(monkeypatch)
    attempts = []

    def boom(c):
        attempts.append(1)
        raise TimeoutError("unreachable")

    monkeypatch.setattr(mail_service, "_connect", boom)
    with mail_service.bulk_session():
        with pytest.raises(TimeoutError):
            mail_service.send_email("a@x.test", "s", "b")
    assert len(attempts) == 1


def test_auth_failure_inside_a_bulk_session_is_attempted_once(smtp, monkeypatch):
    # Auth fails in _connect, never in send_message: the real shape of the
    # failure must not turn into two login attempts per message.
    _cfg(monkeypatch, user="u", password="bad")
    attempts = []

    def boom(c):
        attempts.append(1)
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(mail_service, "_connect", boom)
    with mail_service.bulk_session():
        with pytest.raises(smtplib.SMTPAuthenticationError):
            mail_service.send_email("a@x.test", "s", "b")
    assert len(attempts) == 1


def test_a_bulk_connection_is_not_reused_after_the_block(smtp, monkeypatch):
    _cfg(monkeypatch)
    with mail_service.bulk_session():
        mail_service.send_email("a@x.test", "s", "b")
    mail_service.send_email("b@x.test", "s", "b")
    assert len(smtp) == 2


def test_send_delay_throttles_between_messages(smtp, monkeypatch):
    _cfg(monkeypatch, send_delay=0.5)
    slept = []
    monkeypatch.setattr(mail_service.time, "sleep", slept.append)
    # Freeze the clock so a slow CI run can't eat the window and make this
    # flaky. The last reading repeats once the script runs out, so the test
    # is tied to the behaviour and not to the number of monotonic() calls.
    ticks = iter([100.0, 100.1, 100.5])
    held = [100.0]

    def clock():
        held[0] = next(ticks, held[0])
        return held[0]

    monkeypatch.setattr(mail_service.time, "monotonic", clock)
    with mail_service.bulk_session():
        mail_service.send_email("a@x.test", "s", "b")  # first: nothing to wait for
        mail_service.send_email("b@x.test", "s", "b")
    assert slept == [pytest.approx(0.4)]


def test_send_delay_is_capped(smtp, monkeypatch):
    # "60" is a plausible typo for "60 messages per hour"; uncapped it stalls
    # the scheduler thread for hours while it holds the job lock.
    _cfg(monkeypatch, send_delay=600)
    slept = []
    monkeypatch.setattr(mail_service.time, "sleep", slept.append)
    with mail_service.bulk_session():
        mail_service.send_email("a@x.test", "s", "b")
        mail_service.send_email("b@x.test", "s", "b")
    assert slept and slept[0] <= 10.0


def test_no_delay_configured_never_sleeps(smtp, monkeypatch):
    _cfg(monkeypatch)
    monkeypatch.setattr(mail_service.time, "sleep",
                        lambda s: pytest.fail("slept with send_delay=0"))
    with mail_service.bulk_session():
        mail_service.send_email("a@x.test", "s", "b")
        mail_service.send_email("b@x.test", "s", "b")


# ---- sender alignment (Settings-page hint) ----

@pytest.mark.parametrize("host, from_addr, user, expected", [
    # Correct free-mail setups: the mail does leave the provider's own servers.
    # The SMTP host is rarely under the mailbox domain, which is why a plain
    # "host endswith domain" check flagged every one of these as broken.
    ("smtp.gmail.com", "me@gmail.com", "", None),
    ("smtp.gmail.com", "me@googlemail.com", "", None),
    ("smtp-mail.outlook.com", "me@hotmail.com", "", None),
    ("smtp-mail.outlook.com", "me@live.it", "", None),
    ("smtp.office365.com", "me@outlook.com", "", None),
    ("smtp.mail.me.com", "me@icloud.com", "", None),
    ("smtp.mail.yahoo.com", "me@yahoo.it", "", None),
    ("smtp.libero.it", "me@inwind.it", "", None),
    # A per-country alias reached through its own host: the map lists a
    # provider's shared hosts, so the domain itself must pass on its own.
    ("smtp.yahoo.co.uk", "me@yahoo.co.uk", "", None),
    ("smtp.hotmail.it", "me@hotmail.it", "", None),
    # Broken: free-mail From relayed elsewhere -> SPF and DKIM both fail.
    ("mx.myrelay.net", "me@gmail.com", "", "freemail"),
    ("smtp.sendgrid.net", "me@hotmail.com", "", "freemail"),
    ("smtp.notgmail.com", "me@gmail.com", "", "freemail"),  # suffix, not subdomain
    ("mx.myrelay.net", "me@poste.it", "", "freemail"),
    # No From: the username reaches the header, so judge THAT.
    ("mx.myrelay.net", "", "me@gmail.com", "freemail"),
    ("mx.myrelay.net", "", "relay-login-123", "no_from"),
    ("mx.myrelay.net", "", "", "no_from"),
    # A local bridge or relay (Proton Bridge, a system Postfix): the real
    # forwarding happens downstream under its own identity, so there is no
    # alignment here to judge.
    ("127.0.0.1", "me@proton.me", "", None),
    ("localhost", "me@gmail.com", "", None),
    # Own domain, and SMTP switched off.
    ("mx.myrelay.net", "noreply@mydomain.test", "", None),
    ("mx.myrelay.net", "AccessFlow <noreply@mydomain.test>", "", None),
    ("", "me@gmail.com", "", None),
])
def test_sender_warning(host, from_addr, user, expected):
    assert mail_service.sender_warning(
        {"host": host, "from_addr": from_addr, "user": user}) == expected


def test_sender_warning_agrees_with_the_address_actually_sent(smtp, monkeypatch):
    # The two read the same helper on purpose: while they disagreed, Settings
    # judged smtp_from while the mail went out as smtp_user.
    c = _cfg(monkeypatch, from_addr="", user="me@gmail.com", host="mx.myrelay.net")
    mail_service.send_email("user@x.test", "s", "b")
    assert smtp[0].sent[0]["From"] == "me@gmail.com"
    assert mail_service.sender_warning(c) == "freemail"
