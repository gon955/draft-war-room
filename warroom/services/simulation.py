"""Auto-draft the other teams so one person can run a mock alone.

Loads the board once, runs the whole simulation in memory, writes the picks in
one transaction. Not one query per pick: a 10-team, 13-round league is 117
opponent picks, and a round trip inside that loop turns a sub-second call into
a visible hang.

WRITES, so the route behind it needs edit access, not board access — this is
the only service here that changes a mock rather than reading one.

Determinism is a feature, not a test convenience. `seed` makes a simulated
draft reproducible, which is what lets you replay the same board against a
different strategy of your own and see whether the difference was your pick or
the dice.
"""

import random
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from warroom.models import MockDraft, MockPick, Player, ScoringFormat, Valuation
from warroom.services.recommendation import NotValuedYet
from warroom.services.valuation import NotAPointsLeague, settings_for, to_projection
from warroom.valuation.draft import autodraft
from warroom.valuation.engine import ReplacementBasis as EngineBasis


@dataclass(frozen=True)
class MadePick:
    """One persisted simulated pick, for the response."""

    pick_number: int
    team_slot: int
    player: Player


@dataclass(frozen=True)
class SimulationResult:
    picks: list[MadePick]
    next_pick_number: int | None
    next_is_mine: bool
    board_complete: bool


def simulate(
    db: Session,
    mock: MockDraft,
    reach: int = 3,
    seed: int | None = None,
    stop_at_my_pick: bool = True,
) -> SimulationResult:
    """Fill the opponents' picks up to your next turn. Does not commit."""
    league = mock.board.league

    if league.scoring_format is not ScoringFormat.POINTS:
        raise NotAPointsLeague(
            "Simulated picks are driven by value over replacement, which is "
            f"points-league only; this league is scored by {league.scoring_format.value}."
        )

    valued = {
        player.id: (player, valuation)
        for player, valuation in db.execute(
            select(Player, Valuation)
            .join(
                Valuation,
                (Valuation.player_id == Player.id) & (Valuation.league_id == league.id),
            )
            .where(Player.season == league.season)
        ).all()
    }
    if not valued:
        raise NotValuedYet(
            "This league has no computed valuations yet, so there is nothing to "
            f"draft on. Run POST /leagues/{league.id}/valuations/compute first."
        )

    picks = list(
        db.scalars(
            select(MockPick).where(MockPick.mock_draft_id == mock.id).order_by(MockPick.pick_number)
        )
    )

    rosters: dict[int, list] = {pick.team_slot: [] for pick in picks}
    drafted: set[uuid.UUID] = set()
    for pick in picks:
        if pick.player_id is None:
            continue
        drafted.add(pick.player_id)
        entry = valued.get(pick.player_id)
        if entry is not None:
            rosters[pick.team_slot].append(to_projection(entry[0]))

    available = [(p, v) for p, v in valued.values() if p.id not in drafted]

    # The board from the first empty slot onward, in pick order. Filled slots
    # earlier in the board are skipped rather than redrafted, so simulating
    # twice is safe and picks you already made are never overwritten.
    upcoming = [(p.pick_number, p.team_slot, p.is_mine) for p in picks if p.player_id is None]

    settings = settings_for(league)

    made = autodraft(
        upcoming=upcoming,
        rosters=rosters,
        available=[to_projection(p) for p, _ in available],
        settings=settings,
        points={p.espn_player_id: v.projected_points for p, v in available},
        preseason={p.espn_player_id: (v.replacement_points, v.value) for p, v in available},
        # The league's own basis, so the bots and the board you read are
        # answering the same question. This was pinned to STARTER while the
        # marginal basis cost a league-wide re-solve per PLAYER per pick —
        # a full draft took 40s. Reading the baseline off the seating instead
        # brought that to 4s, which is affordable; it is still ~5x STARTER,
        # so pin it back here if simulating ever feels slow.
        basis=EngineBasis(settings.replacement_basis),
        rng=random.Random(seed),
        reach=reach,
        stop_at_my_pick=stop_at_my_pick,
    )

    row_by_espn_id = {p.espn_player_id: p for p, _ in available}
    by_number = {p.pick_number: p for p in picks}

    written: list[MadePick] = []
    for sim in made:
        player = row_by_espn_id[sim.espn_player_id]
        by_number[sim.pick_number].player_id = player.id
        written.append(
            MadePick(pick_number=sim.pick_number, team_slot=sim.team_slot, player=player)
        )

    still_open = [p for p in picks if p.player_id is None]
    return SimulationResult(
        picks=written,
        next_pick_number=still_open[0].pick_number if still_open else None,
        next_is_mine=bool(still_open and still_open[0].is_mine),
        board_complete=not still_open,
    )
