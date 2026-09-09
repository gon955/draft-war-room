"""League routes (SPEC 4).

    POST   /leagues              authed       owner = caller; pulls settings,
                                              point weights and the player pool
                                              from the injected PlayerDataSource
    GET    /leagues              authed       only the caller's leagues
    GET    /leagues/{id}         owner only
    POST   /leagues/{id}/sync    owner only   refresh players / projections
    DELETE /leagues/{id}         owner only

Creation and sync both go through services.sync, never through espn-api here.

TODO (Phase 3).
"""
from fastapi import APIRouter

router = APIRouter(prefix="/leagues", tags=["leagues"])
