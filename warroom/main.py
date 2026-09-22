"""FastAPI application entry point (SPEC 1).

Run locally with:  uvicorn warroom.main:app --reload

Every router is registered below. Most are still empty APIRouters carrying only
their docstring, so including them adds no routes and advertises nothing the app
cannot serve — each one starts serving the moment its handlers land, with no
edit needed here.

Note the two ways to get an app. `create_app()` builds a fresh, isolated one and
is what the test suite uses, so its dependency_overrides touch only that
instance. The module-level `app` below exists for `uvicorn warroom.main:app`.
Overriding a dependency on the wrong one of those is silent: the request runs
against the un-overridden app and nothing complains.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from warroom.config import get_settings

# A hand-testing console, not the SPEC 1 frontend. Next.js still owns that.
# One static file, no build step, served from the API's OWN origin — so it needs
# no entry in the CORS allowlist below and keeps working whatever that is set to.
DEV_CONSOLE = Path(__file__).resolve().parent.parent / "frontend" / "dev.html"


def create_app() -> FastAPI:
    """Build the application. A factory so tests can construct an isolated app."""
    app = FastAPI(title="Draft War Room", version="0.1.0")

    # The browser blocks every cross-origin call without this: the frontend is
    # served from another origin entirely (Pages in production, :3000 locally),
    # and a preflight with no handler is a 405.
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_origin_regex=settings.cors_origin_regex,
        # False because auth is a bearer token, not a cookie. It also has to
        # stay False if allow_origins ever becomes ["*"]: the CORS spec forbids
        # that pair and browsers reject the response outright.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        # Authorization is the one that matters. Starlette merges in the
        # CORS-safelisted request headers (Accept, Accept-Language,
        # Content-Language, Content-Type) on its own, so those need not be
        # listed — but Authorization is not safelisted and must be.
        allow_headers=["Authorization"],
    )

    from warroom.api.routes import (
        auth,
        boards,
        leagues,
        mocks,
        players,
        rankings,
        shares,
        tiers,
    )

    for module in (auth, leagues, players, boards, rankings, tiers, mocks, shares):
        app.include_router(module.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """Liveness probe — the one endpoint that never needs auth or a DB."""
        return {"status": "ok"}

    if DEV_CONSOLE.exists():
        # Guarded by existence rather than a flag: the file is not deployed, so
        # the route simply is not there in production. include_in_schema=False
        # keeps a dev page out of the OpenAPI document.
        @app.get("/dev", include_in_schema=False)
        def dev_console() -> FileResponse:
            return FileResponse(DEV_CONSOLE)

    return app


app = create_app()
