"""Points-league player valuation: value over replacement (VOR).

Ranking by raw projected points ignores position scarcity; VOR fixes that by
measuring each player against the last startable player at their position across
the whole league. A scarce position (steep drop-off) yields a low replacement
level, so its top players get more value — which is the entire point of the tool
over ESPN's default rank.

Definitions used here (documented so they can be defended in review):
  * projected_points(p) = sum(stat * league_weight) over the league's weights.
  * replacement level at a slot = the projected points of the LAST starter at
    that slot across the league, i.e. the (num_teams * slot_count)-th best
    eligible player. So the marginal starter has value 0; everyone above is
    positive, everyone below is negative.
  * a multi-eligible player is credited at their SCARCEST eligible slot — the one
    with the lowest replacement level, which maximizes their value over
    replacement (you'd play them where they help most).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum

from .domain import LeagueSettings, PlayerProjection, PlayerValue

# Slots that don't create starter demand and so don't set a replacement level.
NON_STARTING_SLOTS = {"BE", "BN", "BENCH", "IR", "IL", "NA"}

# What a player who wins no starting chair is credited at.
BENCH_SLOT = "BENCH"


class ReplacementBasis(str, Enum):
    """Which "freely available player" a value is measured against.

    STARTER is the classic VOR baseline and what this engine shipped with: the
    last player who would START at a slot across the league, i.e. the
    (num_teams * slot_count)-th best eligible player. It answers "would this
    player improve a starting lineup?"

    WAIVER answers the question a DRAFT actually poses: "is this player worth a
    roster spot, against what I could pick up for nothing?" The alternative to
    drafting someone is not the marginal starter — that player is rostered by
    somebody — it is the best player nobody drafted at all.

    The two differ by more than a constant. A 10-team league with 13 roster
    spots and 8 starting slots rosters 130 players but starts only 80, so the
    starter baseline sits at about rank 80 and the waiver baseline at about
    131. How far apart those are depends on how steeply a position's talent
    falls away between them, which is different for every slot — so switching
    basis reorders the board rather than just rescaling it.

    Not modelled here: the in-season streaming premium. You do not hold one
    waiver player all year, you take the best available every week, and the
    best available fluctuates upward with injuries and role changes. That
    makes the true baseline somewhat higher than WAIVER computes. Quantifying
    it needs in-season transaction history this app does not collect, so it is
    named and left out rather than guessed at.
    """

    STARTER = "starter"
    WAIVER = "waiver"
    MARGINAL = "marginal"


def default_slot_eligibility(slot: str, positions: tuple[str, ...]) -> bool:
    """Whether a player with `positions` can fill `slot` in a standard NBA league.

    UTIL/UT: anyone. G: a guard (PG/SG/G). F: a forward (SF/PF/F). A combo slot
    joins alternatives with "/" and means EITHER side, so ESPN's SG/SF, G/F,
    PF/C and F/C each accept a player matching any one part. Otherwise the slot
    is a specific position and the player must list it.

    Combo slots must be split rather than matched literally: "PF/C" is never an
    element of `positions`, so a literal check finds nobody eligible, the slot
    creates no starter demand, and every replacement level is computed against a
    shallower league than the real one.
    """
    slot = slot.upper()
    pos = {p.upper() for p in positions}
    if slot in ("UTIL", "UT"):
        return True
    return any(_matches_slot_part(part, pos) for part in slot.split("/"))


def _matches_slot_part(part: str, pos: set[str]) -> bool:
    """One side of a slot: a flex letter (G/F) or a literal position."""
    if part == "G":
        return bool(pos & {"PG", "SG", "G"})
    if part == "F":
        return bool(pos & {"SF", "PF", "F"})
    return part in pos


def starting_slots(settings: LeagueSettings) -> dict[str, int]:
    """The slots that create starter demand, and how many seats each has per team.

    Bench and IR are dropped: they take a roster spot but no lineup spot, so
    they set no replacement level. Extracted because draft.py has to walk the
    same set to decide which slots a drafted player consumes, and two copies of
    "which slots count" would drift.
    """
    return {
        slot: count
        for slot, count in settings.roster_slots.items()
        if slot.upper() not in NON_STARTING_SLOTS and count > 0
    }


def project_points(player: PlayerProjection, weights: dict[str, float]) -> float:
    """Linear fantasy-point projection: dot product of stats and league weights."""
    return sum(player.stats.get(stat, 0.0) * weight for stat, weight in weights.items())


def compute_replacement_levels(
    players: Iterable[PlayerProjection],
    settings: LeagueSettings,
    points: dict[int, float],
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
    demand: Mapping[str, int] | None = None,
) -> dict[str, float]:
    """Replacement points for each starting slot in the roster.

    `points` maps espn_player_id -> projected points (from project_points).

    `demand` overrides how many starters the league still needs at each slot.
    Default None means the preseason question: every seat is open, so demand is
    num_teams * count. draft.py passes the seats left UNFILLED mid-draft, which
    is the same definition of a replacement level asked of a smaller league —
    hence one implementation rather than two. A slot whose demand has fallen to
    zero is omitted from the result: it is saturated, it can absorb nobody
    else, and so it no longer sets a level for anyone.
    """
    players = list(players)
    replacement: dict[str, float] = {}
    for slot, count in starting_slots(settings).items():
        n_starters = settings.num_teams * count if demand is None else demand.get(slot, 0)
        if n_starters <= 0:
            continue
        eligible = [p for p in players if eligibility(slot, p.positions)]
        if not eligible:
            replacement[slot] = 0.0
            continue
        eligible.sort(key=lambda p: points[p.espn_player_id], reverse=True)
        # Last starter across the league; clamp if the position can't be filled.
        idx = min(n_starters - 1, len(eligible) - 1)
        replacement[slot] = points[eligible[idx].espn_player_id]
    return replacement


def value_over_replacement(
    players: Iterable[PlayerProjection],
    settings: LeagueSettings,
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
    basis: ReplacementBasis = ReplacementBasis.STARTER,
) -> list[PlayerValue]:
    """Rank a player pool by value over replacement, descending.

    `basis` picks what "replacement" means — see ReplacementBasis. STARTER is
    the default so an existing league's numbers do not move unless somebody
    asks them to.

    Only valid for points leagues; raises if handed a categories league so a
    mis-wired call fails loudly rather than returning silent nonsense.
    """
    if settings.scoring_format != "points":
        raise ValueError(
            f"value_over_replacement is for points leagues, got {settings.scoring_format!r}"
        )

    players = list(players)
    weights = settings.point_weights
    points = {p.espn_player_id: project_points(p, weights) for p in players}
    if basis is ReplacementBasis.MARGINAL:
        # Per-player rather than per-slot: the whole point is that two players
        # in the same slot can be replaceable to different degrees.
        per_player = marginal_replacements(players, settings, points, eligibility)
        results = [
            PlayerValue(
                p,
                points[p.espn_player_id],
                per_player[p.espn_player_id][1],
                points[p.espn_player_id] - per_player[p.espn_player_id][1],
                per_player[p.espn_player_id][0],
            )
            for p in players
        ]
        results.sort(key=lambda v: (-v.value, v.player.espn_player_id))
        return results

    replacement = (
        waiver_replacement_levels(players, settings, points, eligibility)
        if basis is ReplacementBasis.WAIVER
        else compute_replacement_levels(players, settings, points, eligibility)
    )

    results: list[PlayerValue] = []
    for p in players:
        eligible_slots = [s for s in replacement if eligibility(s, p.positions)]
        if eligible_slots:
            best_slot = min(eligible_slots, key=lambda s: replacement[s])
            repl = replacement[best_slot]
        else:
            best_slot, repl = "NONE", 0.0
        pts = points[p.espn_player_id]
        results.append(PlayerValue(p, pts, repl, pts - repl, best_slot))

    results.sort(key=lambda v: v.value, reverse=True)
    return results


# The drafted pool depends on the values, and the values depend on the
# replacement levels, which depend on the drafted pool. Iterating settles it;
# in practice it converges in two or three passes, and this is the guard
# against a pathological pool oscillating forever.
MAX_WAIVER_ITERATIONS = 10


def _values_against(
    players: Sequence[PlayerProjection],
    replacement: Mapping[str, float],
    points: Mapping[int, float],
    eligibility: Callable[[str, tuple[str, ...]], bool],
) -> dict[int, float]:
    """Each player's value given a set of levels, crediting their scarcest slot."""
    out: dict[int, float] = {}
    for p in players:
        fits = [replacement[s] for s in replacement if eligibility(s, p.positions)]
        out[p.espn_player_id] = points[p.espn_player_id] - (min(fits) if fits else 0.0)
    return out


