"""Tier routes (SPEC 4).

    GET  /boards/{id}/tiers   board access
    POST /boards/{id}/tiers   edit access
    PATCH  /tiers/{id}        edit access on the parent board
    DELETE /tiers/{id}        edit access on the parent board

TODO (Phase 3).
"""
from fastapi import APIRouter

router = APIRouter(tags=["tiers"])
