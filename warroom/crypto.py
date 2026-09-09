"""Encryption at rest for the ESPN private-league cookie (SPEC 2.3).

ESPN_S2 is a live session credential, not a column: it is handled like a secret
or not stored at all. This wraps Fernet with the app key from FERNET_KEY so
leagues.espn_s2_encrypted never holds plaintext.

TODO (Phase 3): `encrypt(value) -> str` / `decrypt(token) -> str`, and a hard
failure when FERNET_KEY is unset rather than a silent plaintext fallback.
"""
