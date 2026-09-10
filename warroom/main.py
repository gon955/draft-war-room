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

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the application. A factory so tests can construct an isolated app."""
    app = FastAPI(title="Draft War Room", version="0.1.0")

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

    return app


app = create_app()
