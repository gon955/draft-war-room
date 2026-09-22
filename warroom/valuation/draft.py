"""Draft-time valuation: what a player is worth given the board as it stands.

engine.py answers "who is the best player" once, in the preseason, against the
whole pool. That is the right question in August and the wrong one at pick 37.
By then a third of the pool is gone, and the replacement level every VOR number
is measured against has moved — usually a long way, and unevenly by slot.

So this module re-asks the engine's question against the live board:

    replacement level at slot s, mid-draft
      = the projected points of the (seats still unfilled at s)-th best
        UNDRAFTED player eligible for s

Both halves shrink as the draft runs, and they shrink at different rates per
slot. That is the whole point: once eight of ten centre seats are gone, the
replacement centre craters and the centres still on the board gain value,
while a position nobody has touched barely moves. A static board cannot show
that, which is why `best-available` alone goes stale by the third round.

Same rules as engine.py and stats.py: plain dataclasses in, values out, no
SQLAlchemy, no FastAPI, no network. `points` is passed in rather than computed
because `valuations.projected_points` is already cached per league — the
expensive half (stats reconciliation, ESPN's vocabulary) was paid at compute
time and must not be paid again on every pick.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from .domain import LeagueSettings, PlayerProjection, PlayerValue
from .engine import (
    ReplacementBasis,
    compute_replacement_levels,
    default_slot_eligibility,
    marginal_replacements,
    starting_slots,
)

Eligibility = Callable[[str, tuple[str, ...]], bool]

# What a player is credited at when every slot they fit is already saturated:
# they can still be rostered, just not started anywhere.
BENCH_SLOT = "BENCH"


@dataclass(frozen=True)
class LiveValue:
    """One player's draft-time value, and how far it has moved since preseason.

    `shift` is the reason this module exists, so it is returned rather than
    left for the caller to subtract: a player whose value rose 90 points
    because their slot drained is the recommendation, and the number that
    explains it should travel with it.
    """

    value: PlayerValue
    preseason_replacement: float
    preseason_value: float

    @property
    def shift(self) -> float:
        """How much the replacement level moved. Negative = the slot drained."""
        return self.value.replacement_points - self.preseason_replacement

    @property
    def value_change(self) -> float:
        """How much this player's VOR moved as a result. Positive = gained."""
        return self.value.value - self.preseason_value


# Lineups read top-down in a conventional order, and roster_slots cannot give
# us one: it is JSONB, so Postgres hands the keys back sorted by length then
# bytes ("G", "PG", "UT", "G/F", "PF/C", "SG/SF"). Specific positions first,
# then the flex slots that can absorb them, which is also roughly scarcest
# first. Anything unrecognised sorts after these, alphabetically.
SLOT_DISPLAY_ORDER = (
    "PG", "SG", "SF", "PF", "C",
    "G", "F",
    "PG/SG", "SG/SF", "SF/PF", "PF/C", "G/F", "F/C",
    "UTIL", "UT",
)  # fmt: skip


def _display_key(slot: str) -> tuple[int, str]:
    upper = slot.upper()
    known = (
        SLOT_DISPLAY_ORDER.index(upper) if upper in SLOT_DISPLAY_ORDER else len(SLOT_DISPLAY_ORDER)
    )
    return (known, upper)


@dataclass(frozen=True)
class Seat:
    """One starting slot on one team, filled or empty.

    `index` numbers the seats within a slot from 1, so a league with PF/C:2
    has two distinguishable seats rather than one slot with a list in it —
    which is what lets the UI draw an empty seat next to a filled one.
    """

    slot: str
    index: int
    player: PlayerProjection | None


@dataclass(frozen=True)
class RosterAssignment:
    """A team's drafted players laid out over their starting slots and bench."""

    seats: tuple[Seat, ...]
    bench: tuple[PlayerProjection, ...]

    @property
    def counts(self) -> dict[str, int]:
        """Seats filled per slot — what the replacement-level maths consumes."""
        filled: dict[str, int] = {}
        for seat in self.seats:
            filled[seat.slot] = filled.get(seat.slot, 0) + (seat.player is not None)
        return filled