def waiver_replacement_levels(
    players: Iterable[PlayerProjection],
    settings: LeagueSettings,
    points: Mapping[int, float],
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
) -> dict[str, float]:
    """Replacement points per slot, measured at the best UNDRAFTED player.

    `num_teams * roster_size` players come off the board, so the best player
    left is the one anybody can have for free. Which players those are depends
    on how they are valued, and how they are valued depends on these levels —
    so this iterates to a fixed point, seeded from the starter basis.

    Requires roster_size: without it there is no draft to be outside of, and
    the function falls back to the starter basis rather than inventing a pool
    size.
    """
    players = list(players)
    slots = starting_slots(settings)
    seed = compute_replacement_levels(players, settings, points, eligibility)
    if settings.roster_size < 1 or not players or not slots:
        return seed

    drafted_count = min(settings.num_teams * settings.roster_size, len(players))
    levels = seed

    for _ in range(MAX_WAIVER_ITERATIONS):
        values = _values_against(players, levels, points, eligibility)
        ranked = sorted(players, key=lambda p: (-values[p.espn_player_id], p.espn_player_id))
        undrafted = ranked[drafted_count:]

        nxt: dict[str, float] = {}
        for slot in slots:
            free = [p for p in undrafted if eligibility(slot, p.positions)]
            if free:
                nxt[slot] = max(points[p.espn_player_id] for p in free)
                continue
            # Everyone eligible here is rostered, so there is nothing to pick
            # up. The honest floor is the worst player at the slot rather than
            # 0.0, which would hand every eligible player their whole
            # projection as value.
            eligible = [p for p in players if eligibility(slot, p.positions)]
            nxt[slot] = min((points[p.espn_player_id] for p in eligible), default=0.0)

        if nxt == levels:
            break
        levels = nxt

    return levels


