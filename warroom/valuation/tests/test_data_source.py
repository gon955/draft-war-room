"""Contract tests for the ESPN adapter — fixtures only, never the network.

The payloads below mirror the shape of the live league's raw JSON (SPEC 2.2):
10 teams, 8 starters, three combo slots. Testing the translation functions
directly is what keeps the mapping honest without a network call — EspnDataSource
itself adds only fetching, which is stubbed in TestEspnDataSource.
"""
from typing import Any, ClassVar

import pytest

from warroom.valuation.data_source import (
    EspnDataSource,
    FakePlayerDataSource,
    PlayerDataSource,
    ProjectionsUnavailable,
    league_settings_from_raw,
    point_weights_from_raw,
    positions_of,
    projected_stats_of,
    scoring_format_from_type,
    starting_slots_from_raw,
)
from warroom.valuation.engine import compute_replacement_levels, value_over_replacement

SEASON = 2027

# POSITION_MAP slot ids: 0 PG, 1 SG, 2 SF, 3 PF, 4 C, 5 G, 6 F, 7 SG/SF,
# 8 G/F, 9 PF/C, 10 F/C, 11 UT, 12 BE, 13 IR, 14 blank.
RAW_SETTINGS: dict[str, Any] = {
    "name": "Test League",
    "size": 10,
    "rosterSettings": {
        "lineupSlotCounts": {
            "0": 1,   # PG
            "1": 0,   # SG      — unused, no starter demand
            "2": 0,   # SF      — unused
            "3": 0,   # PF      — unused
            "4": 0,   # C       — unused
            "5": 1,   # G
            "6": 0,   # F       — unused
            "7": 1,   # SG/SF   — combo
            "8": 1,   # G/F     — combo
            "9": 2,   # PF/C    — combo
            "10": 0,  # F/C     — unused
            "11": 2,  # UT
            "12": 5,  # BE      — bench, excluded
            "13": 1,  # IR      — excluded
            "14": 3,  # blank   — ESPN's unnamed slot, excluded
        }
    },
    "scoringSettings": {
        "scoringType": "H2H_POINTS",
        "scoringItems": [
            {"statId": 0, "points": 1.0},    # PTS
            {"statId": 6, "points": 1.2},    # REB
            {"statId": 3, "points": 1.5},    # AST
            {"statId": 2, "points": 3.0},    # STL
            {"statId": 1, "points": 3.0},    # BLK
            {"statId": 11, "points": -1.0},  # TO
            {"statId": 17, "points": 0.5},   # 3PM
            {"statId": 19, "points": 0.0},   # FG% — present but unscored
        ],
    },
}


class TestScoringFormat:
    @pytest.mark.parametrize("scoring_type", ["H2H_POINTS", "TOTAL_POINTS", "h2h_points"])
    def test_points_types_map_to_points(self, scoring_type):
        assert scoring_format_from_type(scoring_type) == "points"

    @pytest.mark.parametrize("scoring_type", ["H2H_CATEGORY", "ROTO", "H2H_MOST_CATEGORIES"])
    def test_category_types_map_to_categories(self, scoring_type):
        assert scoring_format_from_type(scoring_type) == "categories"

    def test_missing_type_is_not_silently_points(self):
        # Guessing "points" from a missing field would let the engine run on a
        # league it can't value; "categories" makes it raise instead.
        assert scoring_format_from_type(None) == "categories"


class TestStartingSlots:
    def test_reads_the_real_roster_shape(self):
        assert starting_slots_from_raw(RAW_SETTINGS) == {
            "PG": 1, "G": 1, "SG/SF": 1, "G/F": 1, "PF/C": 2, "UT": 2,
        }

    def test_eight_starters_per_team(self):
        assert sum(starting_slots_from_raw(RAW_SETTINGS).values()) == 8

    def test_bench_ir_and_blank_slots_are_excluded(self):
        slots = starting_slots_from_raw(RAW_SETTINGS)
        # Bench and IR create no starter demand (SPEC 5.1); the blank slot id 14
        # is ESPN's own placeholder and names no position at all.
        assert not {"BE", "IR", ""} & set(slots)

    def test_zero_count_slots_are_excluded(self):
        assert "SG" not in starting_slots_from_raw(RAW_SETTINGS)

    def test_combo_labels_survive_intact(self):
        # The engine splits these itself; flattening them here would lose the
        # slot's real eligibility.
        assert "PF/C" in starting_slots_from_raw(RAW_SETTINGS)

    def test_missing_roster_settings_yields_no_slots(self):
        assert starting_slots_from_raw({"size": 10}) == {}


class TestPointWeights:
    def test_weights_are_lowercase_stat_codes(self):
        assert point_weights_from_raw(RAW_SETTINGS) == {
            "pts": 1.0, "reb": 1.2, "ast": 1.5,
            "stl": 3.0, "blk": 3.0, "to": -1.0, "3pm": 0.5,
        }

    def test_unscored_stats_are_dropped(self):
        # SPEC 2.2: if the league doesn't weight percentages there's no
        # volume-weighting to do, and a 0.0 weight would just be noise.
        assert "fg%" not in point_weights_from_raw(RAW_SETTINGS)

    def test_negative_weights_are_preserved(self):
        assert point_weights_from_raw(RAW_SETTINGS)["to"] == -1.0


