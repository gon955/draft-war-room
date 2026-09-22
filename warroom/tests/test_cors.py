"""Cross-origin access for the browser frontend (SPEC 1).

The frontend is served from another origin entirely — Cloudflare Pages in
production, :3000 in development — so without this middleware a browser blocks
every call and the preflight is a bare 405. curl and the test client never see
that, which is why these assertions exist: nothing else in 565 tests would
notice CORS breaking.

Two failures this file is written against, both of which shipped once:

  * the setting was declared `cors_origns` and read as `cors_origins`, so
    create_app() raised AttributeError and the app did not build at all;
  * `allow_headers` listed "Contnet-Type". Harmless as it turns out — Starlette
    merges the CORS-safelisted request headers in regardless — but a header
    name nobody can spell is worth a test that names the one that matters.
"""

import pytest
from fastapi.testclient import TestClient

from warroom.config import get_settings
from warroom.main import create_app

ALLOWED = "http://localhost:3000"
ALLOWED_ALT = "http://127.0.0.1:3000"
DENIED = "https://evil.example.com"


def preflight(client, origin, method="POST", headers="authorization,content-type"):
    return client.options(
        "/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


@pytest.fixture
def cors_client():
    """A client over a fresh app, so it picks up whatever settings say now."""
    with TestClient(create_app()) as client:
        yield client


class TestPreflight:
    def test_an_allowed_origin_gets_a_200(self, cors_client):
        assert preflight(cors_client, ALLOWED).status_code == 200

    def test_the_preflight_echoes_the_origin(self, cors_client):
        r = preflight(cors_client, ALLOWED)

        assert r.headers["access-control-allow-origin"] == ALLOWED

    def test_authorization_is_allowed(self, cors_client):
        """The header the whole API depends on, and the only one here that is
        NOT CORS-safelisted — omit it and every authenticated call dies."""
        allowed = preflight(cors_client, ALLOWED).headers["access-control-allow-headers"]

        assert "authorization" in allowed.lower()

    def test_content_type_is_allowed(self, cors_client):
        allowed = preflight(cors_client, ALLOWED).headers["access-control-allow-headers"]

        assert "content-type" in allowed.lower()

    @pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
    def test_every_verb_the_api_uses_is_allowed(self, cors_client, method):
        allowed = preflight(cors_client, ALLOWED, method=method).headers[
            "access-control-allow-methods"
        ]

        assert method in allowed

    def test_both_localhost_spellings_are_allowed(self, cors_client):
        """A browser treats these as different origins. Allowing one and
        browsing the other fails without saying which."""
        assert preflight(cors_client, ALLOWED_ALT).headers["access-control-allow-origin"] == (
            ALLOWED_ALT
        )

    def test_credentials_are_not_advertised(self, cors_client):
        """Auth is a bearer token, not a cookie. allow_credentials=True with a
        wildcard origin is also rejected outright by browsers, so this stays
        False and the header stays absent."""
        assert "access-control-allow-credentials" not in preflight(cors_client, ALLOWED).headers


class TestDeniedOrigins:
    def test_an_unknown_origin_gets_no_allow_origin_header(self, cors_client):
        """The assertion that proves the allowlist is a list rather than
        decoration. The response still arrives — it is the BROWSER that refuses
        it, on the strength of this missing header."""
        r = cors_client.get("/health", headers={"Origin": DENIED})

        assert "access-control-allow-origin" not in r.headers

    def test_a_denied_preflight_is_not_a_200(self, cors_client):
        assert preflight(cors_client, DENIED).status_code != 200


class TestNoOrigin:
    def test_same_origin_requests_are_untouched(self, cors_client):
        """curl, the dev console and the container health check send no Origin
        at all; CORS must be invisible to them."""
        r = cors_client.get("/health")

        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestOriginRegex:
    """Cloudflare Pages gives every preview deploy its own hostname, which an
    exact allowlist cannot match."""

    @pytest.fixture
    def preview_client(self, monkeypatch):
        monkeypatch.setattr(
            get_settings(), "cors_origin_regex", r"https://[a-z0-9-]+\.warroom\.pages\.dev"
        )
        with TestClient(create_app()) as client:
            yield client

    def test_a_preview_host_is_allowed(self, preview_client):
        origin = "https://abc123.warroom.pages.dev"

        assert preflight(preview_client, origin).headers["access-control-allow-origin"] == origin

    def test_a_lookalike_host_is_not(self, preview_client):
        """evil-warroom.pages.dev must not match warroom.pages.dev."""
        r = preflight(preview_client, "https://evil-warroom.pages.dev")

        assert r.headers.get("access-control-allow-origin") != "https://evil-warroom.pages.dev"
