"""In-process login throttling / lockout for local accounts.

PONYTAIL: deliberately the laziest thing that works for a single-container app.
- State lives in a module-level dict. It RESETS ON PROCESS RESTART — a restart
  forgives all lockouts. Fine for one container; swap _BUCKETS for Redis if this
  ever runs multi-replica or must survive restarts.
- Uses time.monotonic() (not wall clock) so the window is immune to NTP/clock steps.
- One global lock guards the dict. Login is low-QPS; no per-key locks needed.
"""
import threading
import time

MAX_FAILS = 5             # lock after this many fails within the window
WINDOW_SECONDS = 15 * 60  # 15 min: counting window AND lockout duration

# key -> (fail_count, first_fail_monotonic)
_BUCKETS: dict[str, tuple[int, float]] = {}
_LOCK = threading.Lock()


def _keys(username: str, ip: str) -> tuple[str, str]:
    # Username lowercased so "Admin"/"admin" share a bucket. IP counted
    # independently so rotating usernames from one host still trips the lock.
    return (f"u:{(username or '').strip().lower()}", f"ip:{ip or 'unknown'}")


def check_locked(username: str, ip: str) -> int | None:
    """If (username OR ip) is currently locked, return remaining seconds (>0),
    else None. Generic by design: caller must not reveal which key matched."""
    now = time.monotonic()
    with _LOCK:
        for k in _keys(username, ip):
            entry = _BUCKETS.get(k)
            if entry is None:
                continue
            count, first = entry
            if now - first >= WINDOW_SECONDS:
                del _BUCKETS[k]
                continue
            if count >= MAX_FAILS:
                return int(WINDOW_SECONDS - (now - first)) + 1
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
        entry = _BUCKETS.get(ip_key)
        if entry is None:
            return None
        count, first = entry
        if now - first >= WINDOW_SECONDS:
            del _BUCKETS[ip_key]
            return None
        if count >= MAX_FAILS:
            return int(WINDOW_SECONDS - (now - first)) + 1
    return None


def register_failure(username: str, ip: str) -> int | None:
    """Record one failed attempt against both keys. Returns remaining lock
    seconds if this failure caused (or sustains) a lock, else None."""
    now = time.monotonic()
    locked_for: int | None = None
    with _LOCK:
        # Prune expired entries so a spray of unique usernames can't grow the
        # dict without bound (the lazy per-key cleanup never sees those keys again).
        stale = [k for k, (_, first) in _BUCKETS.items()
                 if now - first >= WINDOW_SECONDS]
        for k in stale:
            del _BUCKETS[k]
        for k in _keys(username, ip):
            count, first = _BUCKETS.get(k, (0, now))
            if now - first >= WINDOW_SECONDS:   # stale window -> restart count
                count, first = 0, now
            count += 1
            _BUCKETS[k] = (count, first)
            if count >= MAX_FAILS:
                rem = int(WINDOW_SECONDS - (now - first)) + 1
                locked_for = rem if locked_for is None else max(locked_for, rem)
    return locked_for


def reset(username: str, ip: str) -> None:
    """Clear counters for both keys. Call on successful login."""
    with _LOCK:
        for k in _keys(username, ip):
            _BUCKETS.pop(k, None)
