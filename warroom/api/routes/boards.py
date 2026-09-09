"""Board routes (SPEC 4).

    POST   /boards        authed        must reference a league the caller can
                                        access; owner = caller
    GET    /boards        authed        owned by OR shared with the caller
    GET    /boards/{id}   board access
    PATCH  /boards/{id}   edit access
    DELETE /boards/{id}   owner only    a share, even an edit share, is not
                                        permission to delete someone's board

TODO (Phase 3).
"""
from fastapi import APIRouter

router = APIRouter(prefix="/boards", tags=["boards"])
