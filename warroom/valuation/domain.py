"""Plain domain objects for the valuation engine.

These are framework-free on purpose: the engine and its tests depend only on
these, not on SQLAlchemy, FastAPI, or ESPN. The persistence layer maps its rows
to/from these; the ESPN adapter produces PlayerProjection / LeagueSettings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# One completed season's ACTUAL totals, keyed by lowercase stat code like
# PlayerProjection.stats ("gp", "min", "oreb", "dreb", "dd", ...). Raw lines
# rather than anything derived from them, so a model fitted on history can be
# refitted without refetching it.
SeasonLine = dict[str, float]

# A player's completed seasons before the one being valued, keyed by season.
# The three states are distinct and every consumer needs to tell them apart:
#
#   key absent      that season was not fetched (the league did not exist yet,
#                   or history has never been synced) — nothing is known
#   value None      fetched, and ESPN had no record of this player: not in the
#                   NBA that season. How a rookie is identified.
#   value {}        ESPN lists the player but has no stat line: a whole season
#                   lost to injury (Haliburton, 2026) — which an availability
#                   model must not mistake for a rookie — but also a season
#                   played abroad while still listed (Lyles, 2026). A zero-
#                   games season is not proof of injury on its own.
PlayerHistory = dict[int, SeasonLine | None]


@dataclass(frozen=True)
class PlayerProjection:
    """A player's projected stats for a season, from the data source.

    `stats` keys are lowercase stat codes (e.g. "pts", "reb", "ast", "stl",
    "blk", "tpm", "to", "fgm", "fga", "ftm", "fta"). Only the stats your league
    scores need weights; missing stats are treated as 0 at valuation time.
    """

    espn_player_id: int
    name: str
    positions: tuple[str, ...]
    stats: dict[str, float]
    pro_team: str = ""
    # ESPN's projected fantasy total for this player (their `appliedTotal`):
    # this league's weights applied to the SAME projected line as `stats`, with
    # every stat ESPN does not project — oreb, dreb, dd, td — scored as zero.
    # Verified to 0.1% on all 1,085 projected player-seasons 2024-2026. So it
    # is not a second opinion on the player; it is ESPN's number with the very
    # hole stats.py exists to fill, and it undervalues rebounders and
    # double-double producers accordingly. Kept because it is what the draft
    # room SEES, which is what the market survival model and the ESPN bots
    # need. Optional because a fake source, a hand-built projection and a pool
    # synced before this field existed all lack it.
    espn_points: float | None = None
    # ESPN's injury flag: "ACTIVE", "DAY_TO_DAY" or "OUT" (None when the feed
    # says nothing). Carried because it is decision-relevant at the draft and
    # arrives free in the same payload — a player who is OUT is worth knowing
    # about before you spend a pick, whatever their projection says.
    #
    # It deliberately does NOT feed the valuation, though not for the reason
    # this comment once gave. It is not a double count: ESPN's projected games
    # still run ~12% above games played (valuation/availability.py). It is
    # that ESPN serves only a player's CURRENT flag, so there is no record of
    # what the flag said before any past season, and nothing to fit a
    # discount against. An unfitted guess would move the board on a number
    # nobody has checked.
    injury_status: str | None = None
    # Prior seasons' actuals, fetched at sync so valuation never has to go
    # back to ESPN. Empty when history has not been synced, which every
    # consumer must treat as "unknown", never as "rookie" — see PlayerHistory.
    history: PlayerHistory = field(default_factory=dict)


@dataclass(frozen=True)
class LeagueSettings:
    """League configuration, read from ESPN at sync time and stored on the league.

    `roster_slots` counts STARTING slots only (bench/IR excluded), e.g.
    {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UTIL": 3}.
    `point_weights` is the league's own per-stat scoring, e.g.
    {"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0, "to": -1.0}.
    `roster_size` is the TOTAL roster — starters plus bench, IR excluded — so
    it is not derivable from `roster_slots`, which drops bench seats because
    they create no starter demand. Defaults to 0 so a settings object built
    by hand (the engine's tests, the fake data source) need not supply it.
    """

    scoring_format: str  # "points" | "categories"
    num_teams: int
    roster_slots: dict[str, int]
    point_weights: dict[str, float] = field(default_factory=dict)
    categories: tuple[str, ...] | None = None
    roster_size: int = 0
    # What a value is measured against; see engine.ReplacementBasis. A
    # plain string so this module stays free of the engine's imports, the
    # same reason scoring_format is one.
    replacement_basis: str = "starter"


@dataclass(frozen=True)
class PlayerValue:
    """One player's valuation result."""

    player: PlayerProjection
    projected_points: float
    replacement_points: float
    value: float  # projected_points - replacement_points
    assigned_slot: str  # the slot the player is credited at (their scarcest)