def assign_roster(
    roster: Iterable[PlayerProjection],
    settings: LeagueSettings,
    eligibility: Eligibility = default_slot_eligibility,
    priority: Mapping[int, float] | None = None,
) -> RosterAssignment:
    """Lay one team's drafted players out over their starting slots.

    Most-constrained-first, and that ordering is load-bearing rather than
    tidiness. Take a roster of one pure C and one PF/C into {PF: 1, C: 1}: fill
    in roster order and the PF/C may take C, leaving the pure C on the bench
    behind an empty PF seat this team can never fill. Assigning the player with
    the fewest eligible slots first cannot make that mistake.

    Within a player, the scarcest slot wins — fewest seats per team, so C (1)
    is consumed before UTIL (3). Same instinct as the engine crediting a player
    at their scarcest slot, with seat count standing in for replacement level,
    which is not yet known at this point in the computation.

    `priority` breaks ties between equally constrained players, higher first;
    pass projected points and the better player takes the seat. It does not
    change HOW MANY seats get filled — that is fixed by the eligibility graph
    — so the counts the replacement-level maths reads are identical with it
    and without it. What it changes is WHO ends up on the bench, which is the
    whole difference between a lineup you would field and one you would not.
    Without it the tie falls to espn_player_id, i.e. roughly who has been in
    the league longest, which is meaningless for fantasy purposes.

    This is a greedy pass, not a maximum matching. It is exact whenever a
    perfect assignment exists for the eligibility graphs real NBA leagues have
    (a chain of nested flex slots), and its failure mode is a bench player who
    could in principle have been shuffled in — never a wrong seat.
    """
    slots = starting_slots(settings)
    rank = priority or {}

    def eligible_slots(player: PlayerProjection) -> list[str]:
        return [s for s in slots if eligibility(s, player.positions)]

    def order(player: PlayerProjection) -> tuple[int, float, int]:
        # Most constrained first, then the better player, then espn_player_id
        # so the same roster always produces the same lineup.
        return (
            len(eligible_slots(player)),
            -rank.get(player.espn_player_id, 0.0),
            player.espn_player_id,
        )

    placed: dict[str, list[PlayerProjection]] = {s: [] for s in slots}
    bench: list[PlayerProjection] = []

    for player in sorted(roster, key=order):
        with_room = [s for s in eligible_slots(player) if len(placed[s]) < slots[s]]
        if not with_room:
            bench.append(player)  # A roster spot, but no lineup spot.
            continue
        placed[min(with_room, key=lambda s: (slots[s], s))].append(player)

    seats: list[Seat] = []
    for slot in sorted(slots, key=_display_key):
        for index in range(slots[slot]):
            occupant = placed[slot][index] if index < len(placed[slot]) else None
            seats.append(Seat(slot=slot, index=index + 1, player=occupant))

    return RosterAssignment(seats=tuple(seats), bench=tuple(bench))


def assign_to_slots(
    roster: Iterable[PlayerProjection],
    settings: LeagueSettings,
    eligibility: Eligibility = default_slot_eligibility,
) -> dict[str, int]:
    """How many starting seats one team's drafted players occupy, per slot.

    A count view of assign_roster, kept because the replacement-level maths
    wants numbers and the lineup screen wants names — from one placement, so
    the two can never disagree about who is starting.
    """
    assignment = assign_roster(roster, settings, eligibility)
    return {slot: assignment.counts.get(slot, 0) for slot in starting_slots(settings)}


def consumed_slots(
    rosters: Iterable[Sequence[PlayerProjection]],
    settings: LeagueSettings,
    eligibility: Eligibility = default_slot_eligibility,
) -> dict[str, int]:
    """Starting seats taken league-wide, summed over every team's roster.

    League-wide, not just the opponents': a replacement level is a property of
    the whole league's demand, so your own picks drain it exactly as theirs do.
    """
    total = dict.fromkeys(starting_slots(settings), 0)
    for roster in rosters:
        for slot, count in assign_to_slots(roster, settings, eligibility).items():
            total[slot] += count
    return total


def remaining_demand(settings: LeagueSettings, consumed: Mapping[str, int]) -> dict[str, int]:
    """Starter seats still open at each slot; never negative."""
    return {
        slot: max(0, settings.num_teams * count - consumed.get(slot, 0))
        for slot, count in starting_slots(settings).items()
    }


