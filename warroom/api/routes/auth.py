"""Auth routes (SPEC 4).

POST /auth/register   public   409 on a duplicate email
POST /auth/login      public   -> {access_token}
GET  /auth/me         authed   401 without a token

These are the only two routes reachable without a token, and each spends a full
argon2 hash, so both carry warroom.throttle's limiter. It is declared on the
ROUTER rather than on the two handlers: a third unauthenticated endpoint added
here is then throttled by default instead of by whoever remembers. GET /auth/me
is authenticated and cheap, and is throttled only as a side effect of sharing
the router — which is harmless and not worth a second router to avoid.

Both lookups below match on lower(email), not on email. The only index on the
table is the functional unique one over lower(email) (models.py), so an
equality on the bare column cannot use it and every login seq-scans users.
Addresses are already lowercased on the way in by schemas.auth.LowercaseEmail,
so the two forms select the same rows — this one just does it with the index.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from warroom.deps import CurrentUser, DbSession
from warroom.models import User
from warroom.schemas.auth import LoginIn, RegisterIn, TokenOut, UserOut
from warroom.security import (
    PasswordHashingBusy,
    create_access_token,
    dummy_verify,
    hash_password,
    verify_and_upgrade,
)
from warroom.throttle import throttle_auth

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(throttle_auth)])


def _busy() -> HTTPException:
    """503 when every password-hashing slot is taken.

    Not a 500: the request was well formed and the server is healthy, it simply
    has no memory budget free this instant (see security.py). Retry-After makes
    that actionable rather than mysterious.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="The server is busy verifying other sign-ins. Try again in a moment.",
        headers={"Retry-After": "2"},
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: DbSession) -> User:
    existing_user = db.scalar(select(User).where(func.lower(User.email) == payload.email))

    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists."
        )

    try:
        password_hash = hash_password(payload.password)
    except PasswordHashingBusy:
        raise _busy() from None

    user = User(email=payload.email, password_hash=password_hash)
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists."
        ) from None

    return user


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: DbSession) -> TokenOut:
    unauthorized_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="incorrect email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user = db.scalar(select(User).where(func.lower(User.email) == payload.email))

    try:
        if user is None:
            # Same cost as a real verify, so the endpoint does not answer
            # "does this address have an account?" to anyone with a stopwatch.
            dummy_verify()
            raise unauthorized_exception

        # verify_and_upgrade, not verify: argon2 stores its cost parameters
        # inside the hash, so a row written under the old 64 MiB defaults keeps
        # costing 64 MiB to check for as long as it sits there. This rewrites it
        # at the current cost on the first successful login after the change,
        # which is what actually retires the old parameters — lowering the
        # setting alone only affects accounts created afterwards.
        ok, upgraded = verify_and_upgrade(payload.password, user.password_hash)
    except PasswordHashingBusy:
        raise _busy() from None

    if not ok:
        raise unauthorized_exception

    if upgraded is not None:
        user.password_hash = upgraded
        db.commit()

    token_str = create_access_token(subject=user.id)
    return TokenOut(access_token=token_str)


@router.get("/me", response_model=UserOut)
def get_me(user: CurrentUser) -> User:
    return user
