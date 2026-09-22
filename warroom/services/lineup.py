"""Lay your own drafted players out over the league's starting slots.

Read-only, and the assignment is the engine's, not a second implementation:
valuation/draft.py already decides which slot a player occupies, and its rules
are the ones the replacement-level maths depends on. Doing it again here — or,
worse, in TypeScript on the screen — would give a lineup that disagrees with
the valuations about who is starting, which is the kind of bug nobody notices
until draft night.

Bench size is derived, not stored. `roster_slots` counts STARTING slots only
(bench seats create no starter demand, so the engine drops them), while
`roster_size` is the whole roster. The difference is the bench, and it cannot
be read off either column alone.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from warroom.models import League, MockDraft, MockPick, Player, Valuation
from warroom.services.valuation import settings_for, to_projection
from warroom.valuation.draft import RosterAssignment, assign_roster
from warroom.valuation.engine import starting_slots


@dataclass(frozen=True)
class Lineup:
    """One team's roster as seats, bench, and how much of it is still to come."""

    assignment: RosterAssignment
    players_by_espn_id: dict[int, Player]
    bench_size: int
    picks_made: int
    picks_remaining: int


def _bench_size(league: League) -> int:
    """Roster spots that are not starting slots. Never negative.

    A league whose roster_size is smaller than its starting slots is
    nonsensical but reachable — sync writes roster_size from ESPN separately
    from lineupSlotCounts — and a negative bench would render as a phantom
    seat rather than an obvious error, so it clamps.
    """
    starters = sum(starting_slots(settings_for(league)).values())
    return max(0, league.roster_size - starters)


def my_lineup(db: Session, mock: MockDraft) -> Lineup:
    """The caller's own roster in this mock, assigned to slots.

    `is_mine` is the filter, which is why generate_picks sets it at creation
    from my_draft_slot: the alternative is recomputing the snake here and
    getting a lineup that disagrees with the board it is drawn beside.
    """
    league = mock.board.league

    rows = list(
        db.scalars(
            select(MockPick)
            .options(selectinload(MockPick.player))
            .where(
                MockPick.mock_draft_id == mock.id,
                MockPick.is_mine.is_(True),
            )
            .order_by(MockPick.pick_number)
        )
    )

    mine = [pick.player for pick in rows if pick.player is not None]

    # Tie-break the placement by projected points, so that when two players
    # fit the same seats the better one starts. Without this the tie falls to
    # espn_player_id and the lineup can bench your best guard behind a worse
    # one, which is wrong for the one thing this endpoint is for.
    #
    # A left join in spirit: a player with no valuation (pool synced, league
    # not yet computed) simply gets no priority and falls to the id tie-break.
    points = dict(
        db.execute(
            select(Player.espn_player_id, Valuation.projected_points)
            .join(Valuation, Valuation.player_id == Player.id)
            .where(
                Valuation.league_id == league.id,
                Player.id.in_([p.id for p in mine]) if mine else False,
            )
        ).all()
    )

    assignment = assign_roster(
        [to_projection(p) for p in mine], settings_for(league), priority=points
    )

    return Lineup(
        assignment=assignment,
        # The engine speaks espn_player_id; the route needs the ORM rows back
        # to serialize a PlayerOut. unique(espn_player_id, season) makes this
        # mapping total over one league's season.
        players_by_espn_id={p.espn_player_id: p for p in mine},
        bench_size=_bench_size(league),
        picks_made=len(mine),
        picks_remaining=max(0, len(rows) - len(mine)),
    )


def lineup_for(db: Session, mock_id: uuid.UUID) -> Lineup | None:
    """Convenience for callers holding only an id; None when the mock is gone."""
    mock = db.get(MockDraft, mock_id)
    return None if mock is None else my_lineup(db, mock)
