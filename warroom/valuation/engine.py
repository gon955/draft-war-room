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

from collections.abc import Callable, Iterable

from .domain import LeagueSettings, PlayerProjection, PlayerValue

# Slots that don't create starter demand and so don't set a replacement level.
NON_STARTING_SLOTS = {"BE", "BN", "BENCH", "IR", "IL", "NA"}


def default_slot_eligibility(slot: str, positions: tuple[str, ...]) -> bool:
    """Whether a player with `positions` can fill `slot` in a standard NBA league.

    UTIL/UT: anyone. G: a guard (PG/SG/G). F: a forward (SF/PF/F). Otherwise the
    slot is a specific position and the player must list it.
    """
    slot = slot.upper()
    pos = {p.upper() for p in positions}
    if slot in ("UTIL", "UT"):
        return True
    if slot == "G":
        return bool(pos & {"PG", "SG", "G"})
    if slot == "F":
        return bool(pos & {"SF", "PF", "F"})
    return slot in pos


def project_points(player: PlayerProjection, weights: dict[str, float]) -> float:
    """Linear fantasy-point projection: dot product of stats and league weights."""
    return sum(player.stats.get(stat, 0.0) * weight for stat, weight in weights.items())


def compute_replacement_levels(
    players: Iterable[PlayerProjection],
    settings: LeagueSettings,
    points: dict[int, float],
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
) -> dict[str, float]:
    """Replacement points for each starting slot in the roster.

    `points` maps espn_player_id -> projected points (from project_points).
    """
    players = list(players)
    replacement: dict[str, float] = {}
    for slot, count in settings.roster_slots.items():
        if slot.upper() in NON_STARTING_SLOTS or count <= 0:
            continue
        eligible = [p for p in players if eligibility(slot, p.positions)]
        if not eligible:
            replacement[slot] = 0.0
            continue
        eligible.sort(key=lambda p: points[p.espn_player_id], reverse=True)
        n_starters = settings.num_teams * count
        # Last starter across the league; clamp if the position can't be filled.
        idx = min(n_starters - 1, len(eligible) - 1)
        replacement[slot] = points[eligible[idx].espn_player_id]
    return replacement


def value_over_replacement(
    players: Iterable[PlayerProjection],
    settings: LeagueSettings,
    eligibility: Callable[[str, tuple[str, ...]], bool] = default_slot_eligibility,
) -> list[PlayerValue]:
    """Rank a player pool by value over replacement, descending.

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
    replacement = compute_replacement_levels(players, settings, points, eligibility)

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
