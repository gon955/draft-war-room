"""Auth routes (SPEC 4).

    POST /auth/register   public   409 on a duplicate email
    POST /auth/login      public   -> {access_token}
    GET  /auth/me         authed   401 without a token

TODO (Phase 2).
"""
from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])
