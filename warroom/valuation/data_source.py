"""The seam between the app and ESPN.

Everything that reads ESPN goes through PlayerDataSource. Production wires an
EspnDataSource (wrapping the `espn-api` basketball League); tests and local dev
wire FakePlayerDataSource, so the suite never touches the network and CI stays
green regardless of ESPN's uptime or undocumented changes.

This module is the ONLY place that knows ESPN's vocabulary — lineupSlotCounts,
scoringItems, statId, eligibleSlots. Everything downstream sees LeagueSettings
and PlayerProjection. If ESPN changes their undocumented API, one file changes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .domain import LeagueSettings, PlayerProjection
from .engine import NON_STARTING_SLOTS

# The five real basketball positions. ESPN's eligibleSlots mixes these with flex
# slots (G, F), combo slots (SG/SF, PF/C) and non-playing slots (BE, IR, UT);
# the domain carries only real positions and lets the engine derive the rest.
REAL_POSITIONS = frozenset({"PG", "SG", "SF", "PF", "C"})

# How many free agents to pull. ESPN pages this; a points league needs the pool
# deep enough that every starting slot reaches its replacement level.
FREE_AGENT_POOL_SIZE = 400


class ProjectionsUnavailable(RuntimeError):
    """ESPN returned a player pool with no projections for the requested season.

    ESPN publishes next season's projections some time before the season opens;
    until then every split for that season comes back empty. A pool with no
    projections values every player at 0.0, which the engine will happily rank
    into meaningless order — so this is raised instead of returning it.
    """


@runtime_checkable
class PlayerDataSource(Protocol):
    def get_league_settings(self, espn_league_id: int, season: int) -> LeagueSettings: ...
    def get_player_pool(self, espn_league_id: int, season: int) -> list[PlayerProjection]: ...


class FakePlayerDataSource:
    """In-memory data source for tests and local development."""

    def __init__(self, settings: LeagueSettings, players: list[PlayerProjection]):
        self._settings = settings
        self._players = players

    def get_league_settings(self, espn_league_id: int, season: int) -> LeagueSettings:
        return self._settings

    def get_player_pool(self, espn_league_id: int, season: int) -> list[PlayerProjection]:
        return list(self._players)


# --------------------------------------------------------------------------- #
# Translation. These are pure functions over raw ESPN payloads so the mapping is
# unit-tested against fixtures without a network call; EspnDataSource below is
# then a thin wrapper whose only extra job is fetching.
# --------------------------------------------------------------------------- #
def scoring_format_from_type(scoring_type: str | None) -> str:
    """ESPN's scoringType -> the domain's scoring_format.

    ESPN spells it H2H_POINTS / TOTAL_POINTS for points leagues and
    H2H_CATEGORY / ROTO for category leagues.
    """
    return "points" if "POINT" in (scoring_type or "").upper() else "categories"


def starting_slots_from_raw(raw_settings: dict[str, Any]) -> dict[str, int]:
    """Starting lineup slots and their counts, bench/IR excluded.

    espn-api's basketball settings class does not parse lineupSlotCounts (only
    the football one does), so this reads the raw league JSON. Slot ids are
    resolved through POSITION_MAP, which is why combo labels like "PF/C" reach
    the engine intact — see engine.default_slot_eligibility.
    """
    from espn_api.basketball.constant import POSITION_MAP

    counts = raw_settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
    slots: dict[str, int] = {}
    for slot_id, count in counts.items():
        if count <= 0:
            continue
        label = POSITION_MAP.get(int(slot_id))
        # An unknown id, ESPN's blank slot, or bench/IR: no starter demand.
        if not label or label.upper() in NON_STARTING_SLOTS:
            continue
        slots[label] = count
    return slots


def point_weights_from_raw(raw_settings: dict[str, Any]) -> dict[str, float]:
    """The league's own per-stat point values, keyed by lowercase stat code.

    Read rather than hardcoded: ESPN points leagues let the commissioner set
    custom weights, and valuation is only correct for THIS league if it uses
    them (SPEC 2.2). Stats worth 0 are dropped so the result describes what the
    league actually scores — if FG%/FT% are unscored they simply don't appear.
    """
    from espn_api.basketball.constant import STATS_MAP

    weights: dict[str, float] = {}
    for item in raw_settings.get("scoringSettings", {}).get("scoringItems", []):
        code = STATS_MAP.get(str(item.get("statId")), "")
        points = float(item.get("points", 0.0))
        if not code or points == 0.0:
            continue
        weights[code.lower()] = points
    return weights


def league_settings_from_raw(raw_settings: dict[str, Any]) -> LeagueSettings:
    """Assemble a LeagueSettings from the raw `settings` block of the league JSON."""
    return LeagueSettings(
        scoring_format=scoring_format_from_type(
            raw_settings.get("scoringSettings", {}).get("scoringType")
        ),
        num_teams=int(raw_settings["size"]),
        roster_slots=starting_slots_from_raw(raw_settings),
        point_weights=point_weights_from_raw(raw_settings),
    )


def positions_of(eligible_slots: list[str], default_position: str = "") -> tuple[str, ...]:
    """Real positions from ESPN's eligibleSlots, in a stable order.

    eligibleSlots is a slot list, not a position list: it also carries G, F,
    SG/SF, UT, BE and IR. Keeping only the five real positions leaves the slot
    vocabulary in this module and lets the engine's eligibility rules expand
    flex and combo slots on its own.
    """
    found = {s.upper() for s in eligible_slots} & REAL_POSITIONS
    if not found and default_position.upper() in REAL_POSITIONS:
        found = {default_position.upper()}
    return tuple(p for p in ("PG", "SG", "SF", "PF", "C") if p in found)


def projected_stats_of(player_stats: dict[str, Any], season: int) -> dict[str, float]:
    """This season's PROJECTED season totals, keyed by lowercase stat code.

    espn-api files splits under keys like "2027_projected" and "2027_total";
    projections are what draft prep values, so actuals are deliberately ignored.
    Lowercasing matches point_weights_from_raw, which is what lets project_points
    line the two up.
    """
    split = player_stats.get(f"{season}_projected", {})
    totals = split.get("total") or {}
    return {
        str(code).lower(): float(value)
        for code, value in totals.items()
        if isinstance(value, (int, float))
    }


class EspnDataSource:
    """Reads a real ESPN league through the `espn-api` package.

    Not exercised against the network in tests — the translation functions above
    carry the mapping and are tested against fixtures. Credentials are optional:
    a public league needs none, a private one needs the ESPN_S2/SWID cookies,
    which are live session secrets and never logged or persisted here (SPEC 2.3).
    """

    def __init__(self, espn_s2: str | None = None, swid: str | None = None):
        self._espn_s2 = espn_s2
        self._swid = swid
        self._cache: dict[tuple[int, int], Any] = {}

    def _league(self, espn_league_id: int, season: int) -> Any:
        """Fetch (and memoize) the League. Settings and pool are one sync, one fetch."""
        key = (espn_league_id, season)
        if key not in self._cache:
            from espn_api.basketball import League

            self._cache[key] = League(
                league_id=espn_league_id,
                year=season,
                espn_s2=self._espn_s2,
                swid=self._swid,
            )
        return self._cache[key]

    def get_league_settings(self, espn_league_id: int, season: int) -> LeagueSettings:
        league = self._league(espn_league_id, season)
        raw = league.espn_request.get_league()["settings"]
        return league_settings_from_raw(raw)

    def get_player_pool(self, espn_league_id: int, season: int) -> list[PlayerProjection]:
        """Every player who matters: free agents plus everyone already rostered.

        Replacement level is defined over the whole league-wide pool, so a pool
        missing rostered players would set it against free agents alone and
        inflate every value.
        """
        league = self._league(espn_league_id, season)
        pool = list(league.free_agents(size=FREE_AGENT_POOL_SIZE))
        pool += [p for team in league.teams for p in team.roster]

        projections: dict[int, PlayerProjection] = {}
        for p in pool:
            # Dedupe: a player claimed between the two calls can appear in both.
            if p.playerId in projections:
                continue
            projections[p.playerId] = PlayerProjection(
                espn_player_id=p.playerId,
                name=p.name,
                positions=positions_of(p.eligibleSlots, getattr(p, "position", "")),
                stats=projected_stats_of(p.stats, season),
                pro_team=getattr(p, "proTeam", ""),
            )

        # ESPN only projects the top few hundred players, so SOME empty stat
        # lines are normal (a deep bench player is genuinely worth ~0). None at
        # all means the season itself has no projections published yet.
        if projections and not any(p.stats for p in projections.values()):
            raise ProjectionsUnavailable(
                f"ESPN has no published projections for season {season} "
                f"(league {espn_league_id}); all {len(projections)} players came back "
                f"with empty stats. Projections appear closer to opening night — "
                f"until then, value the prior season instead."
            )
        return list(projections.values())
