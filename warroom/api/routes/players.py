"""Player and valuation routes (SPEC 4).

    GET  /leagues/{id}/players              league access  filter by position,
                                            sort by value or rank, paginated
    POST /leagues/{id}/valuations/compute   owner only     runs the engine
    GET  /leagues/{id}/valuations           league access

Read access, not ownership, for the two GETs — authz.require_league_access
grants it to share-holders too, because a read share that shows the board but
not one player on it is worthless.

These reads drive off players (filtered by league.season) and outer-join
valuations, NOT off rankings: rankings only has rows for players the user has
touched (see services/tiering.py), so a rankings-driven query would hide most of
the pool. ix_valuations_league_id_value exists for the value-sorted page.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from warroom.authz import require_league_access, require_league_owner
from warroom.deps import CurrentUser, DbSession
from warroom.models import Player, Valuation
from warroom.schemas.player import (
    ComputeResult,
    Page,
    PlayerOut,
    PlayerSort,
    PlayerWithValuationOut,
    Position,
    ValuationOut,
)
from warroom.services import valuation as valuation_service

router = APIRouter(prefix="/leagues", tags=["players", "valuations"])

# Shared by both paginated reads. 200 is a page size a UI can actually render;
# the cap exists so one request cannot ask for the whole pool.
LimitParam = Annotated[int, Query(ge=1, le=200)]
OffsetParam = Annotated[int, Query(ge=0)]


@router.get("/{league_id}/players", response_model=Page[PlayerWithValuationOut])
def list_players(
    league_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    position: Position | None = None,
    sort: PlayerSort = PlayerSort.VALUE,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
) -> Page[PlayerWithValuationOut]:
    """The league's cached pool with this league's valuations attached."""
    league = require_league_access(db, league_id, user)

    # league_id belongs in the ON clause, NOT the WHERE. In the WHERE it
    # discards every row whose valuation is NULL, so this outer join silently
    # becomes an inner one and a synced-but-unvalued pool returns nothing.
    joined = select(Player, Valuation).outerjoin(
        Valuation,
        (Valuation.player_id == Player.id) & (Valuation.league_id == league.id),
    )

    filters = [Player.season == league.season]
    if position is not None:
        # JSONB containment: positions @> '["PG"]'
        filters.append(Player.positions.contains([position.value]))

    order = {
        # NULLS LAST, so a player the engine has not valued yet sorts behind
        # every valued one instead of ahead of the whole pool — which is what
        # Postgres does by default for DESC.
        PlayerSort.VALUE: Valuation.value.desc().nulls_last(),
        PlayerSort.PROJECTED_POINTS: Valuation.projected_points.desc().nulls_last(),
        PlayerSort.NAME: Player.name.asc(),
    }[sort]

    # Counted off players with the same filters and no join: the join would be
    # wasted work here, and counting over it would agree only by accident of
    # unique(league_id, player_id).
    total = db.scalar(select(func.count()).select_from(Player).where(*filters))

    rows = db.execute(
        joined.where(*filters)
        # espn_player_id as the tiebreak. Values tie for real — two players on
        # the same replacement level are both exactly 0.0 — and without a
        # second key the same page comes back in a different order each call.
        .order_by(order, Player.espn_player_id)
        .limit(limit)
        .offset(offset)
    ).all()

    return Page[PlayerWithValuationOut](
        items=[
            PlayerWithValuationOut(
                player=PlayerOut.model_validate(player),
                valuation=ValuationOut.model_validate(valuation) if valuation is not None else None,
            )
            for player, valuation in rows
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post("/{league_id}/valuations/compute", response_model=ComputeResult)
def compute_valuations(league_id: uuid.UUID, db: DbSession, user: CurrentUser) -> ComputeResult:
    """Run the engine over the cached pool and persist the results.

    Owner only (SPEC 4): it overwrites reference data every share-holder reads.
    Takes no data source — the engine values the CACHED pool, so this endpoint
    never touches ESPN and a stale pool is fixed with /sync, not here.
    """
    league = require_league_owner(db, league_id, user)

    try:
        valued = valuation_service.compute_valuations(db, league)
        db.commit()
    except (valuation_service.NotAPointsLeague, valuation_service.EmptyPlayerPool) as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    # Not the rows' own computed_at: reading that back costs another query, and
    # this is the same "what the write did" shape as league.SyncResult.
    return ComputeResult(players_valued=valued, computed_at=datetime.now(UTC))


@router.get("/{league_id}/valuations", response_model=Page[ValuationOut])
def list_valuations(
    league_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    limit: LimitParam = 50,
    offset: OffsetParam = 0,
) -> Page[ValuationOut]:
    """The engine's output for this league, best first."""
    league = require_league_access(db, league_id, user)

    total = db.scalar(
        select(func.count()).select_from(Valuation).where(Valuation.league_id == league.id)
    )

    # Joined to players only for the tiebreak; ix_valuations_league_id_value
    # still drives the leading (league_id, value DESC) columns. Ordering by
    # espn_player_id inside a tie is also what makes this read reproduce the
    # SPEC 5.3 worked example exactly rather than approximately.
    rows = db.scalars(
        select(Valuation)
        .join(Player, Player.id == Valuation.player_id)
        .where(Valuation.league_id == league.id)
        .order_by(Valuation.value.desc(), Player.espn_player_id.asc())
        .limit(limit)
        .offset(offset)
    )

    return Page[ValuationOut](
        items=[ValuationOut.model_validate(v) for v in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
