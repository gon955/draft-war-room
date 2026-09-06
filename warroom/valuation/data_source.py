"""The seam between the app and ESPN.

Everything that reads ESPN goes through PlayerDataSource. Production wires an
EspnDataSource (wrapping the `espn-api` basketball League); tests and local dev
wire FakePlayerDataSource, so the suite never touches the network and CI stays
green regardless of ESPN's uptime or undocumented changes.
"""
from __future__ import annotations

from typing import Protocol

from .domain import LeagueSettings, PlayerProjection


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


# Sketch of the real adapter — not exercised in tests (it would hit the network).
# Fill in the stat/settings mapping against the espn-api objects when you build it.
#
# class EspnDataSource:
#     def __init__(self, espn_s2: str | None = None, swid: str | None = None):
#         self._espn_s2, self._swid = espn_s2, swid
#
#     def get_league_settings(self, espn_league_id, season):
#         from espn_api.basketball import League
#         lg = League(league_id=espn_league_id, year=season,
#                     espn_s2=self._espn_s2, swid=self._swid)
#         s = lg.settings
#         return LeagueSettings(
#             scoring_format="points",           # derive from s.scoring_type
#             num_teams=s.team_count,
#             roster_slots={...},                # from s roster settings
#             point_weights={...},               # from s scoring items -> lowercase codes
#         )
#
#     def get_player_pool(self, espn_league_id, season):
#         from espn_api.basketball import League
#         lg = League(league_id=espn_league_id, year=season,
#                     espn_s2=self._espn_s2, swid=self._swid)
#         pool = lg.free_agents(size=400) + [p for t in lg.teams for p in t.roster]
#         return [PlayerProjection(
#                     espn_player_id=p.playerId, name=p.name,
#                     positions=tuple(p.eligibleSlots), stats={...})   # projected stats
#                 for p in pool]
