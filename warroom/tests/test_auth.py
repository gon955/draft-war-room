"""Registration, login and token handling (SPEC 6).

  register -> login -> GET /auth/me returns that user
  a protected route without a token -> 401
  registering a duplicate email -> 409
  emails are stored lowercased, so Alice@x.com and alice@x.com collide

The password itself is what most of this is really about. Two things must hold
and neither is visible from a passing happy path: the hash must never equal the
password, and a wrong email must cost the same as a wrong password. auth.py
calls pwd_context.dummy_verify() for the unknown-email case precisely so the two
take the same time; without it the endpoint answers "does this address have an
account?" to anyone with a stopwatch.
"""

import uuid

import pytest
from sqlalchemy import select

from warroom.models import User
from warroom.tests.conftest import TEST_PASSWORD

NEW_EMAIL = "newcomer@example.com"
NEW_PASSWORD = "a-sufficiently-long-password"


def register(client, email=NEW_EMAIL, password=NEW_PASSWORD):
    return client.post("/auth/register", json={"email": email, "password": password})


def login(client, email=NEW_EMAIL, password=NEW_PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


class TestRegistration:
    def test_register_returns_the_created_user(self, client):
        r = register(client)

        assert r.status_code == 201
        body = r.json()
        assert body["email"] == NEW_EMAIL
        assert uuid.UUID(body["id"])
        assert body["created_at"]

    def test_the_password_never_comes_back(self, client):
        """UserOut has no password field; this fails the day someone adds one."""
        body = register(client).json()

        assert "password" not in body
        assert "password_hash" not in body
        assert NEW_PASSWORD not in str(body)

    def test_the_stored_hash_is_not_the_password(self, client, db):
        register(client)

        stored = db.scalar(select(User.password_hash).where(User.email == NEW_EMAIL))
        assert stored != NEW_PASSWORD
        assert stored.startswith("$argon2")

    def test_a_duplicate_email_is_409(self, client):
        assert register(client).status_code == 201
        assert register(client).status_code == 409

    def test_a_duplicate_is_409_even_in_a_different_case(self, client):
        """users.email is unique on lower(email) (ix_users_email_lower), and
        LowercaseEmail normalises on the way in. Alice@ and alice@ are one
        account, so the second attempt collides rather than creating a twin."""
        assert register(client, email="Alice@example.com").status_code == 201
        assert register(client, email="alice@example.com").status_code == 409

    def test_the_address_is_stored_lowercased(self, client, db):
        register(client, email="MiXeD@Example.COM")

        assert db.scalar(select(User.email).where(User.email == "mixed@example.com"))

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            ({"email": "not-an-email", "password": NEW_PASSWORD}, "malformed address"),
            ({"email": NEW_EMAIL, "password": "short"}, "password under 8 chars"),
            ({"email": NEW_EMAIL}, "no password at all"),
        ],
    )
    def test_bad_input_is_rejected_before_a_user_exists(self, client, db, payload, why):
        assert client.post("/auth/register", json=payload).status_code == 422, why
        assert db.scalar(select(User).where(User.email == NEW_EMAIL)) is None


class TestLogin:
    def test_register_then_login_then_me(self, client):
        """The whole round trip SPEC 6 asks for, in one test."""
        created = register(client).json()

        token = login(client).json()["access_token"]
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert me.status_code == 200
        assert me.json()["id"] == created["id"]
        assert me.json()["email"] == NEW_EMAIL

    def test_the_token_is_a_bearer_token(self, client):
        register(client)

        assert login(client).json()["token_type"] == "bearer"

    def test_login_accepts_a_differently_cased_address(self, client):
        register(client, email="Casey@example.com")

        assert login(client, email="CASEY@EXAMPLE.COM").status_code == 200

    def test_a_wrong_password_is_401(self, client):
        register(client)

        assert login(client, password="wrong-but-long-enough").status_code == 401

    def test_an_unknown_email_is_401(self, client):
        assert login(client, email="nobody@example.com").status_code == 401

    def test_unknown_email_and_wrong_password_are_indistinguishable(self, client):
        """Same status AND same body. A different detail string for the two
        turns the endpoint into an oracle for which addresses have accounts."""
        register(client)

        unknown = login(client, email="nobody@example.com")
        wrong = login(client, password="wrong-but-long-enough")

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json() == wrong.json()


class TestTokens:
    def test_a_protected_route_without_a_token_is_401(self, client):
        assert client.get("/auth/me").status_code == 401

    @pytest.mark.parametrize(
        ("header", "why"),
        [
            ("Bearer not-a-jwt", "unparseable"),
            ("Bearer ", "empty credentials"),
            ("Basic dXNlcjpwYXNz", "wrong scheme"),
        ],
    )
    def test_a_bad_authorization_header_is_401(self, client, header, why):
        r = client.get("/auth/me", headers={"Authorization": header})

        assert r.status_code == 401, why

    def test_a_token_signed_with_another_secret_is_401(self, client, user_a):
        """The signature is the whole defence: a well-formed token whose claims
        name a real user must still be rejected when it was not signed by us."""
        import jwt

        forged = jwt.encode({"sub": str(user_a.id)}, "not-the-app-secret", algorithm="HS256")

        assert (
            client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
        )

    def test_a_valid_token_for_a_deleted_user_is_401(self, client, db, user_a, auth_a):
        """A signature outlives the row it names. deps.get_current_user looks the
        user up for exactly this case."""
        assert client.get("/auth/me", headers=auth_a).status_code == 200

        db.delete(user_a)
        db.commit()

        assert client.get("/auth/me", headers=auth_a).status_code == 401

    def test_the_fixture_password_still_logs_in(self, client, user_a):
        """Guards the conftest optimisation: user_a's hash is computed once at
        import from TEST_PASSWORD. If those drift apart, every auth-dependent
        test starts failing for a reason that has nothing to do with auth."""
        r = client.post("/auth/login", json={"email": user_a.email, "password": TEST_PASSWORD})

        assert r.status_code == 200
