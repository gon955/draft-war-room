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

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable

from .domain import LeagueSettings, PlayerHistory, PlayerProjection, SeasonLine
from .engine import NON_STARTING_SLOTS

# The five real basketball positions. ESPN's eligibleSlots mixes these with flex
# slots (G, F), combo slots (SG/SF, PF/C) and non-playing slots (BE, IR, UT);
# the domain carries only real positions and lets the engine derive the rest.
REAL_POSITIONS = frozenset({"PG", "SG", "SF", "PF", "C"})

# How many free agents to pull. ESPN pages this; a points league needs the pool
# deep enough that every starting slot reaches its replacement level.
FREE_AGENT_POOL_SIZE = 400

OFF_ROSTER_SLOTS = frozenset({"IR", "IL", "NA"})

# How many completed seasons before the synced one to fetch actuals for. Three
# because that is what the deepest consumer needs: availability weights games
# played over three seasons, the rebound split and the experience buckets only
# look at the last one or two.
HISTORY_SEASONS = 3


class EspnUnreachable(RuntimeError):
    """ESPN did not answer in time, or the connection failed outright.

    Separate from ProjectionsUnavailable, which means ESPN answered and had
    nothing to publish. This one means the request never completed, so the
    right response is "try again", not "value the prior season instead".
    """


# How long an ESPN call may take before it is abandoned. Process-wide rather
# than per instance because the only place it can be enforced is a module-level
# patch — see _install_request_timeout.
DEFAULT_ESPN_TIMEOUT = 15.0


