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

import logging

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


class TestCorsSurvivesAServerError:
    """A 500 must reach the browser as a 500, not as a CORS failure.

    This is the bug that cost real debugging time: Postgres was unreachable,
    POST /auth/login raised, and Starlette's ServerErrorMiddleware answered 500
    from OUTSIDE the CORS middleware — so the response had no
    Access-Control-Allow-Origin, the browser rejected it before any JavaScript
    saw the status, and the frontend reported "Cannot reach the API ... is this
    origin in CORS_ORIGINS?". The API was running and CORS was configured
    correctly. Neither fact was visible from the symptom.
    """

    @pytest.fixture
    def exploding_client(self) -> TestClient:
        app = create_app()

        @app.get("/boom", include_in_schema=False)
        def boom() -> None:
            raise RuntimeError("something went wrong deep in a route")

        # raise_server_exceptions=False so the TestClient behaves like a real
        # server: return the 500 rather than re-raising into the test.
        return TestClient(app, raise_server_exceptions=False)

    def test_a_500_still_carries_the_cors_header(self, exploding_client):
        r = exploding_client.get("/boom", headers={"Origin": ALLOWED})

        assert r.status_code == 500
        assert r.headers["access-control-allow-origin"] == ALLOWED

    def test_the_body_is_json_the_frontend_can_read(self, exploding_client):
        """api.ts reads `detail` off every error. A text/plain body makes a
        genuine server error render as an unparseable response."""
        r = exploding_client.get("/boom", headers={"Origin": ALLOWED})

        assert r.json() == {"detail": "Internal server error"}

    def test_the_exception_text_is_not_sent_to_the_browser(self, exploding_client):
        """Exception messages here routinely carry the connection string,
        password included, and this response is unauthenticated."""
        r = exploding_client.get("/boom", headers={"Origin": ALLOWED})

        assert "something went wrong deep in a route" not in r.text

    def test_the_traceback_still_reaches_the_logs(self, exploding_client, caplog):
        """Catching the exception takes it out of ServerErrorMiddleware's
        hands. A readable browser error bought with a lost server traceback
        would be a bad trade."""
        with caplog.at_level(logging.ERROR, logger="warroom"):
            exploding_client.get("/boom", headers={"Origin": ALLOWED})

        assert any(
            rec.exc_info and "something went wrong deep in a route" in str(rec.exc_info[1])
            for rec in caplog.records
        )

    def test_a_disallowed_origin_gets_no_header_even_on_a_500(self, exploding_client):
        """The fix must not turn the error path into a way round the allowlist."""
        r = exploding_client.get("/boom", headers={"Origin": DENIED})

        assert r.status_code == 500
        assert "access-control-allow-origin" not in r.headers

    def test_ordinary_responses_are_unaffected(self, client):
        assert client.get("/health", headers={"Origin": ALLOWED}).status_code == 200
