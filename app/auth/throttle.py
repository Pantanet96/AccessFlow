"""In-process login throttling / lockout for local accounts.

PONYTAIL: deliberately the laziest thing that works for a single-container app.
- State lives in module-level dicts. It RESETS ON PROCESS RESTART — a restart
  forgives all lockouts. Fine for one container; swap for Redis if this ever
  runs multi-replica or must survive restarts.
- Uses time.monotonic() (not wall clock) so the windows are immune to NTP/clock steps.
- One global lock guards the dicts. Login is low-QPS; no per-key locks needed.

Escalation: every MAX_FAILS failures within WINDOW_SECONDS lock the key for the
next step of LOCK_STEPS (5 min, 10, 15, ... up to 4 h, then 4 h each time). A
key with no failure for FORGET_SECONDS starts again from the first step.
"""
import threading
import time
from dataclasses import dataclass

MAX_FAILS = 5             # failures within the window that trigger a lock
WINDOW_SECONDS = 15 * 60  # counting window for those failures
LOCK_STEPS = (5, 10, 15, 30, 60, 120, 240)  # minutes, one step per lock in a row
FORGET_SECONDS = 24 * 3600  # quiet this long -> escalation starts over
PLEX_MAX_HITS = 20        # anonymous Plex sign-in requests per IP per window


@dataclass
class _State:
    fails: int = 0       # failures in the current window
    first: float = 0.0   # start of the current window
    level: int = 0       # locks so far (index into LOCK_STEPS)
    until: float = 0.0   # locked until (monotonic)
    last: float = 0.0    # last failure


_BUCKETS: dict[str, _State] = {}
_PLEX: dict[str, tuple[int, float]] = {}  # ip -> (hits, window start)
_LOCK = threading.Lock()


def _keys(username: str, ip: str) -> tuple[str, str]:
    # Username lowercased so "Admin"/"admin" share a bucket. IP counted
    # independently so rotating usernames from one host still trips the lock.
    return (f"u:{(username or '').strip().lower()}", f"ip:{ip or 'unknown'}")


def _remaining(st: _State | None, now: float) -> int | None:
    if st is None or st.until <= now:
        return None
    return int(st.until - now) + 1


def _prune(now: float) -> None:
    # Bound the dicts against a spray of unique usernames/IPs. A key that never
    # locked is only worth keeping for its window; one that did keeps its
    # escalation level until it has been quiet for FORGET_SECONDS.
    # Caller holds _LOCK.
    stale = [
        k for k, st in _BUCKETS.items()
        if st.until <= now
        and now - st.last >= (WINDOW_SECONDS if st.level == 0 else FORGET_SECONDS)
    ]
    for k in stale:
        del _BUCKETS[k]
    for k in [k for k, (_, first) in _PLEX.items() if now - first >= WINDOW_SECONDS]:
        del _PLEX[k]


def check_locked(username: str, ip: str) -> int | None:
    """If (username OR ip) is currently locked, return remaining seconds (>0),
    else None. Generic by design: caller must not reveal which key matched."""
    now = time.monotonic()
    with _LOCK:
        for k in _keys(username, ip):
            rem = _remaining(_BUCKETS.get(k), now)
            if rem is not None:
                return rem
    return None


def ip_locked(ip: str) -> int | None:
    """Remaining lock seconds for the IP key ALONE, else None.

    The per-IP lock is the hard, non-bypassable block (it throttles an attacker
    hammering from one host). The per-username lock is deliberately NOT consulted
    here: any IP can trip it, so treating it as a hard block let anyone lock a
    known user (e.g. the superadmin) out at will. Login checks this first and,
    when only the username is locked, still verifies credentials so the real user
    (who knows the password) gets in while wrong guesses stay rejected."""
    now = time.monotonic()
    _, ip_key = _keys("", ip)
    with _LOCK:
        return _remaining(_BUCKETS.get(ip_key), now)


def register_failure(username: str, ip: str) -> int | None:
    """Record one failed attempt against both keys. Returns remaining lock
    seconds if a key is locked after this failure, else None."""
    now = time.monotonic()
    locked_for: int | None = None
    with _LOCK:
        _prune(now)
        for k in _keys(username, ip):
            st = _BUCKETS.get(k)
            if st is None or now - st.last >= FORGET_SECONDS:
                st = _BUCKETS[k] = _State(first=now)
            if now - st.first >= WINDOW_SECONDS:  # stale window -> restart count
                st.fails, st.first = 0, now
            st.fails += 1
            st.last = now
            if st.fails >= MAX_FAILS:
                step = LOCK_STEPS[min(st.level, len(LOCK_STEPS) - 1)]
                st.until = now + step * 60
                st.level += 1
                st.fails, st.first = 0, now
            rem = _remaining(st, now)
            if rem is not None:
                locked_for = rem if locked_for is None else max(locked_for, rem)
    return locked_for


def plex_rate_limited(ip: str) -> int | None:
    """Count one anonymous Plex sign-in request (each one waits on plex.tv in a
    worker thread) for this IP. Remaining seconds once over PLEX_MAX_HITS, else
    None. A real sign-in is 2 requests, so the cap leaves room for retries."""
    now = time.monotonic()
    key = ip or "unknown"
    with _LOCK:
        _prune(now)
        count, first = _PLEX.get(key, (0, now))
        _PLEX[key] = (count + 1, first)
        if count + 1 > PLEX_MAX_HITS:
            return int(WINDOW_SECONDS - (now - first)) + 1
    return None


def reset(username: str, ip: str) -> None:
    """Clear counters (and escalation) for both keys. Call on successful login."""
    with _LOCK:
        for k in _keys(username, ip):
            _BUCKETS.pop(k, None)
