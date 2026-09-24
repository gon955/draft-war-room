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

WHAT THE BOTS VALUE is a knob (BotValuation). By default they use this app's
own projections; set it to ESPN and they use ESPN's published projected total
for each player instead. Only the per-player STRENGTH input changes — every
piece of scarcity machinery runs exactly as before, because live replacement
levels are derived FROM that mapping rather than alongside it. So the bots
still re-price each slot after every pick, still drain a position and lift what
is left in it, and still fill their own lineup before taking depth. They just
disagree with you about who is good, which is the entire point: a room full of
bots using your own numbers cannot show you the player your league will let
slide.
"""

import random
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from warroom.models import MockDraft, MockPick, Player, ScoringFormat, Valuation
from warroom.services.recommendation import NotValuedYet
from warroom.services.valuation import NotAPointsLeague, settings_for, to_projection
from warroom.valuation.draft import autodraft
from warroom.valuation.engine import ReplacementBasis as EngineBasis


class BotValuation(str, Enum):
    """Whose opinion of a player the simulated teams draft on.

    ENGINE  this app's projections, reconciled against the league's scoring by
            stats.py and cached on `valuations`.
    ESPN    ESPN's own projected fantasy total, already scored under this
            league's settings and stored on `players.espn_projected_points`.

    Not stored on the mock. It changes who the bots take, not what the board
    means, and re-running a mock with the other setting is the comparison
    worth having rather than a migration.
    """

    ENGINE = "engine"
    ESPN = "espn"


class NoEspnProjections(RuntimeError):
    """ESPN mode was asked for and the player pool carries no ESPN totals.

    Almost always a pool synced before the column existed: the value arrives in
    the same payload as everything else, so one re-sync fills it. Distinct from
    NotValuedYet, which is about THIS app's numbers being absent — the fix is a
    different endpoint.
    """


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
    bot_valuation: BotValuation = BotValuation.ENGINE,
) -> SimulationResult:
    """Fill the opponents' picks up to your next turn. Does not commit."""
    league = mock.board.league

    if league.scoring_format is not ScoringFormat.POINTS:
        raise NotAPointsLeague(
            "Simulated picks are driven by value over replacement, which is "
            f"points-league only; this league is scored by {league.scoring_format.value}."
        )

    # ESPN mode drives off `players` alone, and deliberately so: drafting on
    # ESPN's opinion should not require this app's engine to have run at all.
    # ENGINE mode keeps the inner join, which is also what restricts the pool to
    # players this league has actually valued — switching it to an outer join
    # would quietly admit unvalued players at 0.0 and change the default path.
    if bot_valuation is BotValuation.ESPN:
        rows: list[tuple[Player, Valuation | None]] = [
            (player, None)
            for player in db.scalars(select(Player).where(Player.season == league.season))
        ]
    else:
        rows = list(
            db.execute(
                select(Player, Valuation)
                .join(
                    Valuation,
                    (Valuation.player_id == Player.id) & (Valuation.league_id == league.id),
                )
                .where(Player.season == league.season)
            ).all()
        )

    valued = {player.id: (player, valuation) for player, valuation in rows}
    if not valued:
        raise NotValuedYet(
            "This league has no computed valuations yet, so there is nothing to "
            f"draft on. Run POST /leagues/{league.id}/valuations/compute first."
        )

    # Checked over the WHOLE pool, not over what is still available: late in a
    # draft everyone left can legitimately be someone ESPN never projected, and
    # that is a thin board rather than a misconfiguration.
    if bot_valuation is BotValuation.ESPN and not any(
        player.espn_projected_points for player, _ in rows
    ):
        raise NoEspnProjections(
            "No ESPN projections are stored for this league's players, so the "
            "simulated teams have nothing to draft on. They arrive with the "
            f"normal player sync — run POST /leagues/{league.id}/sync and try again."
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

    # THE one thing bot_valuation changes. Everything downstream — live
    # replacement levels, the marginal basis, roster fit — is derived from this
    # mapping, so swapping it swaps whose opinion the scarcity maths is applied
    # TO without touching the maths itself.
    #
    # A player ESPN did not project scores 0.0 rather than being dropped: they
    # belong in the pool (they are draftable, and they sit at the bottom of it,
    # which is where an unprojected player belongs) and removing them would
    # shrink the pool that sets every replacement level.
    if bot_valuation is BotValuation.ESPN:
        points = {p.espn_player_id: float(p.espn_projected_points or 0.0) for p, _ in available}
        # Empty on purpose. `preseason` only decorates LiveValue with how far a
        # number has moved since the preseason, and autodraft never reads it
        # when choosing. Passing this app's cached baseline here while the bots
        # price on ESPN's would report a shift between two different scales.
        preseason: dict[int, tuple[float, float]] = {}
    else:
        points = {p.espn_player_id: v.projected_points for p, v in available if v is not None}
        preseason = {
            p.espn_player_id: (v.replacement_points, v.value) for p, v in available if v is not None
        }

    made = autodraft(
        upcoming=upcoming,
        rosters=rosters,
        available=[to_projection(p) for p, _ in available],
        settings=settings,
        points=points,
        preseason=preseason,
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
