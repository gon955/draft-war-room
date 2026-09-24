"""Re-value a league's undrafted pool against a mock draft in progress.

The only module that knows both valuation/draft.py and the database, exactly as
services/valuation.py is the only one that knows engine.py and the database.

READ-ONLY, DELIBERATELY. Nothing here writes to `valuations`, and it must stay
that way: that table is a per-LEAGUE cache with unique(league_id, player_id),
while a mock is per-BOARD and a board can hold several at once. Persisting
draft-adjusted numbers would let two mocks of the same league overwrite each
other's idea of what a player is worth, and would corrupt the preseason
baseline that the shift is measured against.

The whole computation runs off `valuations.projected_points`, which is already
cached. That is what keeps it cheap enough to do per request: no ESPN call, no
re-projection, no second pass through stats.py — just arithmetic over a few
hundred (positions, points) pairs.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from warroom.config import get_settings
from warroom.models import MockDraft, MockPick, Player, Ranking, ScoringFormat, Valuation
from warroom.services.valuation import NotAPointsLeague, settings_for, to_projection
from warroom.valuation.draft import (
    LiveValue,
    bench_replacement,
    default_slot_eligibility,
    expected_next_best,
    lineup_deltas,
    live_replacement_levels,
    live_values,
    open_slots,
    pick_scores,
    picks_until_next,
    survival_probabilities,
)
from warroom.valuation.engine import ReplacementBasis as EngineBasis


class NotValuedYet(RuntimeError):
    """The league has no cached valuations, so there is nothing to re-value.

    Distinct from an empty player pool: the pool may be synced and simply never
    valued. Without projected_points there is no arithmetic to do at all, so
    this is a 422 telling the caller to run POST /valuations/compute, not an
    empty page that reads as "no good players left".
    """


@dataclass(frozen=True)
class Recommendation:
    """One undrafted player, everything the route needs to render them."""

    player: Player
    valuation: Valuation
    ranking: Ranking | None
    live: LiveValue
    # Whether any seat you have not filled accepts this player. Distinct from
    # marginal > 0: late on, when everyone left grades at or under
    # replacement, marginal is 0 for the whole board and this is the only
    # thing still separating a player who plugs your hole from one who
    # cannot.
    fills_open_seat: bool
    # P(still on the board when you pick again).
    survival: float
    # What you could expect to get next turn, having taken this player now.
    expected_next: float
    # The SIGNED lineup change, where `marginal` is its clamp at zero. Kept
    # because from the moment your lineup fills, marginal is 0.0 for every
    # candidate and can no longer order them — this still can, by how far each
    # one sits below the starter they would have to displace. That is what
    # stops a fifth centre outranking your only backup point guard.
    lineup_delta: float
    # marginal + expected_next: what these two picks are worth together.
    score: float
    # What this player would add to YOUR starting lineup, in the same units as
    # live value. 0.0 means every seat they fit is held by someone better, so
    # they are depth rather than an upgrade.
    marginal: float


def recommend(db: Session, mock: MockDraft) -> tuple[list[Recommendation], int]:
    """Every undrafted player in this mock, re-valued against the live board.

    Returns them best-first by live value, plus how many picks you have to
    wait through before your next turn. Position filtering and paging belong
    to the caller and MUST happen after this returns — see the route.
    """
    board = mock.board
    league = board.league

    if league.scoring_format is not ScoringFormat.POINTS:
        raise NotAPointsLeague(
            "Draft recommendations are value over replacement, which is "
            f"points-league only; this league is scored by {league.scoring_format.value}."
        )

    # One pass for the pool and its valuations. An inner join, not an outer
    # one: a player with no valuation has no projected_points, so there is
    # nothing to rank them by and nothing to re-baseline. They are absent from
    # the recommendation rather than silently sorted to the bottom.
    rows = db.execute(
        select(Player, Valuation)
        .join(Valuation, (Valuation.player_id == Player.id) & (Valuation.league_id == league.id))
        .where(Player.season == league.season)
    ).all()
    if not rows:
        raise NotValuedYet(
            "This league has no computed valuations yet. Run POST "
            f"/leagues/{league.id}/valuations/compute first."
        )

    valued: dict[uuid.UUID, tuple[Player, Valuation]] = {p.id: (p, v) for p, v in rows}

    # Drafted players, grouped into one roster per team. team_slot is what
    # makes opponent rosters legible at all — is_mine only marks your own.
    # Named board_rows, not board: `board` is already the Board this mock
    # hangs off, and the rankings query below reads board.id.
    board_rows = db.execute(
        select(MockPick.pick_number, MockPick.team_slot, MockPick.player_id, MockPick.is_mine)
        .where(MockPick.mock_draft_id == mock.id)
        .order_by(MockPick.pick_number)
    ).all()
    picks = [(slot, pid, mine) for _, slot, pid, mine in board_rows if pid is not None]

    rosters: dict[int, list] = defaultdict(list)
    drafted: set[uuid.UUID] = set()
    mine: list = []
    for team_slot, player_id, is_mine in picks:
        drafted.add(player_id)
        # A drafted player only needs positions here, to work out which slot
        # they consume, so one without a valuation still counts against demand.
        # Falling back to a direct load keeps the seat count honest.
        entry = valued.get(player_id)
        player = entry[0] if entry else db.get(Player, player_id)
        if player is not None:
            projection = to_projection(player)
            rosters[team_slot].append(projection)
            if is_mine:
                mine.append(projection)

    available = [(p, v) for p, v in valued.values() if p.id not in drafted]

    settings = settings_for(league)
    # The live board answers the same question the cached one does; a
    # board that redefines value the moment you start drafting is not one
    # you can practise against.
    basis = EngineBasis(settings.replacement_basis)
    projections = [to_projection(p) for p, _ in available]
    points = {p.espn_player_id: v.projected_points for p, v in available}

    results = live_values(
        available=projections,
        rosters=list(rosters.values()),
        settings=settings,
        points=points,
        preseason={p.espn_player_id: (v.replacement_points, v.value) for p, v in available},
        basis=basis,
    )

    # Step 2: the same board, re-read against YOUR seats. The replacement
    # levels are recomputed rather than passed out of live_values because the
    # rostered players need valuing on the same basis as the candidates — a
    # lineup that compares one player's VOR against another's raw projection
    # would rate every incumbent replaceable.
    replacement = live_replacement_levels(projections, list(rosters.values()), settings, points)
    # Points for EVERY valued player, drafted or not: marginal_values has to
    # price the incumbents in your lineup, and they are by definition absent
    # from the available pool.
    all_points = {p.espn_player_id: v.projected_points for p, v in valued.values()}

    needed = open_slots(mine, settings)

    delta = lineup_deltas(
        candidates=results,
        roster=mine,
        settings=settings,
        replacement=replacement,
        bench=bench_replacement(projections, replacement, points),
        points=all_points,
    )
    # The clamp, for display. `delta` keeps the ordering the clamp destroys.
    marginal = {pid: max(0.0, d) for pid, d in delta.items()}

    # Step 3. Who picks between this pick and your next one, and what each
    # of those teams is short of — a team with your position still open is a
    # threat to your shortlist, one with it filled is not.
    upcoming = [(n, slot, is_mine) for n, slot, pid, is_mine in board_rows if pid is None]
    intervening = picks_until_next(upcoming)

    # The room's board, for the survival model only. Built from ESPN's own
    # projected total, which every other manager in the league can see and
    # this app's cannot be. A pool synced before that column existed yields an
    # empty mapping, and field_order then falls back to our own ordering —
    # the previous behaviour, rather than an error.
    market_rank = {
        player.espn_player_id: rank
        for rank, (player, _) in enumerate(
            sorted(
                (pair for pair in valued.values() if pair[0].espn_projected_points is not None),
                key=lambda pair: -pair[0].espn_projected_points,
            )
        )
    }

    survival = survival_probabilities(
        results,
        [rosters.get(slot, []) for slot in intervening],
        settings,
        market_rank=market_rank,
        market_weight=get_settings().draft_market_weight,
    )
    expected = expected_next_best(results, marginal, survival)
    scores = pick_scores(marginal, expected)

    # The engine speaks espn_player_id end to end; the rows are keyed by UUID.
    # unique(espn_player_id, season) plus the season filter above makes this
    # mapping total, the same argument services/valuation.py makes.
    by_espn_id = {p.espn_player_id: (p, v) for p, v in available}

    rankings = {
        r.player_id: r for r in db.scalars(select(Ranking).where(Ranking.board_id == board.id))
    }

    out = []
    for result in results:
        player, valuation = by_espn_id[result.value.player.espn_player_id]
        out.append(
            Recommendation(
                player,
                valuation,
                rankings.get(player.id),
                result,
                any(
                    default_slot_eligibility(slot, result.value.player.positions) for slot in needed
                ),
                survival.get(result.value.player.espn_player_id, 1.0),
                expected.get(result.value.player.espn_player_id, 0.0),
                delta.get(result.value.player.espn_player_id, 0.0),
                scores.get(result.value.player.espn_player_id, 0.0),
                marginal.get(result.value.player.espn_player_id, 0.0),
            )
        )
    return out, len(intervening)