def bench_replacement(
    available: Sequence[PlayerProjection],
    replacement: Mapping[str, float],
    points: Mapping[int, float],
) -> float:
    """What to measure a player against when every slot they fit is saturated.

    Not 0.0, which is what engine.py uses for a player eligible nowhere. In the
    preseason that case means broken position data and is vanishingly rare; the
    moment slots start saturating it becomes common, and a 0.0 replacement
    hands the player their whole projection as value — so the recommender would
    put a benched centre at the top of the board precisely because nobody can
    start him. The inversion is total, not marginal.

    Two honest answers instead:
      * some slots still have demand -> the SHALLOWEST of them. The player
        cannot actually fill it, so this is an approximation, but it is the
        conservative one: it is the highest replacement level still in play, so
        it yields the lowest value and keeps him below every genuine fit.
      * no slot has demand at all (pure bench rounds) -> the best player left.
        The best remaining player then scores 0 and everyone else is negative,
        so the order collapses to plain projected points. Which is correct:
        with every lineup full, a bench pick is just "take the best one left".
    """
    if replacement:
        return max(replacement.values())
    if not available:
        return 0.0
    return max(points[p.espn_player_id] for p in available)


def live_replacement_levels(
    available: Iterable[PlayerProjection],
    rosters: Iterable[Sequence[PlayerProjection]],
    settings: LeagueSettings,
    points: Mapping[int, float],
    eligibility: Eligibility = default_slot_eligibility,
) -> dict[str, float]:
    """Replacement level per slot, against the undrafted pool and open seats.

    Slots that are saturated are absent from the result rather than present
    with a stale number — compute_replacement_levels drops a slot whose demand
    has fallen to zero.
    """
    demand = remaining_demand(settings, consumed_slots(rosters, settings, eligibility))
    return compute_replacement_levels(available, settings, dict(points), eligibility, demand=demand)


def live_values(
    available: Iterable[PlayerProjection],
    rosters: Iterable[Sequence[PlayerProjection]],
    settings: LeagueSettings,
    points: Mapping[int, float],
    preseason: Mapping[int, tuple[float, float]],
    eligibility: Eligibility = default_slot_eligibility,
    basis: ReplacementBasis = ReplacementBasis.STARTER,
) -> list[LiveValue]:
    """Every undrafted player, re-valued against the live board, best first.

    `points` and `preseason` are both keyed by espn_player_id — the engine's
    identifier end to end. `preseason` carries each player's cached
    (replacement_points, value) so the shift can be reported alongside.

    Refuses a categories league for the same reason value_over_replacement
    does: VOR is points-league arithmetic, and a silent answer would be nonsense
    rather than an error.
    """
    if settings.scoring_format != "points":
        raise ValueError(f"live_values is for points leagues, got {settings.scoring_format!r}")

    available = list(available)
    rosters = list(rosters)

    if basis is ReplacementBasis.MARGINAL:
        # The scarcest-slot flaw is WORSE mid-draft, not better: as seats fill
        # they drop out one by one, so the survivors are claimed by an
        # ever-larger crowd. Asking the marginal question of the OPEN chairs
        # keeps the live board and the preseason board answering the same
        # question — a board that changes its definition of value the moment
        # you start drafting is not one you can practise against.
        demand = remaining_demand(settings, consumed_slots(rosters, settings, eligibility))
        per_player = marginal_replacements(available, settings, points, eligibility, demand=demand)
        results = []
        for player in available:
            slot, repl = per_player[player.espn_player_id]
            pts = points[player.espn_player_id]
            was_repl, was_value = preseason.get(player.espn_player_id, (0.0, 0.0))
            results.append(
                LiveValue(
                    value=PlayerValue(player, pts, repl, pts - repl, slot),
                    preseason_replacement=was_repl,
                    preseason_value=was_value,
                )
            )
        results.sort(key=lambda r: (-r.value.value, r.value.player.espn_player_id))
        return results

    replacement = live_replacement_levels(available, rosters, settings, points, eligibility)
    bench = bench_replacement(available, replacement, points)

    results: list[LiveValue] = []
    for player in available:
        fits = [s for s in replacement if eligibility(s, player.positions)]
        if fits:
            slot = min(fits, key=lambda s: replacement[s])
            repl = replacement[slot]
        else:
            slot, repl = BENCH_SLOT, bench

        pts = points[player.espn_player_id]
        was_repl, was_value = preseason.get(player.espn_player_id, (0.0, 0.0))
        results.append(
            LiveValue(
                value=PlayerValue(player, pts, repl, pts - repl, slot),
                preseason_replacement=was_repl,
                preseason_value=was_value,
            )
        )

    # espn_player_id settles ties, matching how the API orders equal valuations
    # — without it two identically projected players could swap places between
    # requests and the board would look like it was reshuffling itself.
    results.sort(key=lambda r: (-r.value.value, r.value.player.espn_player_id))
    return results


