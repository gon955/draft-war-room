"""Board share routes — owner-only management (SPEC 4).

    GET    /boards/{id}/shares   owner only
    POST   /boards/{id}/shares   owner only   share with a user by email
    DELETE /shares/{id}          owner only

A non-owner asking about a board's shares gets 404: the share list is exactly
the sort of thing that leaks who else exists.

TODO (Phase 3).
"""

from fastapi import APIRouter

router = APIRouter(tags=["shares"])
