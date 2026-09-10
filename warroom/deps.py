"""Shared FastAPI dependencies (SPEC 2.2, 4).

`get_current_user` decodes the bearer token and 401s when it is absent or
invalid. `get_data_source` yields the PlayerDataSource implementation:
production wires EspnDataSource, and tests override THIS dependency with
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
from warroom.valuation.data_source import EspnDataSource, PlayerDataSource

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


def get_data_source() -> PlayerDataSource:
    settings = get_settings()
    return EspnDataSource(espn_s2=settings.espn_s2, swid=settings.espn_swid)
