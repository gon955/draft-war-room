"""Ranking routes (SPEC 4).

    GET   /boards/{id}/rankings           board access
    POST  /boards/{id}/rankings           edit access   create or override
    PATCH /rankings/{id}                  edit access on the PARENT board
    DELETE /rankings/{id}                 edit access on the parent board
    PATCH /boards/{id}/rankings/reorder   edit access   bulk [{player_id, user_rank}]

Two prefixes (board-scoped and id-scoped), so the router carries none: the
id-scoped routes must resolve the ranking to its board before authorizing.

That resolution is the subtle part. Authorizing on the ranking id alone would
mean anyone could edit any ranking; the parent board is where permission lives,
so _ranking_for_edit loads the row, then asks about board_id — and a ranking
whose board the caller cannot see is reported as a missing ranking, never as a
forbidden one.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from warroom.authz import require_board_access, require_edit_access
from warroom.deps import CurrentUser, DbSession
from warroom.models import Player, Ranking, Tier
from warroom.schemas.ranking import RankingCreate, RankingOut, RankingPatch, ReorderIn

router = APIRouter(tags=["rankings"])


def _ranking_for_edit(db: DbSession, ranking_id: uuid.UUID, user: CurrentUser) -> Ranking:
    """Load a ranking and authorize against the board that owns it."""
    ranking = db.get(Ranking, ranking_id)
    if ranking is None:
        # Same 404 a hidden board produces, so the two are indistinguishable.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    require_edit_access(db, ranking.board_id, user)
    return ranking


def _require_tier_on_board(db: DbSession, board_id: uuid.UUID, tier_id: uuid.UUID | None) -> None:
    """A ranking may only carry a tier from its own board.

    The composite FK on (tier_id, board_id) makes this a database invariant, so
    this check is about the RESPONSE, not the integrity: without it the write
    reaches Postgres and comes back as an IntegrityError, which create_ranking
    would report as "this player is already ranked on this board" — the wrong
    error, blaming the wrong field.

    One message covers both "no such tier" and "tier on another board". Telling
    them apart would confirm that a tier id belonging to someone else's board
    exists, which is the same leak the 404-not-403 rule exists to prevent.
    """
    if tier_id is None:
        return

    tier = db.get(Tier, tier_id)
    if tier is None or tier.board_id != board_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="No such tier on this board.",
        )


@router.get("/boards/{board_id}/rankings", response_model=list[RankingOut])
def list_rankings(board_id: uuid.UUID, db: DbSession, user: CurrentUser) -> list[Ranking]:
    require_board_access(db, board_id, user)

    # NULLS LAST: an unranked player sorts after every ranked one rather than
    # ahead of the whole board, which is what Postgres would do by default for
    # ascending order.
    return list(
        db.scalars(
            select(Ranking)
            .where(Ranking.board_id == board_id)
            .order_by(Ranking.user_rank.asc().nulls_last(), Ranking.created_at)
        )
    )


@router.post(
    "/boards/{board_id}/rankings", response_model=RankingOut, status_code=status.HTTP_201_CREATED
)
def create_ranking(
    board_id: uuid.UUID, payload: RankingCreate, db: DbSession, user: CurrentUser
) -> Ranking:
    board = require_edit_access(db, board_id, user)

    player = db.get(Player, payload.player_id)
    if player is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="No such player."
        )

    # players is season-scoped shared reference data, and nothing in the schema
    # ties a ranking's player to its board's season — the FK only says the
    # player row exists. Rank a player from another season and they join to no
    # valuation (unique(league_id, player_id) is per league, and the league's
    # pool is its own season), so they would surface on the board with no value
    # and drift silently through best-available.
    if player.season != board.league.season:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"That player is not in this league's season ({board.league.season}).",
        )

    _require_tier_on_board(db, board_id, payload.tier_id)

    ranking = Ranking(board_id=board_id, **payload.model_dump())
    db.add(ranking)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # unique(board_id, player_id): one row per player per board.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This player is already ranked on this board.",
        ) from None

    return ranking


@router.patch("/rankings/{ranking_id}", response_model=RankingOut)
def patch_ranking(
    ranking_id: uuid.UUID, payload: RankingPatch, db: DbSession, user: CurrentUser
) -> Ranking:
    ranking = _ranking_for_edit(db, ranking_id, user)

    fields = payload.model_dump(exclude_unset=True)
    if "tier_id" in fields:
        _require_tier_on_board(db, ranking.board_id, fields["tier_id"])

    for field, value in fields.items():
        setattr(ranking, field, value)

    db.commit()
    return ranking


@router.delete("/rankings/{ranking_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ranking(ranking_id: uuid.UUID, db: DbSession, user: CurrentUser) -> None:
    ranking = _ranking_for_edit(db, ranking_id, user)
    db.delete(ranking)
    db.commit()


@router.patch("/boards/{board_id}/rankings/reorder", response_model=list[RankingOut])
def reorder_rankings(
    board_id: uuid.UUID, payload: ReorderIn, db: DbSession, user: CurrentUser
) -> list[Ranking]:
    """Bulk reorder, all or nothing.

    A batch naming a player with no ranking on this board changes nothing at
    all: a half-applied reorder would leave the board in an order the user never
    asked for and cannot easily undo.
    """
    require_edit_access(db, board_id, user)

    existing = {
        ranking.player_id: ranking
        for ranking in db.scalars(select(Ranking).where(Ranking.board_id == board_id))
    }

    unknown = [item.player_id for item in payload.items if item.player_id not in existing]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{len(unknown)} player(s) are not ranked on this board; nothing was changed.",
        )

    for item in payload.items:
        existing[item.player_id].user_rank = item.user_rank

    db.commit()

    return list(
        db.scalars(
            select(Ranking)
            .where(Ranking.board_id == board_id)
            .order_by(Ranking.user_rank.asc().nulls_last(), Ranking.created_at)
        )
    )