# --------------------------------------------------------------------------- #
# Auto-drafting the other nine teams
# --------------------------------------------------------------------------- #
#
# "Take the highest live value" is the right core and, on its own, not a draft.
# Two things have to sit on top of it or the simulation misleads you:
#
#   ROSTER NEED. Live value is a LEAGUE-wide number: it asks what a player is
#   worth against everyone's demand, not against one team's empty seats. A bot
#   running on value alone happily drafts a fifth centre in round 6 because the
#   centre is, league-wide, the best player left. Real opponents fill a lineup
#   first, and the runs that make a draft feel like a draft come from that.
#
#   DISAGREEMENT. Nine bots taking the argmax of one number produce the same
#   draft every single time, and a simulator you can only run once tells you
#   nothing you could not read off the board. Real drafters disagree, reach,
#   and take their guy a round early — so the bots sample rather than maximize.
#
# Both are deliberately crude. The point is a believable board to practise
# against, not a model of your leaguemates.


@dataclass(frozen=True)
class SimulatedPick:
    """One auto-made pick, ready to be persisted by the caller."""

    pick_number: int
    team_slot: int
    espn_player_id: int


def open_slots(
    roster: Iterable[PlayerProjection],
    settings: LeagueSettings,
    eligibility: Eligibility = default_slot_eligibility,
) -> set[str]:
    """Starting slots this team has not filled yet."""
    return {
        seat.slot
        for seat in assign_roster(roster, settings, eligibility).seats
        if seat.player is None
    }


def _bot_pick(
    ranked: Sequence[LiveValue],
    roster: Sequence[PlayerProjection],
    settings: LeagueSettings,
    rng: random.Random,
    reach: int,
    eligibility: Eligibility = default_slot_eligibility,
) -> PlayerProjection:
    """Which player one simulated team takes, given the board.

    Lineup before depth: if anyone left fits a seat this team still has open,
    the choice is made among those; once the starters are full every remaining
    pick is a bench pick and best-available is exactly right.

    `reach` is how far down that shortlist the team is willing to go. 1 is a
    pure value-maximizer, and the weights fall off linearly so the best player
    is still the most likely — a team that reaches three deep takes its top
    choice half the time, not a third of the time.
    """
    needed = open_slots(roster, settings, eligibility)
    fits = [c for c in ranked if any(eligibility(s, c.value.player.positions) for s in needed)]
    shortlist = (fits or list(ranked))[: max(1, reach)]
    weights = [len(shortlist) - i for i in range(len(shortlist))]
    return rng.choices(shortlist, weights=weights, k=1)[0].value.player


def autodraft(
    upcoming: Sequence[tuple[int, int, bool]],
    rosters: Mapping[int, Sequence[PlayerProjection]],
    available: Iterable[PlayerProjection],
    settings: LeagueSettings,
    points: Mapping[int, float],
    preseason: Mapping[int, tuple[float, float]],
    rng: random.Random,
    reach: int = 3,
    stop_at_my_pick: bool = True,
    basis: ReplacementBasis = ReplacementBasis.STARTER,
) -> list[SimulatedPick]:
    """Run the other teams' picks until it is your turn again.

    `upcoming` is the unfilled slots in pick order as (pick_number, team_slot,
    is_mine). Pure: it mutates nothing the caller owns and performs no I/O, so
    the whole simulation is checkable against a seeded RNG with no database.

    The board is re-valued after EVERY pick rather than once at the start, and
    that is the expensive part on purpose. Valuing once would miss the thing
    the simulation exists to show — a slot draining mid-round and lifting
    everyone left in it.
    """
    board = {slot: list(players) for slot, players in rosters.items()}
    pool = list(available)
    made: list[SimulatedPick] = []

    for pick_number, team_slot, is_mine in upcoming:
        if is_mine and stop_at_my_pick:
            break
        if not pool:
            break

        ranked = live_values(pool, list(board.values()), settings, points, preseason, basis=basis)
        player = _bot_pick(ranked, board.setdefault(team_slot, []), settings, rng, reach)

        board[team_slot].append(player)
        pool = [p for p in pool if p.espn_player_id != player.espn_player_id]
        made.append(
            SimulatedPick(
                pick_number=pick_number,
                team_slot=team_slot,
                espn_player_id=player.espn_player_id,
            )
        )

    return made


