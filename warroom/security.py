"""
Keep this free of FastAPI and of the DB — it is pure crypto so it can be tested
without either. The request-time plumbing lives in deps.py.

Two things here are operational rather than cryptographic, and both exist
because the defaults are wrong for a single small container:

  * argon2id COST is read from Settings instead of taken from passlib.
    passlib's defaults are m=65536 KiB (64 MiB), t=3, p=4. Measured, 20
    concurrent logins against that peaked at 899 MiB RSS — an OOM kill on the
    512 MB machine fly.toml asks for, reachable by anyone with curl and no
    account. The defaults in config.py are OWASP's m=19456, t=2, p=1.

  * CONCURRENCY is capped by a semaphore. Cost alone does not bound memory:
    argon2 allocates its whole arena for the duration of every hash, so peak
    memory is concurrency x memory_cost and the only term the server controls
    at request time is the first one. The cap is what makes the peak
    predictable; config.auth_max_attempts in front of it is what keeps most
    callers from reaching it at all.

Old hashes keep verifying whatever the settings say — argon2 encodes its
parameters in the hash string — so lowering the cost is backward compatible.
verify_and_update below is what actually migrates a legacy 64 MiB hash to the
current cost, on the next successful login by that user.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from passlib.context import CryptContext

from warroom.config import get_settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

# Seconds a request will wait for a hashing slot before giving up. Long enough
# to ride out a burst at the configured concurrency (a hash is ~40 ms), short
# enough that a real overload answers instead of holding the connection.
HASH_QUEUE_TIMEOUT = 5.0


class PasswordHashingBusy(RuntimeError):
    """Every hashing slot was taken for HASH_QUEUE_TIMEOUT seconds.

    Deliberately not a generic 500: the server is healthy and the request was
    fine, there was simply no memory budget for it right now. auth.py turns
    this into a 503 with Retry-After, which is the honest answer and the one a
    client can act on.
    """


@lru_cache
def _context() -> CryptContext:
    """The process-wide CryptContext, built on first use.

    Lazy for the same reason crypto._fernet is: this module is imported by
    deps.py and therefore by everything, and reading Settings at import time
    would make an unconfigured environment fail at import rather than at the
    first request that actually needs a password.

    Cached alongside get_settings, so anything changing the cost mid-process
    must clear BOTH — clearing only get_settings leaves this context in place
    and the old cost keeps being applied.
    """
    settings = get_settings()
    return CryptContext(
        schemes=["argon2"],
        deprecated="auto",
        argon2__memory_cost=settings.argon2_memory_kib,
        argon2__time_cost=settings.argon2_time_cost,
        argon2__parallelism=settings.argon2_parallelism,
    )


@lru_cache
def _hash_slots() -> threading.BoundedSemaphore:
    """Bounds how many argon2 arenas can exist at once.

    threading, not asyncio: every route in this app is a sync `def`, so FastAPI
    runs it in the AnyIO worker threadpool and the contention here is between
    OS threads.

    Bounded rather than plain, so releasing a slot that was never acquired
    raises instead of quietly inflating the limit — which would give back
    exactly the unbounded memory this exists to prevent.
    """
    return threading.BoundedSemaphore(get_settings().password_hash_concurrency)


class _slot:
    """Hold one hashing slot, or raise PasswordHashingBusy.

    A context manager rather than a decorator because the release has to happen
    on the exception path too: an argon2 failure that leaked its slot would
    shrink the pool by one every time, and the endpoint would deadlock at a
    concurrency the operator never configured.
    """

    def __enter__(self) -> None:
        if not _hash_slots().acquire(timeout=HASH_QUEUE_TIMEOUT):
            raise PasswordHashingBusy(
                "No password-hashing capacity free. This is a deliberate memory "
                "limit, not a failure — retry in a moment."
            )

    def __exit__(self, *exc: object) -> None:
        _hash_slots().release()


def hash_password(password: str) -> str:
    with _slot():
        return _context().hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    with _slot():
        return _context().verify(plain, hashed)


def verify_and_upgrade(plain: str, hashed: str) -> tuple[bool, str | None]:
    """Verify, and return a re-hash when the stored one is below current cost.

    (ok, new_hash). new_hash is None when nothing needs rewriting, which is the
    common case — so the caller writes to the users table only on the login
    that actually migrates an account off the old parameters.

    One slot covers both the verify and the re-hash. Taking a second one for
    the re-hash would let concurrency reach 2x the configured cap at exactly
    the moment a deployment is migrating every user at once.
    """
    with _slot():
        return _context().verify_and_update(plain, hashed)


def dummy_verify() -> None:
    """Burn a verification against a throwaway hash, for an unknown email.

    Exported so auth.py does not reach into passlib itself: the login handler
    needs "spend the same time as a real verify" and nothing else, and routing
    it through here keeps the choice of hashing library inside this module.

    Takes a slot, exactly like verify_password, and both halves of that
    sentence matter. Uncapped it would be an unbounded-memory path reachable by
    POSTing addresses that do not exist — the precise hole the semaphore
    exists to close, left open on the one endpoint an attacker can reach
    without an account. Capped, it also stays timing-symmetric with the real
    verify under load: if only one of the two queued, the wait itself would
    answer "does this address have an account?".
    """
    with _slot():
        _context().dummy_verify()


def create_access_token(subject: uuid.UUID | str, expires_delta: timedelta | None = None) -> str:
    settings = get_settings()

    now = datetime.now(UTC)

    expire = now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))

    payload = {"sub": str(subject), "exp": expire, "iat": now}

    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
