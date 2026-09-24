"""Application settings, read from the environment (SPEC 1).

A pydantic-settings `Settings` reading DATABASE_URL, JWT_SECRET, FERNET_KEY and
the optional ESPN_S2 / ESPN_SWID cookies, behind a cached accessor so the app
builds one instance. See .env.example for the full list.

get_settings is lru_cached, so anything wanting different settings mid-process
(a test pointing at another database) must call get_settings.cache_clear().

The ESPN values are live session credentials: they may be read here, but never
logged and never returned by an endpoint (SPEC 2.3).
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env")
    database_url: str
    # Declared even though only the test suite reads it. pydantic-settings
    # defaults to extra="forbid" and applies that to keys in the dotenv file, so
    # an undeclared TEST_DATABASE_URL= line in .env makes every Settings()
    # raise — which takes the app, alembic and the whole suite down with it.
    # .env.example invites exactly that line, so the field has to exist.
    test_database_url: str | None = None
    fernet_key: str | None = None
    espn_s2: str | None = None
    espn_swid: str | None = None
    jwt_secret: str
    # Comma-separated. Both spellings of localhost on purpose: a browser treats
    # http://localhost:3000 and http://127.0.0.1:3000 as DIFFERENT origins, and
    # allowing one while browsing the other fails with a CORS error that never
    # says which.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # Cloudflare Pages gives every branch and commit its own preview host
    # (<hash>.<project>.pages.dev), which an exact allowlist can never match.
    # Leave unset to allow production only.
    cors_origin_regex: str | None = None

    # ----------------------------------------------------------------- #
    # Operational limits.
    #
    # Every one of these exists because a default somewhere else is wrong for
    # a single small container, and each was measured rather than guessed —
    # see README "Operational limits" for the numbers.
    # ----------------------------------------------------------------- #

    # Seconds before an ESPN call is abandoned. espn-api issues bare
    # requests.get() with NO timeout, so without this a connection ESPN never
    # closes holds a worker thread until the process dies. Routes run in the
    # threadpool that also serves /health, so enough stuck syncs take the
    # health check down with them and the platform restarts the machine.
    espn_timeout: float = 15.0

    # SQLAlchemy defaults to pool_size=5, max_overflow=10 — 15 connections,
    # against a threadpool of 40. The 16th concurrent request waits
    # pool_timeout seconds and then 500s. Kept at least as large as
    # request_thread_limit below so that cannot happen.
    db_pool_size: int = 10
    db_max_overflow: int = 30
    # Seconds a request waits for a connection before failing. 30 (the
    # default) means a user stares at a spinner for half a minute before
    # being told no; 10 fails while they are still expecting an answer.
    db_pool_timeout: float = 10.0

    # AnyIO's default threadpool is 40 threads. Every route in this app is a
    # sync `def`, so that is the real concurrency limit of the process — and
    # it must stay under db_pool_size + db_max_overflow.
    request_thread_limit: int = 32

    # How many password hashes may run at once. argon2id allocates
    # argon2_memory_kib for the whole of each one, so this is the single
    # number standing between a burst of logins and an OOM kill:
    # peak ≈ password_hash_concurrency × argon2_memory_kib.
    #
    # 4, not more, because a hash made under the OLD 64 MiB cost still costs
    # 64 MiB to VERIFY — argon2 reads the parameters back out of the stored
    # hash. Until every account has logged in once and been re-hashed, the
    # real peak is 4 × 64 MiB ≈ 256 MiB, which a 512 MB machine survives and
    # 8 × 64 MiB would not.
    password_hash_concurrency: int = 4

    # argon2id cost. OWASP's second recommended profile (m=19456 KiB, t=2,
    # p=1). passlib's own defaults are m=65536 (64 MiB), t=3, p=4: 20
    # concurrent logins measured at 899 MiB RSS, which a 512 MB machine does
    # not survive. Raise memory here only with password_hash_concurrency and
    # the machine size in the other hand.
    argon2_memory_kib: int = 19456
    argon2_time_cost: int = 2
    argon2_parallelism: int = 1

    # Auth attempts allowed per client address per window. Registration and
    # login are the only unauthenticated endpoints and each costs a full
    # argon2 hash, so they are the cheapest way to burn the CPU of a machine
    # this size. Set auth_max_attempts to 0 to disable the limiter.
    auth_max_attempts: int = 20
    auth_window_seconds: float = 60.0

    # How much the survival model trusts ESPN's board over our own when
    # predicting what the ROOM will take. 1.0 is full market, which is the
    # honest default: our ranking is the one thing we know the other managers
    # are NOT using. 0.0 restores the pre-market behaviour. Between the two is
    # for a room that drafts half off ESPN and half off reputation.
    #
    # It never touches what a player is worth to YOU — only the estimate of
    # whether they will still be there next turn.
    draft_market_weight: float = 1.0

    # /docs, /redoc and /openapi.json. On by default so local development and
    # the CI type-generation step keep working; turn OFF in production unless
    # the API surface is meant to be public.
    docs_enabled: bool = True


@lru_cache
def get_settings():
    return Settings()
