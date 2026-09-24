"""Deployment hardening: hashing cost, hashing concurrency, and the probes.

None of this changes what the app computes. All of it decides whether the app
survives its first bad ten seconds in production, and none of it is visible
from a passing functional test — which is exactly why it is pinned here.

The numbers come from measurement, not preference. passlib's argon2 defaults
(m=65536 KiB, t=3, p=4) under twenty concurrent logins peaked at 899 MiB RSS,
against the 512 MB machine fly.toml asks for, reachable by anyone with curl and
no account. Cost alone does not fix that — argon2 holds its whole arena for the
duration of every hash, so peak memory is concurrency x memory_cost and only
the first term is under the server's control at request time.
"""

import threading

import pytest
from passlib.context import CryptContext

from warroom import security
from warroom.config import get_settings
from warroom.main import create_app
from warroom.security import (
    PasswordHashingBusy,
    dummy_verify,
    hash_password,
    verify_and_upgrade,
    verify_password,
)

# What passlib would have used if nothing here set the cost. Spelled out so the
# assertion below reads as "not this" rather than as an arbitrary number.
PASSLIB_DEFAULT_MEMORY_KIB = 65536


def hash_parameters(hashed: str) -> dict[str, int]:
    """m/t/p out of a PHC-format argon2 hash string."""
    params = hashed.split("$")[3]
    return {k: int(v) for k, v in (pair.split("=") for pair in params.split(","))}


class TestHashingCost:
    def test_hashes_use_the_configured_cost(self):
        settings = get_settings()

        params = hash_parameters(hash_password("a-sufficiently-long-password"))

        assert params == {
            "m": settings.argon2_memory_kib,
            "t": settings.argon2_time_cost,
            "p": settings.argon2_parallelism,
        }

    def test_the_cost_is_not_passlibs_default(self):
        """The regression that matters. Deleting the argon2__ arguments from
        security._context leaves every functional test green and quietly
        restores a 64 MiB-per-login server."""
        params = hash_parameters(hash_password("a-sufficiently-long-password"))

        assert params["m"] < PASSLIB_DEFAULT_MEMORY_KIB

    def test_a_hash_still_verifies(self):
        hashed = hash_password("correct-horse-battery-staple")

        assert verify_password("correct-horse-battery-staple", hashed) is True
        assert verify_password("not-the-password", hashed) is False

    def test_the_hash_is_never_the_password(self):
        assert "correct-horse" not in hash_password("correct-horse-battery-staple")


class TestLegacyHashes:
    """Lowering the cost must not lock anybody out.

    argon2 writes its parameters into the hash string, so an old hash keeps
    verifying at the OLD cost — which is the whole problem: until it is
    rewritten it still costs 64 MiB to check. Nothing about lowering the
    setting migrates it; only a successful login does.
    """

    @pytest.fixture
    def legacy_hash(self) -> str:
        expensive = CryptContext(
            schemes=["argon2"],
            argon2__memory_cost=PASSLIB_DEFAULT_MEMORY_KIB,
            argon2__time_cost=3,
            argon2__parallelism=4,
        )
        return expensive.hash("correct-horse-battery-staple")

    def test_a_legacy_hash_still_verifies(self, legacy_hash):
        assert verify_password("correct-horse-battery-staple", legacy_hash) is True

    def test_verifying_a_legacy_hash_returns_a_replacement(self, legacy_hash):
        ok, upgraded = verify_and_upgrade("correct-horse-battery-staple", legacy_hash)

        assert ok is True
        assert upgraded is not None
        assert hash_parameters(upgraded)["m"] == get_settings().argon2_memory_kib

    def test_a_current_hash_needs_no_replacement(self):
        """None, not a fresh hash: the caller writes to the users table only
        when this is non-None, so returning one every time would turn every
        login into a write."""
        ok, upgraded = verify_and_upgrade(
            "correct-horse-battery-staple", hash_password("correct-horse-battery-staple")
        )

        assert (ok, upgraded) == (True, None)

    def test_a_wrong_password_is_never_upgraded(self, legacy_hash):
        ok, upgraded = verify_and_upgrade("not-the-password", legacy_hash)

        assert (ok, upgraded) == (False, None)

    def test_login_rewrites_a_legacy_hash(self, client, db, legacy_hash):
        """End to end: the only thing that actually retires the old parameters
        is a user logging in."""
        from warroom.models import User

        user = User(email="legacy@example.com", password_hash=legacy_hash)
        db.add(user)
        db.commit()

        r = client.post(
            "/auth/login",
            json={"email": "legacy@example.com", "password": "correct-horse-battery-staple"},
        )

        assert r.status_code == 200
        db.refresh(user)
        assert hash_parameters(user.password_hash)["m"] == get_settings().argon2_memory_kib