# --------------------------------------------------------------------------- #
# Step 2: what a player is worth TO YOUR ROSTER
# --------------------------------------------------------------------------- #
#
# live_values answers a league question: what is this player worth against
# everyone's demand. It is the same number for all ten teams, so it cannot say
# that your third centre is worth less to YOU than to the manager with none.
#
#     marginal(p) = best lineup with p  -  best lineup without p
#
# WEIGHTED BY LIVE VALUE, NOT BY PROJECTED POINTS, and that choice is the whole
# design. Weight the lineup by raw points and an empty roster makes every
# player's marginal equal their projection, so round one reverts to "most
# projected points" and throws away every bit of scarcity the engine computed
# — Jokic would drop behind SGA again. Weighted by value the arithmetic lines
# up instead: on an empty roster marginal(p) IS p's VOR, so early picks rank
# exactly as they do today, and the two only diverge as your seats fill. That
# is the correct shape for this feature. It should change nothing in round one
# and a great deal in round nine.


def _valuation_of(
    player: PlayerProjection,
    replacement: Mapping[str, float],
    bench: float,
    points: Mapping[int, float],
    eligibility: Eligibility,
) -> PlayerValue:
    """One player's live value against a given set of replacement levels.

    Factored out of live_values so a DRAFTED player can be valued on the same
    basis as an available one. The lineup maths has to weigh the player
    already in a seat against the candidate for it, and comparing a VOR to a
    raw projection would make every incumbent look replaceable.
    """
    fits = [s for s in replacement if eligibility(s, player.positions)]
    if fits:
        slot = min(fits, key=lambda s: replacement[s])
        repl = replacement[slot]
    else:
        slot, repl = BENCH_SLOT, bench

    pts = points[player.espn_player_id]
    return PlayerValue(player, pts, repl, pts - repl, slot)


def seat_slots(settings: LeagueSettings) -> list[str]:
    """Starting slots expanded one entry per seat: PF/C:2 becomes two seats."""
    return [slot for slot, count in starting_slots(settings).items() for _ in range(count)]


# Above this many seats the bitmask below stops being free. No real NBA league
# comes close; the guard exists so a nonsense roster_slots degrades instead of
# hanging the request.
MAX_SEATS_FOR_EXACT_LINEUP = 16


def lineup_values_by_mask(
    weighted: Sequence[tuple[PlayerProjection, float]],
    seats: Sequence[str],
    eligibility: Eligibility = default_slot_eligibility,
) -> list[float]:
    """Best achievable lineup value for every subset of seats.

    Returns a table indexed by seat bitmask: entry `m` is the most value this
    roster can put into exactly the seats in `m` (or any subset of them).

    Exact, not greedy, and that matters here. assign_roster is
    most-constrained-first, which maximizes SEATS FILLED — the right objective
    for counting league-wide demand and the wrong one for points. With PG:1 and
    UTIL:1 and a roster of two good guards plus a weak centre, filling the most
    seats puts the centre in UTIL; maximizing value benches him and starts both
    guards. The two answers differ, and marginal value needs the second.

    One pass over this table then answers every question the caller has: the
    best lineup overall is its maximum, and the best lineup that leaves seat s
    free is the maximum over masks without that bit.
    """
    n = len(seats)
    size = 1 << n
    best = [float("-inf")] * size
    best[0] = 0.0
    if n == 0:
        return best

    for player, weight in weighted:
        eligible = [i for i in range(n) if eligibility(seats[i], player.positions)]
        if not eligible:
            continue
        # Snapshot, so each player is placed at most once.
        previous = best[:]
        for mask in range(size):
            base = previous[mask]
            if base == float("-inf"):
                continue
            for i in eligible:
                bit = 1 << i
                if mask & bit:
                    continue
                best[mask | bit] = max(best[mask | bit], base + weight)
    return best


