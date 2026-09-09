"""Fixtures for the integration suite (SPEC 6).

TODO (Phase 1):
  * `db` — a transactional session rolled back after every test, so tests share
    one Postgres schema without sharing state.
  * `client` — TestClient over create_app() with get_db and get_data_source
    overridden. ESPN is ALWAYS faked; no test touches the network.
  * `user_a`, `user_b` and their tokens — the two identities the SPEC 6
    authorization matrix is written against.
  * `league` owned by A over a small fixed player pool, `board` owned by A, and
    a read share and an edit share to B.

Postgres, not SQLite: the schema is JSONB-heavy and a passing SQLite suite would
prove nothing about what CI and production actually run.
"""