class _TimeoutRequests:
    """Stands in for the `requests` module inside espn_api.

    espn-api calls the module-level requests.get()/requests.post() with no
    timeout anywhere, and exposes no way to pass one. Left alone, a connection
    ESPN accepts and never answers holds the calling thread for as long as the
    process lives. That thread comes from the same AnyIO threadpool that serves
    every other route INCLUDING /health, so enough stuck syncs stop the health
    check and the platform restarts the machine — losing every draft in flight
    to a fault that was only ever one league's sync hanging.

    A shim over the module rather than a rewrite of espn-api, and rather than
    socket.setdefaulttimeout: urllib3 sets its own socket timeouts per request,
    so the process-wide default is overridden before it ever applies.

    __getattr__ forwards everything else untouched, so this stays a true stand-in
    for the module — if espn-api starts calling requests.put(), it keeps working
    (without a timeout, which is why the assertion in the tests enumerates the
    methods espn-api actually uses).
    """

    def __init__(self, delegate: Any, timeout: float):
        self._delegate = delegate
        self.timeout = timeout

    def _with_timeout(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # setdefault, not assignment: a caller that passes its own timeout
        # means it, and silently overriding it would be the same class of
        # surprise this shim exists to remove.
        kwargs.setdefault("timeout", self.timeout)
        return kwargs

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate.get(*args, **self._with_timeout(kwargs))

    def post(self, *args: Any, **kwargs: Any) -> Any:
        return self._delegate.post(*args, **self._with_timeout(kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def _install_request_timeout(timeout: float) -> None:
    """Give every espn-api HTTP call a timeout. Idempotent.

    Installed on first use rather than at import, so merely importing this
    module — which the test suite does constantly — does not reach into a third
    party package. Re-called with a new timeout it updates the existing shim
    instead of wrapping a wrapper, which would otherwise stack one layer per
    request and eventually blow the stack.
    """
    from espn_api.requests import espn_requests

    current = espn_requests.requests
    if isinstance(current, _TimeoutRequests):
        current.timeout = timeout
        return

    espn_requests.requests = _TimeoutRequests(current, timeout)


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
    def get_player_history(
        self, espn_league_id: int, season: int, player_ids: Sequence[int]
    ) -> dict[int, PlayerHistory]: ...


class PlayerDataSourceFactory(Protocol):
    """Builds a data source for ONE league, given that league's ESPN cookie.

    The credential is per league, not per process: a private league is read with
    the cookie its owner supplied, which lives encrypted on the leagues row and
    is decrypted at request time. A single long-lived data source cannot carry
    that, so the app injects this and calls it per request.
    """

    def __call__(self, espn_s2: str | None = None) -> PlayerDataSource: ...


def fixed_source_factory(source: PlayerDataSource) -> PlayerDataSourceFactory:
    """A factory that ignores the cookie and always returns `source`.

    What a test's dependency override hands back in place of the real,
    credential-aware factory — so overriding still takes one line.
    """

    def factory(espn_s2: str | None = None) -> PlayerDataSource:
        return source

    return factory


class FakePlayerDataSource:
    """In-memory data source for tests and local development.

    `history` is keyed by espn_player_id. A player it does not mention gets an
    empty history — no seasons known — which is what a league with no prior
    seasons returns, so a fake built without it behaves like a new league.
    """

    def __init__(
        self,
        settings: LeagueSettings,
        players: list[PlayerProjection],
        history: Mapping[int, PlayerHistory] | None = None,
    ):
        self._settings = settings
        self._players = players
        self._history = dict(history or {})

    def get_league_settings(self, espn_league_id: int, season: int) -> LeagueSettings:
        return self._settings

    def get_player_pool(self, espn_league_id: int, season: int) -> list[PlayerProjection]:
        return list(self._players)

    def get_player_history(
        self, espn_league_id: int, season: int, player_ids: Sequence[int]
    ) -> dict[int, PlayerHistory]:
        return {pid: dict(self._history.get(pid, {})) for pid in player_ids}


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
        roster_size=roster_size_from_raw(raw_settings),
    )


def roster_size_from_raw(raw_settings: dict[str, Any]) -> int:
    """Total roster spots per team: starters plus bench, IR excluded.

    Not derivable from starting_slots_from_raw, which drops bench seats on
    purpose — they create no starter demand, so the engine must not see them.
    Bench depth survives only here. IR/IL/NA are excluded because they are
    extra capacity for injured players, not roster spots to draft into.
    """
    from espn_api.basketball.constant import POSITION_MAP

    lineup_counts = raw_settings.get("rosterSettings", {}).get("lineupSlotCounts", {})

    total_size = 0
    for slot_id_str, count in lineup_counts.items():
        if count <= 0:
            continue

        label = POSITION_MAP.get(int(slot_id_str))
        # An unknown id or ESPN's blank slot is not a roster spot. Without
        # this, label is None, `None not in OFF_ROSTER_SLOTS` is True, and
        # every slot ESPN adds in future silently inflates the roster.
        if not label or label.upper() in OFF_ROSTER_SLOTS:
            continue

        total_size += count

    return total_size


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
    return _totals_of(player_stats, f"{season}_projected")


def actual_stats_of(player_stats: dict[str, Any], season: int) -> SeasonLine:
    """A COMPLETED season's actual totals, keyed like projected_stats_of.

    Actuals carry stats the projection does not — oreb, dreb, dd, td, gs —
    which is the reason history is worth fetching at all. Empty for a player
    ESPN lists but who logged no stat line that season.
    """
    return _totals_of(player_stats, f"{season}_total")


def _totals_of(player_stats: dict[str, Any], split_key: str) -> dict[str, float]:
    totals = player_stats.get(split_key, {}).get("total") or {}
    return {
        str(code).lower(): float(value)
        for code, value in totals.items()
        if isinstance(value, (int, float))
    }


def history_from_cards(
    cards: Mapping[int, Sequence[Any] | None], player_ids: Sequence[int]
) -> dict[int, PlayerHistory]:
    """Assemble each player's history from one player-card fetch per season.

    `cards` maps a season to the espn-api players ESPN returned for it, or to
    None when the league could not be read for that season at all. The two
    are kept apart all the way through: a player missing from a season that
    WAS read is recorded as None (not in the NBA that year), while a season
    that was not read is left out entirely (nothing known) — see PlayerHistory.
    """
    history: dict[int, PlayerHistory] = {pid: {} for pid in player_ids}
    for season, players in cards.items():
        if players is None:
            continue
        lines = {p.playerId: actual_stats_of(p.stats, season) for p in players}
        for pid, seasons in history.items():
            seasons[season] = lines.get(pid)
    return history


def espn_applied_total(player_stats: dict[str, Any], season: int) -> float | None:
    """ESPN's own projected fantasy total for the season, or None.

    `appliedTotal` on the projected split: the stat line ESPN projects, scored
    with THIS league's settings, by ESPN. It sits in the same dict as the raw
    totals projected_stats_of reads and was previously thrown away.

    Worth keeping because it is an independent opinion rather than a different
    route to ours. ESPN projects every stat it scores — including oreb/dreb and
    double-doubles, which it does not publish and stats.py therefore has to
    model — so this number is the one place their estimate of those is visible.
    """
    split = player_stats.get(f"{season}_projected", {})
    total = split.get("applied_total")
    if not isinstance(total, (int, float)):
        return None
    return float(total)


class EspnDataSource:
    """Reads a real ESPN league through the `espn-api` package.

    Not exercised against the network in tests — the translation functions above
    carry the mapping and are tested against fixtures. Credentials are optional:
    a public league needs none, a private one needs the ESPN_S2/SWID cookies,
    which are live session secrets and never logged or persisted here (SPEC 2.3).
    """

    def __init__(
        self,
        espn_s2: str | None = None,
        swid: str | None = None,
        timeout: float = DEFAULT_ESPN_TIMEOUT,
    ):
        self._espn_s2 = espn_s2
        self._swid = swid
        self._timeout = timeout
        self._cache: dict[tuple[int, int], Any] = {}

    def _league(self, espn_league_id: int, season: int) -> Any:
        """Fetch (and memoize) the League. Settings and pool are one sync, one fetch."""
        key = (espn_league_id, season)
        if key not in self._cache:
            from espn_api.basketball import League

            # Before the first call that can block, and on every source since
            # the timeout is a setting that can change under a restart-free
            # config reload. The patch is process-wide (see the function), so
            # the last source built wins — they all read the same setting.
            _install_request_timeout(self._timeout)

            with self._reachable():
                self._cache[key] = League(
                    league_id=espn_league_id,
                    year=season,
                    espn_s2=self._espn_s2,
                    swid=self._swid,
                )
        return self._cache[key]

    @contextmanager
    def _reachable(self) -> Iterator[None]:
        """Turn a transport failure into EspnUnreachable.

        requests' own exceptions are the wrong currency to hand upwards: they
        are the detail this module exists to hide, and uncaught they surface as
        a bare 500 on a request that was perfectly valid and an upstream that
        was merely slow. Only transport failures are translated — an
        ESPNAccessDenied for a bad cookie still comes through as itself,
        because the caller can act on that one.
        """
        from requests import RequestException

        try:
            yield
        except RequestException as exc:
            raise EspnUnreachable(
                f"ESPN did not respond within {self._timeout:g}s. It may be slow or "
                f"down; the league's cached data is unchanged."
            ) from exc

    def get_league_settings(self, espn_league_id: int, season: int) -> LeagueSettings:
        league = self._league(espn_league_id, season)
        with self._reachable():
            raw = league.espn_request.get_league()["settings"]
        return league_settings_from_raw(raw)

    def get_player_pool(self, espn_league_id: int, season: int) -> list[PlayerProjection]:
        """Every player who matters: free agents plus everyone already rostered.

        Replacement level is defined over the whole league-wide pool, so a pool
        missing rostered players would set it against free agents alone and
        inflate every value.
        """
        league = self._league(espn_league_id, season)
        with self._reachable():
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
                espn_points=espn_applied_total(p.stats, season),
                # espn-api also exposes `injured` and `expected_return_date`.
                # The first is redundant with this ("ACTIVE" or not) and two
                # columns that almost always agree is the kind of pair that
                # rots; the second was empty for all 300 players sampled.
                injury_status=getattr(p, "injuryStatus", None),
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

    def get_player_history(
        self, espn_league_id: int, season: int, player_ids: Sequence[int]
    ) -> dict[int, PlayerHistory]:
        """Prior seasons' actuals for exactly these players.

        Looked up by id rather than by diffing each season's pool: a pool is
        the top few hundred by ownership, so a veteran who fell out of it would
        read as a rookie. By id, ESPN returns every player it has for that
        season and nobody else.
        """
        ids = list(player_ids)
        if not ids:
            return {}
        cards = {
            prior: self._player_cards(espn_league_id, prior, ids)
            for prior in range(season - HISTORY_SEASONS, season)
        }
        return history_from_cards(cards, ids)

    def _player_cards(
        self, espn_league_id: int, season: int, player_ids: list[int]
    ) -> list[Any] | None:
        """One season's player cards, or None if the league has no such season.

        Deliberately NOT via _league(): constructing a League fetches teams,
        rosters and the pro schedule, ~1.3s per season that history never
        reads. The bare request is one call, ~0.6s, for the whole pool.
        """
        from espn_api.basketball.player import Player
        from espn_api.requests.espn_requests import (
            ESPNAccessDenied,
            EspnFantasyRequests,
            ESPNInvalidLeague,
        )

        _install_request_timeout(self._timeout)
        cookies = (
            {"espn_s2": self._espn_s2, "SWID": self._swid} if self._espn_s2 and self._swid else None
        )
        request = EspnFantasyRequests(
            sport="nba", year=season, league_id=espn_league_id, cookies=cookies
        )
        with self._reachable():
            try:
                # The second argument bounds per-scoring-period splits, which
                # history never reads; the season total rides in on the
                # "00<year>" filter espn-api always adds. ESPN rejects 0 with
                # an HTTP 400, so 1 is the smallest request it accepts.
                raw = request.get_player_card(player_ids, 1)["players"]
            except (ESPNInvalidLeague, ESPNAccessDenied):
                # The league did not exist that season, or this cookie's owner
                # was not in it. Either way that season is unknown, which is a
                # normal state for history rather than a failed sync — the
                # season being synced has already been read successfully.
                return None
        return [Player(entry, season, {}) for entry in raw]
