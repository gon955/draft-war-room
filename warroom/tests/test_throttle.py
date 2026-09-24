"""The rate limit on the unauthenticated endpoints (deployment hardening).

/auth/register and /auth/login are the only routes reachable without a token,
and each spends a full argon2 hash. On a one-CPU container they are therefore
the cheapest way to burn the whole server, which is what warroom.throttle
exists to make expensive.

The limiter itself is unit-tested here rather than only through the API,
because the two properties that matter — the window resetting, and the tracking
table being bounded — are both awkward to provoke over HTTP and trivial to
state directly.
"""

import pytest

from warroom.config import get_settings
from warroom.throttle import FixedWindowLimiter, auth_limiter


class TestFixedWindowLimiter:
    def test_allows_up_to_the_limit(self):
        limiter = FixedWindowLimiter(limit=3, window=60.0)

        assert [limiter.check("ip", now=0.0) for _ in range(3)] == [None, None, None]

    def test_refuses_past_the_limit_and_reports_the_wait(self):
        limiter = FixedWindowLimiter(limit=2, window=60.0)
        limiter.check("ip", now=0.0)
        limiter.check("ip", now=0.0)

        retry_after = limiter.check("ip", now=10.0)

        assert retry_after == pytest.approx(50.0)

    def test_the_window_resets(self):
        limiter = FixedWindowLimiter(limit=1, window=60.0)
        limiter.check("ip", now=0.0)

        assert limiter.check("ip", now=30.0) is not None
        assert limiter.check("ip", now=60.0) is None

    def test_clients_are_counted_separately(self):
        limiter = FixedWindowLimiter(limit=1, window=60.0)

        assert limiter.check("first", now=0.0) is None
        assert limiter.check("second", now=0.0) is None

    def test_a_limit_of_zero_disables_it(self):
        """auth_max_attempts=0 is the documented off switch, so it must not
        instead mean 'refuse everything' — which is what a naive `count >
        limit` would do on the very first request."""
        limiter = FixedWindowLimiter(limit=0, window=60.0)

        assert [limiter.check("ip", now=0.0) for _ in range(50)] == [None] * 50

    def test_the_tracking_table_is_bounded(self):
        """The key comes from a caller-controlled header, so an unbounded dict
        here would BE the memory exhaustion the limiter exists to prevent —
        just reached by rotating addresses instead of by hashing."""
        limiter = FixedWindowLimiter(limit=5, window=60.0, max_keys=10)

        for i in range(1000):
            limiter.check(f"ip-{i}", now=0.0)

        assert len(limiter._hits) == 10

    def test_eviction_drops_the_least_recently_seen(self):
        limiter = FixedWindowLimiter(limit=5, window=60.0, max_keys=2)
        limiter.check("old", now=0.0)
        limiter.check("kept", now=0.0)
        limiter.check("kept", now=0.0)
        limiter.check("new", now=0.0)

        assert "old" not in limiter._hits
        assert set(limiter._hits) == {"kept", "new"}


class TestAuthEndpointsAreThrottled:
    def test_login_eventually_429s(self, client):
        limit = get_settings().auth_max_attempts
        body = {"email": "nobody@example.com", "password": "not-the-password"}

        codes = [client.post("/auth/login", json=body).status_code for _ in range(limit + 1)]

        assert codes[:limit] == [401] * limit
        assert codes[-1] == 429

    def test_register_shares_the_same_budget(self, client):
        """One budget for the router, not one per handler: otherwise the cheap
        way round a login limit is to alternate with register, which costs the
        server exactly as much."""
        limit = get_settings().auth_max_attempts
        for _ in range(limit):
            client.post("/auth/login", json={"email": "a@example.com", "password": "12345678"})

        r = client.post("/auth/register", json={"email": "new@example.com", "password": "12345678"})

        assert r.status_code == 429

    def test_the_429_says_when_to_come_back(self):
        """Retry-After is the difference between a client backing off and a
        client hammering. It is also in CORS expose_headers, or the browser
        could not read it."""
        limiter = FixedWindowLimiter(limit=1, window=60.0)
        limiter.check("ip", now=0.0)

        assert limiter.check("ip", now=0.0) == pytest.approx(60.0)

    def test_a_throttled_caller_is_refused_before_any_hashing(self, client, monkeypatch):
        """The whole point is spending no CPU on a refused attempt. A 429 that
        still hashed first would rate-limit the response and nothing else."""
        limit = get_settings().auth_max_attempts
        body = {"email": "nobody@example.com", "password": "not-the-password"}
        for _ in range(limit):
            client.post("/auth/login", json=body)

        called = False

        def explode() -> None:
            nonlocal called
            called = True

        monkeypatch.setattr("warroom.api.routes.auth.dummy_verify", explode)

        assert client.post("/auth/login", json=body).status_code == 429
        assert called is False

    def test_authenticated_routes_are_not_throttled(self, client, auth_a):
        """The limit defends the two unauthenticated, hash-spending endpoints.
        A board read costs a query and is bounded by having an account at all,
        so throttling it would only break a legitimate heavy user."""
        codes = {client.get("/boards", headers=auth_a).status_code for _ in range(60)}

        assert codes == {200}


class TestLimiterAccessor:
    def test_returns_the_same_instance(self):
        """Routes hold whatever this returns, so a fresh object per call would
        make the conftest reset silently reset something nobody is using."""
        assert auth_limiter() is auth_limiter()