# --------------------------------------------------------------------------- #
# Marginal (shadow-price) replacement
# --------------------------------------------------------------------------- #
#
# The scarcest-slot rule lets EVERY eligible player claim a slot, however few
# chairs it has. Measured on a real 10-team league with one PG seat per team:
# 111 players were valued against PG's replacement level and 10 can sit there,
# while UT and G/F had 30 chairs between them and valued nobody at all.
#
# Cascading the slots (scarcest takes its ten, next takes from the rest) fixes
# the seat count but not the problem: four defensible processing orders gave
# four different boards, shifting the top 50 by a mean of 5 to 16 places. That
# swaps a stable wrong answer for an arbitrary one.
#
# So value is defined marginally instead, which needs no ordering at all:
#
#     value(p) = p's points  -  the points of whoever takes p's seat
#
# Seat the league optimally, remove one player, and see who comes off the
# bench through the eligibility graph. A player nobody can replace is scarce
# by construction; a player whose seat can be filled by any of forty others is
# not. That is what positional scarcity MEANS, rather than a proxy for it.
#
# Two consequences worth stating, because both surprise people:
#
#   * Nobody is valued against a chair that does not exist. Only the players
#     who would actually be seated get a starter's baseline, and each slot's
#     baseline is the worst player the seating actually put there.
#
#   * Positional scarcity survives but shrinks. On a real flex-heavy league
#     the six slots' levels span about 90 points where the scarcest-slot rule
#     spanned 315. Most of that difference was the defect: measuring PG at
#     the 10th best of 111 claimants rather than at the worst player actually
#     starting there overstated how scarce the position was.


def _seat_player(
    player: PlayerProjection,
    capacity: Mapping[str, int],
    occupants: dict[str, list[PlayerProjection]],
    eligibility: Callable[[str, tuple[str, ...]], bool],
    visited: set[str],
) -> bool:
    """Kuhn's augmenting path: seat `player`, reshuffling incumbents if need be."""
    for slot, cap in sorted(capacity.items(), key=lambda kv: (kv[1], kv[0])):
        if slot in visited or not eligibility(slot, player.positions):
            continue
        visited.add(slot)
        if len(occupants[slot]) < cap:
            occupants[slot].append(player)
            return True
        # Full: can any incumbent move elsewhere and free this chair?
        for index, incumbent in enumerate(occupants[slot]):
            if _seat_player(incumbent, capacity, occupants, eligibility, visited):
                occupants[slot][index] = player
                return True
    return False


