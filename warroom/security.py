"""Password hashing and JWT issue/verify (SPEC 1, 4).

TODO (Phase 2):
  * `hash_password` / `verify_password` over passlib with Argon2.
  * `create_access_token` / `decode_access_token` over PyJWT, signed with
    Settings.jwt_secret.

Keep this free of FastAPI and of the DB — it is pure crypto so it can be tested
without either. The request-time plumbing lives in deps.py.
"""
