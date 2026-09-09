"""Ranking routes (SPEC 4).

    GET   /boards/{id}/rankings           board access
    POST  /boards/{id}/rankings           edit access   create or override
    PATCH /rankings/{id}                  edit access on the PARENT board
    DELETE /rankings/{id}                 edit access on the parent board
    PATCH /boards/{id}/rankings/reorder   edit access   bulk [{player_id, user_rank}]

Two prefixes (board-scoped and id-scoped), so the router carries none: the
id-scoped routes must resolve the ranking to its board before authorizing.

TODO (Phase 3).
"""
from fastapi import APIRouter

router = APIRouter(tags=["rankings"])
