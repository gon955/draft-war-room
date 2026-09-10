"""Auth routes (SPEC 4).

    POST /auth/register   public   409 on a duplicate email
    POST /auth/login      public   -> {access_token}
    GET  /auth/me         authed   401 without a token

TODO (Phase 2).
"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from warroom.deps import CurrentUser, DbSession
from warroom.models import User
from warroom.schemas.auth import LoginIn, RegisterIn, TokenOut, UserOut
from warroom.security import create_access_token, hash_password, pwd_context, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterIn, db: DbSession) -> User:
    existing_user = db.scalar(select(User).where(User.email == payload.email))

    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A user with this email already exists."
        )

    user = User(email=payload.email, password_hash=hash_password(payload.password))
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

    user = db.scalar(select(User).where(User.email == payload.email))

    if user is None:
        pwd_context.dummy_verify()
        raise unauthorized_exception
    if not verify_password(payload.password, user.password_hash):
        raise unauthorized_exception

    token_str = create_access_token(subject=user.id)
    return TokenOut(access_token=token_str)


@router.get("/me", response_model=UserOut)
def get_me(user: CurrentUser) -> User:
    return user