def marginal_values(
    candidates: Iterable[LiveValue],
    roster: Iterable[PlayerProjection],
    settings: LeagueSettings,
    replacement: Mapping[str, float],
    bench: float,
    points: Mapping[int, float],
    eligibility: Eligibility = default_slot_eligibility,
) -> dict[int, float]:
    """How much each candidate would add to YOUR starting lineup.

    Never negative: the worst a player can do is ride your bench, which is
    worth zero to the lineup rather than a penalty. A 0.0 here is the useful
    signal — it means every seat this player fits is already held by someone
    better, so drafting them buys depth and nothing else.
    """
    seats = seat_slots(settings)
    weighted = [
        (p, _valuation_of(p, replacement, bench, points, eligibility).value) for p in roster
    ]

    if len(seats) > MAX_SEATS_FOR_EXACT_LINEUP:
        # Degrade to "does it fill an open seat", rather than hang.
        open_ = open_slots([p for p, _ in weighted], settings, eligibility)
        return {
            c.value.player.espn_player_id: (
                c.value.value
                if any(eligibility(s, c.value.player.positions) for s in open_)
                else 0.0
            )
            for c in candidates
        }

    table = lineup_values_by_mask(weighted, seats, eligibility)
    full = max(table)

    # Best lineup the roster can manage while leaving each seat free for the
    # candidate. Computed once for all seats, then read per candidate.
    without_seat = []
    for i in range(len(seats)):
        bit = 1 << i
        without_seat.append(max(v for mask, v in enumerate(table) if not mask & bit))

    out: dict[int, float] = {}
    for candidate in candidates:
        player = candidate.value.player
        best_with = max(
            (
                candidate.value.value + without_seat[i]
                for i in range(len(seats))
                if eligibility(seats[i], player.positions)
            ),
            default=float("-inf"),
        )
        out[player.espn_player_id] = max(0.0, best_with - full)
    return out


# --------------------------------------------------------------------------- #
# Step 3: value over next available
# --------------------------------------------------------------------------- #
#
# Steps 1 and 2 answer "who is worth most to me right now". Neither answers the
# question a draft actually poses, which is a question about TIMING: this pick
# is not a choice between players, it is a choice between taking a player now
# and taking whoever is left when the wheel comes back.
#
#     vona(p) = marginal(p) - E[ best marginal still there at my next pick ]
#
# So a player worth 80 whom nobody else can use is worth less of THIS pick than
# a player worth 70 who will certainly be gone — because the 80 will still be
# sitting there in nine picks' time. That is the whole content of "don't reach,
# and don't wait too long", expressed as one number.

# How sharply the simulated field agrees. Lower = more consensus = the top of
# the board disappears faster. This is deliberately softer than the autodraft's
# `reach`: predicting a field of nine managers needs a wider distribution than
# driving one bot, because the error that matters here is being too certain
# somebody survives.
DEFAULT_FIELD_SPREAD = 4.0


def picks_until_next(
    upcoming: Sequence[tuple[int, int, bool]],
) -> list[int]:
    """The team slots that pick between your next pick and the one after it.

    `upcoming` is the unfilled board in pick order as (pick_number, team_slot,
    is_mine) — the same shape autodraft takes. Returns [] when you are on the
    clock for the last time, which correctly means nothing can be taken from
    you and every candidate survives.
    """
    mine = [i for i, (_, _, is_mine) in enumerate(upcoming) if is_mine]
    if len(mine) < 2:
        # No pick of yours after this one, so nobody can take anything FROM
        # you. Teams still drafting behind you are irrelevant, not a threat.
        return []
    return [upcoming[i][1] for i in range(mine[0] + 1, mine[1])]


