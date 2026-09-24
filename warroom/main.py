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

TWO probes, not one, and the distinction is load-bearing:

  /health  LIVENESS   — is this process alive? Touches nothing else.
  /ready   READINESS  — can it actually serve? Checks the database.

A platform health check pointed at /health keeps a machine in rotation while
every real request 500s on a database it cannot reach; pointed at /ready it
takes a machine out when its database is gone, but also restarts the whole
machine over a blip that has nothing to do with the process. Point the *restart*
check at /health and the *load-balancer* check at /ready — fly.toml does.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import anyio.to_thread
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from warroom.config import get_settings
from warroom.deps import DbSession

logger = logging.getLogger("warroom")

# A hand-testing console, not the SPEC 1 frontend. Next.js still owns that.
# One static file, no build step, served from the API's OWN origin — so it needs
# no entry in the CORS allowlist below and keeps working whatever that is set to.
DEV_CONSOLE = Path(__file__).resolve().parent.parent / "frontend" / "dev.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hold the threadpool to a size the rest of the process can support.

    AnyIO defaults to 40 worker threads. Every route here is a sync `def`, so
    that number IS this process's request concurrency — and it has to stay at or
    under the database pool (db.py), or the surplus requests spend pool_timeout
    waiting for a connection and then fail. It must be set from inside the
    running loop, which is why it lives here and not at import.
    """
    settings = get_settings()
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = settings.request_thread_limit
    yield


async def unhandled_exception_to_500(request: Request, call_next):
    """Turn an unhandled exception into a normal 500 response.

    Starlette already answers 500 for one, from ServerErrorMiddleware — which
    sits OUTSIDE the CORS middleware, so that response carries no
    Access-Control-Allow-Origin. A browser then rejects it before any JavaScript
    can read the status, `fetch` throws a bare TypeError, and the frontend has
    nothing to go on but "the request failed" — which it reports, reasonably and
    completely misleadingly, as the API being unreachable or the origin missing
    from CORS_ORIGINS.

    That is how a database being down presents as a CORS misconfiguration. The
    two have nothing to do with each other and the fix for one is no help
    against the other, so the hours go somewhere useless. Catching the exception
    HERE, inside the CORS middleware, means the 500 goes back through it, keeps
    its headers, and reaches the browser as the server error it is.

    The traceback still has to reach the logs: swallowing it here takes it out
    of ServerErrorMiddleware's hands, so this logs it before answering. A
    readable error in the browser bought by a lost traceback on the server would
    be a bad trade.
    """
    try:
        return await call_next(request)
    except Exception:
        # No detail in the body. The exception text routinely carries the
        # connection string, and this response is going to a browser.
        logger.exception("Unhandled error serving %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def create_app() -> FastAPI:
    """Build the application. A factory so tests can construct an isolated app."""
    settings = get_settings()

    # Off in production unless asked for: /docs and /openapi.json publish every
    # route, parameter and schema in the app. On by default because local
    # development and CI's type-generation step both read /openapi.json, so a
    # default of False would break them in a way that looks like a bug.
    docs = settings.docs_enabled

    app = FastAPI(
        title="Draft War Room",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    # ORDER MATTERS, and it is the reverse of how it reads. add_middleware puts
    # each new layer OUTSIDE the ones already added, so the exception catcher
    # has to be registered FIRST to end up INSIDE the CORS middleware — which is
    # the entire point of it: a 500 raised inside it still passes back out
    # through CORS on the way to the browser. Registered after, it would sit
    # outside and change nothing. There is a test for exactly this.
    app.add_middleware(BaseHTTPMiddleware, dispatch=unhandled_exception_to_500)

    # The browser blocks every cross-origin call without this: the frontend is
    # served from another origin entirely (Pages in production, :3000 locally),
    # and a preflight with no handler is a 405.
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
        # So a browser can read the Retry-After the throttle and the hashing
        # limiter send back. Response headers outside the CORS-safelisted set
        # are invisible to JavaScript unless named here, and a 429 whose
        # Retry-After cannot be read is a 429 the client can only guess at.
        expose_headers=["Retry-After"],
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
        """Liveness — the one endpoint that never needs auth or a DB.

        Answering means the process is up and the threadpool is not exhausted.
        It deliberately says nothing about whether the database is reachable;
        that is /ready's job, and conflating them turns a database blip into a
        machine restart.
        """
        return {"status": "ok"}

    @app.get("/ready", tags=["meta"])
    def ready(response: Response, db: DbSession) -> dict[str, str]:
        """Readiness — can this instance serve a real request?

        SELECT 1 through the same pool every route uses, so this fails for the
        same reasons they would: no database, exhausted pool, dead connection.
        503 rather than an exception, because a readiness probe that 500s is
        indistinguishable from one that crashed.
        """
        try:
            db.execute(text("SELECT 1"))
        except SQLAlchemyError:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            # No exception detail: it carries the DSN, and this endpoint is
            # unauthenticated.
            return {"status": "unavailable", "database": "unreachable"}
        return {"status": "ok", "database": "ok"}

    if DEV_CONSOLE.exists():
        # Guarded by existence rather than a flag: the file is not deployed, so
        # the route simply is not there in production. include_in_schema=False
        # keeps a dev page out of the OpenAPI document.
        @app.get("/dev", include_in_schema=False)
        def dev_console() -> FileResponse:
            return FileResponse(DEV_CONSOLE)

    return app


app = create_app()
