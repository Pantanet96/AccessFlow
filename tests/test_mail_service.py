import smtplib

from app import runtime_config
from app.services import mail_service


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.starttls_called = False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.starttls_called = True

    def login(self, u, p):
        pass

    def send_message(self, m):
        pass


class _FakeSSL(_FakeSMTP):
    pass


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
