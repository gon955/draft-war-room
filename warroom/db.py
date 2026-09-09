"""SQLAlchemy engine, session factory and declarative Base (SPEC 1).

TODO (Phase 1):
  * `Base` — the DeclarativeBase every model in models.py inherits.
  * `engine` / `SessionLocal` built from Settings.database_url over psycopg.
  * `get_db()` — a request-scoped session, wired into routes with Depends.

Postgres only, not merely preferred: the schema stores roster slots, point
weights, positions and projections as JSONB, which SQLite cannot match. That is
why CI runs a Postgres service container rather than a throwaway file DB.
"""
