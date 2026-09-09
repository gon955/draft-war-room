"""Application settings, read from the environment (SPEC 1).

TODO (Phase 1): a pydantic-settings `Settings` reading DATABASE_URL, JWT_SECRET,
FERNET_KEY and the optional ESPN_S2 / ESPN_SWID cookies, with a cached accessor
so the app builds one instance. See .env.example for the full list.

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
    fernet_key: str | None = None
    espn_s2: str | None = None
    espn_swid: str | None = None
    jwt_secret: str


@lru_cache
def get_settings():
    return Settings()
