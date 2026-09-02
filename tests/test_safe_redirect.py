"""Redirect targets stay on-site, and the email regex stays unambiguous."""
import pytest

from app.security import safe_next_url
from app.services.users import valid_notify_email


@pytest.mark.parametrize(
    "raw",
    [
        "//evil.com",            # protocol-relative
        r"/\evil.com",           # backslash variant, same effect in browsers
        "/\t/evil.com",          # browsers strip the tab -> "//evil.com"
        "/\r\n//evil.com",
        "https://evil.com",
        "javascript:alert(1)",
        "",
    ],
)
def test_offsite_next_falls_back(raw):
    assert safe_next_url(raw, "/users") == "/users"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/users", "/users"),
        ("/users/3/subscription?x=1", "/users/3/subscription?x=1"),
        ("/evil.com", "/evil.com"),          # a path, not a host
        ("/\tevil.com", "/evil.com"),        # tab dropped, still one slash
    ],
)
def test_onsite_next_kept(raw, expected):
    assert safe_next_url(raw, "/users") == expected


@pytest.mark.parametrize("addr", ["a@b.c", "user.name@sub.example.co.uk"])
def test_email_accepted(addr):
    assert valid_notify_email(addr)


@pytest.mark.parametrize(
    "addr",
    [
        "a@b",          # no dot in the domain
        "a@b..c",       # empty label
        "a b@c.de",     # space could smuggle a second recipient
        "a@c.de,b@c.de",
        "a@.de",
        "@c.de",
    ],
)
def test_email_rejected(addr):
    assert not valid_notify_email(addr)


def test_email_regex_is_not_quadratic():
    r"""A long dotted non-match must not backtrack: the old pattern had "." in the
    classes on both sides of "\.", so this took seconds instead of microseconds."""
    import time

    payload = "a@" + "a." * 20000
    start = time.perf_counter()
    assert not valid_notify_email(payload)
    assert time.perf_counter() - start < 1.0
