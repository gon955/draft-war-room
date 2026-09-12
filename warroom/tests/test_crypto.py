"""Encryption at rest for the ESPN cookie (SPEC 2.3).

Worth its own file because the ways this module breaks are all quiet ones. A
plaintext fallback still round-trips, so every caller keeps working while the
cookie sits readable in the database. A stale cached Fernet still decrypts
everything written in the same process, so a rotated key looks applied right up
until the next restart. Neither is visible anywhere else in the suite.

No database and no client: crypto.py is pure crypto, so nothing here needs
Postgres or a single conftest fixture.

The key is generated in-process rather than read from the environment. That
keeps the suite deterministic and self-contained — CI needs no FERNET_KEY in its
env block — and it turns the unset-key case into an ordinary fixture instead of
a special environment.
"""

import pytest
from cryptography.fernet import Fernet

from warroom import crypto
from warroom.config import get_settings
from warroom.crypto import InvalidToken, MissingFernetKey, decrypt, encrypt

# Shaped like the real credential: ESPN_S2 is a long URL-safe cookie string.
ESPN_S2 = "AEBxK%2FvN0pQ3rLm8" + "Xy7" * 40


@pytest.fixture(autouse=True)
def clear_fernet_cache():
    """Every test starts and ends with no cached Fernet.

    _fernet() is lru_cached. Without the teardown half, the first test to build
    a Fernet hands it to every test that follows, and a key set later is
    silently ignored — including by the rest of the suite.
    """
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture
def fernet_key(monkeypatch) -> str:
    """Point the module at a freshly generated key for the duration of one test.

    Patches the cached Settings instance in place rather than the environment.
    Settings also reads .env (config.py), so setting or deleting an env var is
    not enough to decide what the app actually sees; get_settings is lru_cached,
    so the instance patched here is the one _fernet() will read. monkeypatch
    restores the attribute afterwards.
    """
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(get_settings(), "fernet_key", key)
    return key


class TestRoundTrip:
    def test_decrypt_undoes_encrypt(self, fernet_key):
        assert decrypt(encrypt(ESPN_S2)) == ESPN_S2

    def test_the_token_does_not_contain_the_plaintext(self, fernet_key):
        """The one assertion a silent plaintext fallback cannot pass."""
        token = encrypt(ESPN_S2)

        assert token != ESPN_S2
        assert ESPN_S2 not in token

    def test_encrypting_twice_gives_different_tokens(self, fernet_key):
        """Fernet mixes in an IV and a timestamp, so equal cookies are not equal
        ciphertexts — two leagues sharing one cookie must not be recognisable as
        two identical rows."""
        first, second = encrypt(ESPN_S2), encrypt(ESPN_S2)

        assert first != second
        assert decrypt(first) == decrypt(second) == ESPN_S2

    def test_both_directions_deal_in_str(self, fernet_key):
        """espn_s2_encrypted is a str column; Fernet's bytes stay inside the
        module."""
        token = encrypt(ESPN_S2)

        assert isinstance(token, str)
        assert isinstance(decrypt(token), str)


class TestMissingKey:
    """SPEC 2.3: no key means no storage — never plaintext storage."""

    @pytest.mark.parametrize("unset", [None, ""], ids=["none", "empty-string"])
    def test_encrypt_refuses(self, monkeypatch, unset):
        """Empty string as well as None. .env.example ships `FERNET_KEY=`, so an
        unconfigured key arrives as "" more often than as None, and a check that
        only catches None lets the more common mistake through."""
        monkeypatch.setattr(get_settings(), "fernet_key", unset)

        with pytest.raises(MissingFernetKey):
            encrypt(ESPN_S2)

    def test_decrypt_refuses(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "fernet_key", None)

        with pytest.raises(MissingFernetKey):
            decrypt("any-token-at-all")

    def test_the_error_leaks_neither_the_secret_nor_the_key(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "fernet_key", None)

        with pytest.raises(MissingFernetKey) as excinfo:
            encrypt(ESPN_S2)

        assert ESPN_S2 not in str(excinfo.value)

    def test_a_malformed_key_is_not_reported_as_a_missing_one(self, monkeypatch):
        """Present-but-unusable is a different operator mistake from absent, so
        it stays a different exception: Fernet's own ValueError. This fails the
        day MissingFernetKey is made to subclass ValueError and the two
        collapse into one."""
        monkeypatch.setattr(get_settings(), "fernet_key", "not-a-real-fernet-key")

        with pytest.raises(ValueError) as excinfo:
            encrypt(ESPN_S2)

        assert not isinstance(excinfo.value, MissingFernetKey)


class TestTamperingAndRotation:
    def test_a_tampered_token_raises(self, fernet_key):
        """The HMAC is the last thing in the token, so editing the tail is the
        cheapest way to prove it is checked at all."""
        token = encrypt(ESPN_S2)
        tampered = token[:-4] + ("BBBB" if token.endswith("AAAA") else "AAAA")

        with pytest.raises(InvalidToken):
            decrypt(tampered)

    def test_garbage_raises_rather_than_returning_garbage(self, fernet_key):
        with pytest.raises(InvalidToken):
            decrypt("clearly-not-a-token")

    def test_a_rotated_key_cannot_read_the_old_rows(self, monkeypatch, fernet_key):
        """The realistic InvalidToken: the row is intact, the key moved on.
        services.sync has to turn this one into "reconnect your league"."""
        token = encrypt(ESPN_S2)

        # Both caches, in this order: a new key with the old Fernet still cached
        # would keep decrypting happily and prove nothing.
        crypto._fernet.cache_clear()
        monkeypatch.setattr(get_settings(), "fernet_key", Fernet.generate_key().decode())

        with pytest.raises(InvalidToken):
            decrypt(token)


def test_invalid_token_is_importable_from_this_module():
    """Callers catch crypto.InvalidToken, not cryptography's, so crypto.py stays
    the only module that names the library."""
    from cryptography.fernet import InvalidToken as UpstreamInvalidToken

    assert InvalidToken is UpstreamInvalidToken
    assert "InvalidToken" in crypto.__all__