class TestLeagueSettingsFromRaw:
    def test_assembles_a_valuable_league(self):
        settings = league_settings_from_raw(RAW_SETTINGS)
        assert settings.scoring_format == "points"
        assert settings.num_teams == 10
        assert settings.roster_slots == {
            "PG": 1, "G": 1, "SG/SF": 1, "G/F": 1, "PF/C": 2, "UT": 2,
        }
        assert settings.point_weights["stl"] == 3.0

    def test_every_slot_reaches_a_real_replacement_level(self):
        # End to end on the fixture: a slot label the engine can't match would
        # fall back to 0.0 and quietly inflate every value at that slot.
        settings = league_settings_from_raw(RAW_SETTINGS)
        pool = [
            _projection(i, ("PG", "SG", "SF", "PF", "C"), pts=100 - i)
            for i in range(100)
        ]
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        repl = compute_replacement_levels(pool, settings, points)
        assert set(repl) == set(settings.roster_slots)
        assert all(v > 0 for v in repl.values())

    def test_ranking_runs_on_the_real_league_shape(self):
        settings = league_settings_from_raw(RAW_SETTINGS)
        pool = [_projection(1, ("C",), pts=40), _projection(2, ("PG",), pts=30)]
        ranked = value_over_replacement(pool, settings)
        assert [v.player.espn_player_id for v in ranked] == [1, 2]


class TestPositionsOf:
    def test_strips_flex_combo_and_non_playing_slots(self):
        # A guard's eligibleSlots as ESPN returns them.
        assert positions_of(["PG", "SG", "G", "G/F", "UT", "BE", "IR"]) == ("PG", "SG")

    def test_order_is_stable_not_espn_order(self):
        assert positions_of(["C", "PF"]) == ("PF", "C")
        assert positions_of(["PF", "C"]) == ("PF", "C")

    def test_falls_back_to_default_position(self):
        # A player whose slots carry no real position still has to be valued.
        assert positions_of(["UT", "BE"], default_position="C") == ("C",)

    def test_unknown_default_is_not_invented(self):
        assert positions_of(["UT", "BE"], default_position="Rookie") == ()

    def test_real_positions_win_over_the_default(self):
        assert positions_of(["SF", "UT"], default_position="C") == ("SF",)


class TestProjectedStats:
    STATS: ClassVar[dict[str, Any]] = {
        f"{SEASON}_projected": {
            "applied_total": 1234.5,
            "total": {"PTS": 1800, "REB": 500.5, "AST": 300, "TO": 150},
        },
        f"{SEASON}_total": {"total": {"PTS": 9, "REB": 9, "AST": 9, "TO": 9}},
    }

    def test_uses_projections_not_actuals(self):
        # Draft prep values what a player WILL do; last season's totals are a
        # different number that would silently rank the pool wrong.
        assert projected_stats_of(self.STATS, SEASON)["pts"] == 1800.0

    def test_codes_are_lowercased_to_match_weights(self):
        stats = projected_stats_of(self.STATS, SEASON)
        assert set(stats) == {"pts", "reb", "ast", "to"}

    def test_values_are_floats(self):
        assert projected_stats_of(self.STATS, SEASON)["reb"] == 500.5

    def test_missing_projection_split_is_empty_not_an_error(self):
        # A player ESPN has no projection for is worth 0, not a crash mid-sync.
        assert projected_stats_of({}, SEASON) == {}
        assert projected_stats_of(self.STATS, 2099) == {}

    def test_non_numeric_stats_are_ignored(self):
        stats = {f"{SEASON}_projected": {"total": {"PTS": 10, "TEAM": "LAL", "DATE": None}}}
        assert projected_stats_of(stats, SEASON) == {"pts": 10.0}

    def test_feeds_the_weights_from_the_same_league(self):
        # The point of lowercasing both sides: the dot product actually lines up.
        weights = point_weights_from_raw(RAW_SETTINGS)
        stats = projected_stats_of(self.STATS, SEASON)
        assert set(stats) <= set(weights)


# --------------------------------------------------------------------------- #
# EspnDataSource itself. Only the fetch is stubbed; the assembly under test is
# the same code that runs against the live API.
# --------------------------------------------------------------------------- #
class _StubPlayer:
    def __init__(self, player_id, name, slots, pts, position="", pro_team="LAL"):
        self.playerId = player_id
        self.name = name
        self.eligibleSlots = slots
        self.position = position
        self.proTeam = pro_team
        self.stats = (
            {f"{SEASON}_projected": {"total": {"PTS": pts}}} if pts is not None
            # An unprojected season: ESPN returns the splits, all empty. Verified
            # against the live league — every 2027 split came back with n=0.
            else {f"{SEASON}_{split}": {"total": {}} for split in
                  ("total", "last_7", "last_15", "last_30")}
        )


