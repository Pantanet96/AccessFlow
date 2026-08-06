"""Login lockout state machine. Monkeypatches monotonic so the 15-min window
is exercised in milliseconds."""
import app.auth.throttle as t


def _clock(now):
    t._BUCKETS.clear()
    t.time.monotonic = lambda: now[0]


def test_lockout_state_machine(monkeypatch):
    now = [1000.0]
    _clock(now)
    U, IP = "admin", "10.0.0.5"

    # Fresh: unlocked.
    assert t.check_locked(U, IP) is None

    # MAX_FAILS-1 failures do NOT lock.
    for _ in range(t.MAX_FAILS - 1):
        assert t.register_failure(U, IP) is None
    assert t.check_locked(U, IP) is None

    # The MAX_FAILS-th failure locks.
    rem = t.register_failure(U, IP)
    assert rem is not None and 0 < rem <= t.WINDOW_SECONDS + 1
    assert t.check_locked(U, IP) is not None

    # reset() clears it (success path).
    t.reset(U, IP)
    assert t.check_locked(U, IP) is None

    # Window expiry forgives the lock without a restart.
    for _ in range(t.MAX_FAILS):
        t.register_failure(U, IP)
    assert t.check_locked(U, IP) is not None
    now[0] += t.WINDOW_SECONDS + 1
    assert t.check_locked(U, IP) is None


def test_ip_lockout_defeats_username_rotation(monkeypatch):
    now = [5000.0]
    _clock(now)
    IP = "10.0.0.9"
    # Same IP, different usernames each time still trips the IP key.
    for i in range(t.MAX_FAILS):
        t.register_failure(f"user{i}", IP)
    assert t.check_locked("brand-new-user", IP) is not None


def test_username_lockout_defeats_ip_rotation(monkeypatch):
    now = [9000.0]
    _clock(now)
    # Same username, rotating IPs still trips the username key.
    for i in range(t.MAX_FAILS):
        t.register_failure("victim", f"1.2.3.{i}")
    assert t.check_locked("victim", "9.9.9.9") is not None


def test_ip_locked_ignores_username_key(monkeypatch):
    now = [7000.0]
    _clock(now)
    # A username lock (rotating IPs) must NOT register as an IP lock: the login
    # hard-block keys on ip_locked only, so this is what stops the lockout DoS.
    for i in range(t.MAX_FAILS):
        t.register_failure("victim", f"1.2.3.{i}")
    assert t.check_locked("victim", "5.5.5.5") is not None  # username is locked
    assert t.ip_locked("5.5.5.5") is None                   # but the IP is not

    # A genuinely hammered IP does trip ip_locked.
    _clock(now)
    for _ in range(t.MAX_FAILS):
        t.register_failure("whoever", "5.5.5.5")
    assert t.ip_locked("5.5.5.5") is not None
