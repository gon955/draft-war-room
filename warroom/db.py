"""SQLAlchemy engine, session factory and declarative Base (SPEC 1).

TODO (Phase 1):
  * `Base` — the DeclarativeBase every model in models.py inherits.
  * `engine` / `SessionLocal` built from Settings.database_url over psycopg.
  * `get_db()` — a request-scoped session, wired into routes with Depends.

Postgres only, not merely preferred: the schema stores roster slots, point
weights, positions and projections as JSONB, which SQLite cannot match. That is
why CI runs a Postgres service container rather than a throwaway file DB.
"""

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from warroom.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        datetime: DateTime(timezone=True),
    }


settings = get_settings()

engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(expire_on_commit=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
