"""A fixed-window rate limit for the unauthenticated endpoints (SPEC 4).

/auth/register and /auth/login are the only two routes reachable with no token,
and each one spends a full argon2 hash. On a one-CPU machine that makes them the
cheapest way to burn the whole server, and there is nothing else in the stack
that says no — the platform's concurrency limit admits far more than the process
can afford.

WHAT THIS IS AND IS NOT. This bounds ATTEMPTS per address; security.py's
semaphore bounds CONCURRENT MEMORY. Only the second is a guarantee. uvicorn runs
with --forwarded-allow-ips='*' (the platform's proxy address is not fixed, so it
has to), which means X-Forwarded-For is caller-controlled and anyone willing to
vary it gets a fresh bucket every request. So treat this as what keeps honest
clients and casual brute force off the CPU, and the semaphore as what keeps the
machine alive when that is not enough. Do not let a future change to one be
justified by the existence of the other.

Per process, deliberately: with more than one machine the real limit is this
number times the machine count. That is fine for what it defends against, and a
shared counter would put a Redis round trip in front of every login to buy
precision this does not need.
"""

import threading
import time
from collections import OrderedDict

from fastapi import HTTPException, Request, status

from warroom.config import get_settings

# Hard cap on how many client addresses are tracked. The keys come from a
# header the caller controls, so an unbounded dict here IS the memory
# exhaustion this module is meant to prevent, just reached by a different road.
# At the cap the least recently seen bucket is dropped: an attacker rotating
# addresses evicts their own entries rather than anyone else's, and the worst
# case is that a limiter which was already being bypassed stops applying.
MAX_TRACKED_CLIENTS = 8192


class FixedWindowLimiter:
    """Counts hits per key in a wall-clock window, and forgets them after it.

    Fixed window rather than a sliding one or a token bucket: it is a dict of
    (start, count) with no per-key timer, the failure mode is well understood
    (up to 2x the limit across a window boundary), and 2x a limit chosen for
    CPU headroom is still comfortably inside the headroom.
    """

    def __init__(self, limit: int, window: float, max_keys: int = MAX_TRACKED_CLIENTS):
        self._limit = limit
        self._window = window
        self._max_keys = max_keys
        # OrderedDict as an LRU. Routes are sync `def`s running in the AnyIO
        # threadpool, so this is touched from many OS threads at once and
        # every read-modify-write below has to be under the lock.
        self._hits: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until reset."""
        if self._limit <= 0:
            return None

        now = time.monotonic() if now is None else now

        with self._lock:
            start, count = self._hits.get(key, (now, 0))

            if now - start >= self._window:
                start, count = now, 0

            count += 1
            self._hits[key] = (start, count)
            self._hits.move_to_end(key)

            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)

            if count > self._limit:
                return max(0.0, self._window - (now - start))
            return None

    def reset(self) -> None:
        """Drop all state. For tests, which must not inherit each other's hits."""
        with self._lock:
            self._hits.clear()


_auth_limiter: FixedWindowLimiter | None = None
_auth_limiter_lock = threading.Lock()


def auth_limiter() -> FixedWindowLimiter:
    """The process-wide limiter for /auth, built on first use.

    Not an lru_cache: tests need reset() on the SAME instance the routes hold,
    and a cache that can be cleared hands back a different object instead.
    """
    global _auth_limiter
    if _auth_limiter is None:
        with _auth_limiter_lock:
            if _auth_limiter is None:
                settings = get_settings()
                _auth_limiter = FixedWindowLimiter(
                    limit=settings.auth_max_attempts,
                    window=settings.auth_window_seconds,
                )
    return _auth_limiter


def client_key(request: Request) -> str:
    """The address to count against.

    request.client is None for ASGI transports that carry no peer — the test
    client among them — so this falls back to a single shared bucket rather
    than raising. That is the safe direction: an unattributable request is
    counted, not exempted.
    """
    client = request.client
    return client.host if client is not None else "unknown"


def throttle_auth(request: Request) -> None:
    """FastAPI dependency: 429 once an address is over the limit.

    Applied at the ROUTER, not inside each handler, so a new unauthenticated
    endpoint added to auth.py is covered by default instead of by remembering.
    """
    retry_after = auth_limiter().check(client_key(request))
    if retry_after is None:
        return

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many attempts. Try again shortly.",
        # Seconds, rounded up: a client told to wait 0 retries immediately and
        # is refused again, which reads as the endpoint being broken.
        headers={"Retry-After": str(max(1, int(retry_after) + 1))},
    )