class TestHashingConcurrency:
    def test_the_semaphore_matches_the_setting(self):
        assert security._hash_slots()._initial_value == get_settings().password_hash_concurrency

    def test_hashing_is_refused_when_every_slot_is_taken(self, monkeypatch):
        """The guarantee the whole memory budget rests on: when the slots are
        gone, the request is refused rather than allocating anyway."""
        monkeypatch.setattr(security, "HASH_QUEUE_TIMEOUT", 0.05)
        slots = security._hash_slots()
        held = [slots.acquire() for _ in range(get_settings().password_hash_concurrency)]

        try:
            with pytest.raises(PasswordHashingBusy):
                hash_password("a-sufficiently-long-password")
        finally:
            for _ in held:
                slots.release()

    def test_the_unknown_email_path_is_capped_too(self, monkeypatch):
        """dummy_verify runs for an email that does not exist — the one hashing
        path an attacker reaches without an account. Uncapped it would be the
        hole the semaphore exists to close, left open on the only door that
        needs no key."""
        monkeypatch.setattr(security, "HASH_QUEUE_TIMEOUT", 0.05)
        slots = security._hash_slots()
        held = [slots.acquire() for _ in range(get_settings().password_hash_concurrency)]

        try:
            with pytest.raises(PasswordHashingBusy):
                dummy_verify()
        finally:
            for _ in held:
                slots.release()

    def test_a_failed_hash_gives_its_slot_back(self, monkeypatch):
        """A leaked slot shrinks the pool permanently, so the endpoint would
        deadlock at a concurrency nobody configured — after enough errors to
        make the cause untraceable."""

        def explode(*args, **kwargs):
            raise ValueError("boom")

        monkeypatch.setattr(security._context(), "hash", explode)
        before = security._hash_slots()._value

        with pytest.raises(ValueError):
            hash_password("a-sufficiently-long-password")

        assert security._hash_slots()._value == before

    def test_concurrent_hashing_never_exceeds_the_cap(self):
        """The property stated directly: however many threads ask at once, the
        number of argon2 arenas alive together stays at or under the cap."""
        cap = get_settings().password_hash_concurrency
        lock = threading.Lock()
        live = 0
        peak = 0
        real_hash = security._context().hash

        def watched(*args, **kwargs):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            try:
                return real_hash(*args, **kwargs)
            finally:
                with lock:
                    live -= 1

        original = security._context().hash
        security._context().hash = watched
        try:
            threads = [
                threading.Thread(target=lambda: hash_password("a-long-enough-password"))
                for _ in range(cap * 4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            security._context().hash = original

        assert peak <= cap


class TestProbes:
    def test_health_needs_no_database(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_ready_reports_the_database(self, client):
        r = client.get("/ready")

        assert r.status_code == 200
        assert r.json() == {"status": "ok", "database": "ok"}

    def test_ready_is_503_when_the_database_is_gone(self, client, db, monkeypatch):
        """503, not an exception. A readiness probe that 500s cannot be told
        apart from one that crashed, and the platform treats them differently."""
        from sqlalchemy.exc import OperationalError

        def dead(*args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

        monkeypatch.setattr(db, "execute", dead)

        r = client.get("/ready")

        assert r.status_code == 503
        assert r.json()["database"] == "unreachable"

    def test_ready_never_leaks_the_connection_string(self, client, db, monkeypatch):
        """The exception text carries the DSN, password included, and this
        endpoint is unauthenticated."""
        from sqlalchemy.exc import OperationalError

        def dead(*args, **kwargs):
            raise OperationalError(
                "SELECT 1", {}, Exception("postgresql://user:hunter2@db.internal/warroom")
            )

        monkeypatch.setattr(db, "execute", dead)

        assert "hunter2" not in client.get("/ready").text


class TestDocsGating:
    def test_docs_are_served_when_enabled(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_docs_can_be_turned_off(self, monkeypatch):
        """/docs and /openapi.json publish every route, parameter and schema.
        Turning them off has to remove the ROUTES, not just hide the link."""
        monkeypatch.setattr(get_settings(), "docs_enabled", False)
        from fastapi.testclient import TestClient

        with TestClient(create_app()) as bare:
            assert bare.get("/openapi.json").status_code == 404
            assert bare.get("/docs").status_code == 404
            assert bare.get("/health").status_code == 200