def survival_probabilities(
    ranked: Sequence[LiveValue],
    opponents: Sequence[Sequence[PlayerProjection]],
    settings: LeagueSettings,
    spread: float = DEFAULT_FIELD_SPREAD,
    eligibility: Eligibility = default_slot_eligibility,
) -> dict[int, float]:
    """P(each candidate is still on the board when you pick again).

    One entry in `opponents` per intervening pick, holding that team's current
    roster — so a team picking twice at the turn is listed twice, and each is
    weighted by its OWN open seats. That is what makes this an opponent model
    rather than a countdown: nine teams that all still need a centre are a
    very different threat to your centre than nine teams that do not.

    Each team's interest decays exponentially with a player's rank on that
    team's own board, and the picks are then treated as independent.
    Independence is the approximation: in truth an early pick removes a
    competitor and reshuffles the later boards. It errs toward saying players
    survive slightly more often than they do, and the alternative — simulating
    the intervening picks repeatedly — costs seconds per request rather than
    milliseconds.
    """
    survival = {c.value.player.espn_player_id: 1.0 for c in ranked}
    if not ranked:
        return survival

    for roster in opponents:
        needed = open_slots(roster, settings, eligibility)
        pool = [c for c in ranked if any(eligibility(s, c.value.player.positions) for s in needed)]
        if not pool:
            pool = list(ranked)

        weights = [math.exp(-rank / spread) for rank in range(len(pool))]
        total = sum(weights)
        if total <= 0:
            continue
        for candidate, weight in zip(pool, weights, strict=True):
            taken = weight / total
            survival[candidate.value.player.espn_player_id] *= 1.0 - taken

    return survival


def expected_next_best(
    ranked: Sequence[LiveValue],
    marginal: Mapping[int, float],
    survival: Mapping[int, float],
) -> dict[int, float]:
    """For each candidate, what you could expect NEXT turn having taken them.

    E[best survivor] under independence has a closed form: walk the board best
    first, and the chance that candidate i is the best one left is the chance
    everyone above them is gone times the chance they are not. Prefix and
    suffix passes make the whole thing linear, including the per-candidate
    version that excludes the player you are considering taking — who is by
    definition unavailable to your future self.
    """
    order = sorted(
        ranked,
        key=lambda c: (
            -marginal.get(c.value.player.espn_player_id, 0.0),
            c.value.player.espn_player_id,
        ),
    )
    ids = [c.value.player.espn_player_id for c in order]
    values = [marginal.get(i, 0.0) for i in ids]
    lives = [survival.get(i, 1.0) for i in ids]
    n = len(order)

    # suffix[i] = E[best survivor among candidates i..n-1]
    suffix = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = values[i] * lives[i] + (1.0 - lives[i]) * suffix[i + 1]

    out: dict[int, float] = {}
    gone = 1.0  # P(everyone before i is already taken)
    head = 0.0  # E[best survivor among candidates before i]
    for i in range(n):
        out[ids[i]] = head + gone * suffix[i + 1]
        head += gone * lives[i] * values[i]
        gone *= 1.0 - lives[i]

    return out


def pick_scores(
    marginal: Mapping[int, float],
    expected_next: Mapping[int, float],
) -> dict[int, float]:
    """Rank this pick by what the NEXT TWO picks are worth together.

        score(p) = marginal(p) + E[best survivor at my next pick, given p is gone]

    Note the plus. The design this implements wrote the objective as a
    DIFFERENCE, `marginal(p) - E[next]`, which is how "value over next
    available" is usually written — and as a ranking over whole players it is
    simply wrong. Take a board with an 80 nobody else can use and a 70 that is
    certain to go:

        take the 80, the 70 is gone next turn   -> 80 + 0  =  80
        take the 70, the 80 is still there      -> 70 + 80 = 150

    The sum picks the 70, correctly. The difference scores the 80 at 80 and
    the 70 at -10 and picks the 80, which is exactly backwards: it throws away
    a player who was never at risk. The conventional formula is a comparison
    BETWEEN POSITIONS — how far a position drops off if you wait — and does
    not survive being used as a total order over players.

    What the difference is good for is explaining the choice, so
    `expected_next` is returned alongside rather than folded away: "take him
    now, because if you wait the best you should expect is 75.6".
    """
    return {pid: value + expected_next.get(pid, 0.0) for pid, value in marginal.items()}
