"""SQLAlchemy engine, session factory and declarative Base (SPEC 1).

`Base` is the DeclarativeBase every model in models.py inherits; `engine` and
`SessionLocal` are built from Settings.database_url over psycopg; `get_db()`
yields a request-scoped session and is what routes take via Depends.

The engine is constructed at import time, so importing this module needs
DATABASE_URL set — but create_engine is lazy and connects to nothing until a
session is used. That is what lets the test suite override get_db with its own
engine and leave this one idle.

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

# Pool size is configured, not defaulted, and the reason is a mismatch that only
# shows up under load. SQLAlchemy's defaults are pool_size=5 + max_overflow=10,
# so 15 connections — against an AnyIO threadpool of 40. Every route in this app
# is a sync `def`, so 40 of them can be in flight at once, and requests 16..40
# each block for pool_timeout (30s by default) before failing with a pool
# timeout that reads like a database outage. The invariant to preserve:
#
#     db_pool_size + db_max_overflow  >=  request_thread_limit
#
# main.py holds the threadpool to request_thread_limit, which is what makes
# that hold. Raising either without the other re-opens the gap.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    # Managed Postgres and the proxies in front of it drop idle connections
    # after a few minutes; pool_pre_ping catches that but pays a round trip to
    # find out. Recycling below any sane idle timeout means it rarely has to.
    pool_recycle=300,
)

SessionLocal = sessionmaker(expire_on_commit=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
