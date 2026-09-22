"""Tier routes (SPEC 4).

    GET  /boards/{id}/tiers   board access
    POST /boards/{id}/tiers   edit access
    PATCH  /tiers/{id}        edit access on the parent board
    DELETE /tiers/{id}        edit access on the parent board
    POST   /boards/{id}/tiers/auto   edit access   seed tiers from the engine

Two prefixes, so the router carries none — same shape as rankings.py, and for
the same reason: the id-scoped routes must resolve the tier to its board before
they can authorize anything. Authorizing on the tier id alone would let anyone
rename anyone else's tiers, and a tier whose board the caller cannot see is
reported as a missing tier rather than a forbidden one (SPEC 0.3).

GET takes board ACCESS, not edit: a read-share holder who cannot see the tiers
sees an ungrouped board, which makes the share close to worthless.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from warroom.authz import require_board_access, require_edit_access
from warroom.deps import CurrentUser, DbSession
from warroom.models import Tier
from warroom.schemas.tier import AutoTierIn, TierCreate, TierOut, TierPatch
from warroom.services import tiering

router = APIRouter(tags=["tiers"])


def _tier_for_edit(db: DbSession, tier_id: uuid.UUID, user: CurrentUser) -> Tier:
    tier = db.get(Tier, tier_id)
    if tier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    require_edit_access(db, tier.board_id, user)
    return tier


@router.get("/boards/{board_id}/tiers", response_model=list[TierOut])
def list_tiers(board_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[Tier]:
    require_board_access(db, board_id, user)

    return list(
        db.scalars(
            select(Tier).where(Tier.board_id == board_id).order_by(Tier.sort_order, Tier.label)
        )
    )


@router.post(
    "/boards/{board_id}/tiers", response_model=TierOut, status_code=status.HTTP_201_CREATED
)
def create_tier(board_id: uuid.UUID, payload: TierCreate, db: DbSession, user: CurrentUser) -> Tier:
    require_edit_access(db, board_id, user)

    fields = payload.model_dump()
    if fields["sort_order"] is None:
        highest = db.scalar(select(func.max(Tier.sort_order)).where(Tier.board_id == board_id))
        fields["sort_order"] = 0 if highest is None else highest + 1

    tier = Tier(board_id=board_id, **fields)
    db.add(tier)
    db.commit()
    return tier


@router.post(
    "/boards/{board_id}/tiers/auto",
    response_model=list[TierOut],
    status_code=status.HTTP_201_CREATED,
)
def autotier(
    board_id: uuid.UUID, payload: AutoTierIn, db: DbSession, user: CurrentUser
) -> list[Tier]:
    """Seed this board's tiers from the league's computed values (SPEC 5.4).

    Not in SPEC 4's endpoint list, which simply does not enumerate it — SPEC 5.4
    and SPEC 8 both call for the feature. It has to be board-scoped, because
    tiers are, which is why it cannot hang off the league-scoped compute route.

    DESTRUCTIVE: it replaces the board's existing tiers. Rankings survive with
    their tier_id reassigned, and user_rank, notes and flags are never touched.

    "/tiers/auto" does not collide with "/tiers": FastAPI matches whole paths
    and a path parameter never spans a slash.
    """
    board = require_edit_access(db, board_id, user)

    try:
        tiers = tiering.autotier_board(
            db, board, n_tiers=payload.n_tiers, pool_size=payload.pool_size
        )
        db.commit()
    except tiering.NotValued as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    return tiers


@router.patch("/tiers/{tier_id}", response_model=TierOut)
def patch_tier(tier_id: uuid.UUID, payload: TierPatch, db: DbSession, user: CurrentUser) -> Tier:
    tier = _tier_for_edit(db, tier_id, user)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(tier, field, value)
    db.commit()
    return tier


@router.delete("/tiers/{tier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tier(tier_id: uuid.UUID, db: DbSession, user: CurrentUser) -> None:
    tier = _tier_for_edit(db, tier_id, user)
    db.delete(tier)
    db.commit()
