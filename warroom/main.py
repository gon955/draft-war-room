"""FastAPI application entry point (SPEC 1).

Run locally with:  uvicorn warroom.main:app --reload

Routers are registered here as each one is built; the includes stay commented
until the router behind them exists, so the app never advertises an endpoint it
cannot serve.
"""
from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the application. A factory so tests can construct an isolated app."""
    app = FastAPI(title="Draft War Room", version="0.1.0")

    # TODO: uncomment each as the router lands (SPEC 4).
    # from warroom.api.routes import (
    #     auth, boards, leagues, mocks, players, rankings, shares, tiers,
    # )
    # for module in (auth, leagues, players, boards, rankings, tiers, mocks, shares):
    #     app.include_router(module.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """Liveness probe — the one endpoint that never needs auth or a DB."""
        return {"status": "ok"}

    return app


app = create_app()
