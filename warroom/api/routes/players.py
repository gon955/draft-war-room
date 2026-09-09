"""Player and valuation routes (SPEC 4).

    GET  /leagues/{id}/players              league access  filter by position,
                                            sort by value or rank, paginated
    POST /leagues/{id}/valuations/compute   owner only     runs the engine
    GET  /leagues/{id}/valuations           league access

TODO (Phase 4).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/leagues", tags=["players", "valuations"])
