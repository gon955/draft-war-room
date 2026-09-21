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


@lru_cache
def get_settings():
    return Settings()
