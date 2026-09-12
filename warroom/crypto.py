"""Encryption at rest for the ESPN private-league cookie (SPEC 2.3).

ESPN_S2 is a live session credential, not a column: it is handled like a secret
or not stored at all. This wraps Fernet with the app key from FERNET_KEY so
leagues.espn_s2_encrypted never holds plaintext.

Two rules this module exists to enforce:

  * A missing key is fatal, never a silent plaintext fallback. Writing the
    cookie unencrypted because the key happened to be absent is the one outcome
    worse than refusing to write it at all.
  * Nothing here logs, and no exception raised here carries the plaintext or
    the key in its message.

Like security.py this stays free of FastAPI and of the DB — pure crypto, so it
is testable without a request or a connection. Callers deal in str; Fernet's
bytes live entirely inside this file, and so does the choice of library. Import
InvalidToken from here rather than from cryptography, and this stays the only
module that knows what backs it.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from warroom.config import get_settings

__all__ = ["InvalidToken", "MissingFernetKey", "decrypt", "encrypt"]


class MissingFernetKey(RuntimeError):
    """FERNET_KEY is absent, so nothing can be encrypted or read back.

    A configuration error rather than a runtime one: it cannot be retried or
    recovered from, only fixed. Deliberately distinct from the ValueError Fernet
    itself raises for a key that is present but malformed, so "unset" and "set
    wrong" stay tellable apart — they are different operator mistakes.
    """


@lru_cache
def _fernet() -> Fernet:
    """The process-wide Fernet, built on first use.

    Lazy, not module-level. warroom.crypto is imported transitively by the
    models, so building the Fernet at import time would stop `alembic upgrade
    head` and the entire test suite from running anywhere FERNET_KEY is unset.

    Cached alongside get_settings, which means anything changing the key
    mid-process must clear BOTH caches: clearing only get_settings leaves this
    Fernet in place, and the old key keeps working.
    """
    settings = get_settings()

    # Falsy rather than `is None`: .env.example ships `FERNET_KEY=`, so an
    # unconfigured key reaches here as "" at least as often as as None. An
    # identity check would let the empty string through to Fernet, which reports
    # it as a malformed key — true, but it sends whoever reads the traceback
    # looking for a corrupted value instead of an absent one.
    if not settings.fernet_key:
        raise MissingFernetKey(
            "FERNET_KEY is unset. Generate one with Fernet.generate_key() and "
            "put it in .env — see .env.example."
        )

    return Fernet(settings.fernet_key)


def encrypt(value: str) -> str:
    """Plaintext in, the token stored in leagues.espn_s2_encrypted out.

    Fernet mixes in an IV and a timestamp and appends an HMAC, so the output is
    non-deterministic — encrypting one cookie twice gives two different tokens —
    and tampering is caught at decrypt instead of silently yielding garbage.
    """
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """A stored token in, plaintext out.

    Raises InvalidToken when the row was written under a previous FERNET_KEY or
    has been tampered with; the two are indistinguishable by design. Not caught
    here, because this module has no idea what the right response is and the
    caller does: services.sync should turn it into "reconnect your league",
    never a 500.

    Whoever catches it has to supply the context — which league, key rotation as
    the likely cause. InvalidToken's own message is empty.
    """
    return _fernet().decrypt(token.encode()).decode()