class _StubTeam:
    def __init__(self, roster):
        self.roster = roster


class _StubLeague:
    def __init__(self, free_agents, teams):
        self._free_agents = free_agents
        self.teams = teams
        self.free_agent_calls = 0

    def free_agents(self, size):
        self.free_agent_calls += 1
        self.last_size = size
        return list(self._free_agents)

    @property
    def espn_request(self):
        return self

    def get_league(self):
        return {"settings": RAW_SETTINGS}


class StubbedEspnDataSource(EspnDataSource):
    """EspnDataSource with the network fetch replaced, nothing else."""

    def __init__(self, league):
        super().__init__()
        self._stub = league

    def _league(self, espn_league_id, season):
        return self._stub


class TestEspnDataSource:
    def _source(self):
        fa = [
            _StubPlayer(1, "Free Agent", ["SF", "PF", "F", "UT", "BE"], pts=900),
            _StubPlayer(2, "Both Lists", ["PG", "G", "UT"], pts=1500),
        ]
        rostered = [
            _StubPlayer(2, "Both Lists", ["PG", "G", "UT"], pts=1500),
            _StubPlayer(3, "Rostered Star", ["C", "PF/C", "UT"], pts=2000),
        ]
        league = _StubLeague(fa, [_StubTeam(rostered)])
        return StubbedEspnDataSource(league), league

    def test_satisfies_the_protocol(self):
        source, _ = self._source()
        assert isinstance(source, PlayerDataSource)
        assert isinstance(FakePlayerDataSource(None, []), PlayerDataSource)

    def test_settings_come_from_the_raw_league_json(self):
        source, _ = self._source()
        settings = source.get_league_settings(19048, SEASON)
        assert settings.num_teams == 10
        assert settings.roster_slots["PF/C"] == 2

    def test_pool_includes_free_agents_and_rostered_players(self):
        # Replacement level is league-wide (SPEC 5.1); a pool of free agents
        # alone would set it far too low and inflate every value.
        source, _ = self._source()
        pool = source.get_player_pool(19048, SEASON)
        assert {p.espn_player_id for p in pool} == {1, 2, 3}

    def test_a_player_in_both_lists_appears_once(self):
        source, _ = self._source()
        pool = source.get_player_pool(19048, SEASON)
        assert len(pool) == 3

    def test_players_are_translated_into_domain_objects(self):
        source, _ = self._source()
        star = next(p for p in source.get_player_pool(19048, SEASON) if p.espn_player_id == 3)
        assert star.name == "Rostered Star"
        assert star.pro_team == "LAL"
        assert star.positions == ("C",)      # PF/C and UT stripped
        assert star.stats == {"pts": 2000.0}

    def test_pool_is_deep_enough_to_reach_replacement(self):
        source, league = self._source()
        source.get_player_pool(19048, SEASON)
        assert league.last_size >= 400

    def test_some_unprojected_players_are_fine(self):
        # ESPN only projects the top few hundred; a deep bench player really is
        # worth ~0 and must not take the whole sync down.
        league = _StubLeague(
            [_StubPlayer(1, "Star", ["PG", "UT"], pts=1500),
             _StubPlayer(2, "Deep Bench", ["C", "UT"], pts=None)],
            [],
        )
        pool = StubbedEspnDataSource(league).get_player_pool(19048, SEASON)
        assert {p.espn_player_id: bool(p.stats) for p in pool} == {1: True, 2: False}

    def test_a_season_with_no_projections_raises(self):
        # SPEC 5: an all-zero pool ranks into meaningless order rather than
        # failing, so the adapter refuses to hand it over.
        league = _StubLeague(
            [_StubPlayer(1, "Star", ["PG", "UT"], pts=None),
             _StubPlayer(2, "Other", ["C", "UT"], pts=None)],
            [],
        )
        with pytest.raises(ProjectionsUnavailable, match=f"season {SEASON}"):
            StubbedEspnDataSource(league).get_player_pool(19048, SEASON)

    def test_an_empty_pool_is_not_mistaken_for_missing_projections(self):
        # Nothing to rank is a different problem from nothing being projected.
        assert StubbedEspnDataSource(_StubLeague([], [])).get_player_pool(19048, SEASON) == []

    def test_the_pool_values_end_to_end(self):
        source, _ = self._source()
        settings = source.get_league_settings(19048, SEASON)
        ranked = value_over_replacement(source.get_player_pool(19048, SEASON), settings)
        assert [v.player.espn_player_id for v in ranked] == [3, 2, 1]
        assert all(v.assigned_slot != "NONE" for v in ranked)


def _projection(player_id, positions, pts):
    from warroom.valuation.domain import PlayerProjection

    return PlayerProjection(
        espn_player_id=player_id, name=f"p{player_id}",
        positions=positions, stats={"pts": float(pts)},
    )
