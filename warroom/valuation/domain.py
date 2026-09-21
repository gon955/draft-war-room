"""Plain domain objects for the valuation engine.

These are framework-free on purpose: the engine and its tests depend only on
these, not on SQLAlchemy, FastAPI, or ESPN. The persistence layer maps its rows
to/from these; the ESPN adapter produces PlayerProjection / LeagueSettings.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