def optimal_seating(
    players: Sequence[PlayerProjection],
    points: Mapping[int, float],
    capacity: Mapping[str, int],
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
) -> dict[int, str]:
    """Fill every starting chair in the league with the best players who fit.

    Best-first with augmenting paths, and that IS the optimum rather than a
    heuristic: a player contributes the same points wherever they sit, so the
    seatable sets form a transversal matroid, and greedy by weight is optimal
    on a matroid. Which is precisely what the cascade could not promise —
    no processing order to choose, no arbitrary answer.
    """
    occupants: dict[str, list[PlayerProjection]] = {slot: [] for slot in capacity}
    for player in sorted(players, key=lambda p: (-points[p.espn_player_id], p.espn_player_id)):
        _seat_player(player, capacity, occupants, eligibility, set())
    return {p.espn_player_id: slot for slot, seated in occupants.items() for p in seated}


def marginal_replacements(
    players: Iterable[PlayerProjection],
    settings: LeagueSettings,
    points: Mapping[int, float],
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
    demand: Mapping[str, int] | None = None,
) -> dict[int, tuple[str, float]]:
    """Per-player (slot, replacement points) from the optimal league seating.

    `demand` overrides the chair count per slot, exactly as it does for
    compute_replacement_levels — which is what lets the draft-time engine ask
    the same question of the seats that are still OPEN.

    A player who wins no chair is measured against the best other unseated
    player who shares a slot with them, so the best man on the bench lands
    near zero and the rest fall away beneath him.

    The slot in the returned pair is the chair the seating gave the player,
    and it DOES pick their baseline. An optimal seating does not pin who sits
    where — an augmenting path can move an incumbent to an equally good chair
    — so this is deterministic rather than canonical. The exposure is bounded
    by how far apart the slots' levels are, which on a real flex-heavy league
    is under a hundred points against values in the thousands.
    """
    players = list(players)
    capacity = {
        slot: (settings.num_teams * count if demand is None else demand.get(slot, 0))
        for slot, count in starting_slots(settings).items()
    }
    capacity = {slot: cap for slot, cap in capacity.items() if cap > 0}
    if not capacity or not players:
        return {p.espn_player_id: (BENCH_SLOT, 0.0) for p in players}

    seated = optimal_seating(players, points, capacity, eligibility)
    bench = sorted(
        (p for p in players if p.espn_player_id not in seated),
        key=lambda p: (-points[p.espn_player_id], p.espn_player_id),
    )
    bench_level = points[bench[0].espn_player_id] if bench else 0.0

    # The marginal STARTER at each slot: the worst player the optimal seating
    # actually put there.
    #
    # Not a shadow price. The first version of this asked "remove a player,
    # who comes off the bench" — and on a real league every one of the 80
    # starters came back with the SAME replacement, because flex chairs let
    # one bench player reach any vacancy through an alternating path. The
    # board collapsed to a ranking by projected points (mean shift 0.03 from
    # raw order) and every trace of positional scarcity went with it.
    #
    # The arithmetic was right and the model was wrong: that alternating path
    # needs OTHER MANAGERS to rearrange their lineups to backfill your
    # vacancy, and ten independent teams do not do that. Marginal analysis
    # over a whole league assumes a coordination that a draft does not have.
    # It is valid inside ONE roster, which is where it already lives, in
    # draft.marginal_values.
    #
    # So the baseline stays per slot, which is what carries scarcity — but it
    # is read off the optimal seating, so no slot can be claimed by more
    # players than it has chairs. That was the actual defect.
    per_slot: dict[str, float] = {}
    for player in players:
        slot = seated.get(player.espn_player_id)
        if slot is None:
            continue
        value = points[player.espn_player_id]
        if slot not in per_slot or value < per_slot[slot]:
            per_slot[slot] = value

    out: dict[int, tuple[str, float]] = {}
    for player in players:
        slot = seated.get(player.espn_player_id)
        if slot is not None:
            out[player.espn_player_id] = (slot, per_slot[slot])
            continue
        # Unseated: nobody is starting them, so they are measured against
        # the best player also left on the bench.
        out[player.espn_player_id] = (BENCH_SLOT, bench_level)
    return out
