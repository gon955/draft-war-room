"""Shared FastAPI dependencies (SPEC 2.2, 4).

`get_current_user` decodes the bearer token and 401s when it is absent or
invalid. `get_data_source_factory` yields a builder of PlayerDataSource
implementations: production builds EspnDataSource around a given league's
cookie, and tests override THIS dependency with a factory over
FakePlayerDataSource, which is what keeps the suite off the network.

Routes depend on the interface, never on EspnDataSource directly — otherwise the
override has nothing to grab and the adapter seam stops being a seam.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError
from sqlalchemy.orm import Session

from warroom.config import get_settings
from warroom.db import get_db
from warroom.models import User
from warroom.security import decode_access_token
from warroom.valuation.data_source import (
    EspnDataSource,
    PlayerDataSource,
    PlayerDataSourceFactory,
)

DbSession = Annotated[Session, Depends(get_db)]

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    """Resolve the bearer token to a User, or 401.

    Every failure returns the same generic detail: distinguishing "expired" from
    "bad signature" from "no such user" tells an attacker which half of a guess
    was right.
    """

    def unauthorized() -> HTTPException:
        """Returns the error rather than raising it, so every `raise` below is
        visible at its call site. A helper that raises reads as if control might
        continue past it — and if it were ever edited to return instead, the
        callers would silently fall through and authenticate anyone."""
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = decode_access_token(credentials.credentials)
    except PyJWTError:
        # `from None` keeps the JWT error out of the traceback chain in logs.
        raise unauthorized() from None

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise unauthorized() from None

    # A signature can outlive its row: a token for a deleted account must not
    # authenticate, even though it verifies.
    user = db.get(User, user_id)
    if user is None:
        raise unauthorized()

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_data_source_factory() -> PlayerDataSourceFactory:
    """Build ESPN data sources, each with one league's own session cookie.

    A factory rather than a ready-made source because the credential is per
    league: leagues.espn_s2_encrypted holds the cookie that league's owner
    supplied, and it has to be decrypted at request time and handed to the
    adapter for that league's fetch. A process-wide source can only ever use
    the server's own cookie, which is not the caller's.

    `espn_s2` falls back to the environment so a deployment with its own cookie
    keeps working for leagues that have none stored — a public league needs no
    credential at all.

    SWID stays process-wide. SPEC 3 gives leagues a column for the s2 cookie
    only, so a private league is reached with a per-league s2 plus the server's
    ESPN_SWID, not with a fully per-user credential pair.

    Tests override THIS dependency; the override returns a factory, for which
    warroom.valuation.data_source.fixed_source_factory wraps a fake in one line.
    """
    settings = get_settings()

    def factory(espn_s2: str | None = None) -> PlayerDataSource:
        return EspnDataSource(espn_s2=espn_s2 or settings.espn_s2, swid=settings.espn_swid)

    return factory


DataSourceFactory = Annotated[PlayerDataSourceFactory, Depends(get_data_source_factory)]
