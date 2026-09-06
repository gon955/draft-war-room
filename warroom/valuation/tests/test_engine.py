"""Unit tests for the points-league valuation engine.

Every expected number here is computed by hand in the comments, so a failure
points at a real logic change, not a mystery. No DB, no network.
"""
import pytest

from warroom.valuation.data_source import FakePlayerDataSource
from warroom.valuation.domain import LeagueSettings, PlayerProjection
from warroom.valuation.engine import (
    compute_replacement_levels,
    project_points,
    value_over_replacement,
)


def P(pid, positions, pts, **stats):
    """Terse player builder; `pts` is the 'pts' stat, extras via kwargs."""
    return PlayerProjection(
        espn_player_id=pid, name=f"P{pid}", positions=tuple(positions),
        stats={"pts": pts, **stats},
    )


# --------------------------------------------------------------------------- #
# project_points
# --------------------------------------------------------------------------- #
class TestProjectPoints:
    def test_linear_dot_product(self):
        weights = {"pts": 1.0, "reb": 1.0, "ast": 2.0, "to": -1.0}
        player = P(1, ["PG"], 20, reb=10, ast=5, to=3)
        # 20*1 + 10*1 + 5*2 + 3*(-1) = 20 + 10 + 10 - 3 = 37
        assert project_points(player, weights) == 37.0

    def test_turnovers_reduce_score_via_negative_weight(self):
        weights = {"pts": 1.0, "to": -1.0}
        clean = P(1, ["PG"], 20, to=1)   # 20 - 1 = 19
        loose = P(2, ["PG"], 20, to=5)   # 20 - 5 = 15
        assert project_points(clean, weights) > project_points(loose, weights)

    def test_unweighted_stats_are_ignored(self):
        weights = {"pts": 1.0}
        player = P(1, ["PG"], 20, reb=99, blk=99)  # reb/blk have no weight
        assert project_points(player, weights) == 20.0


# --------------------------------------------------------------------------- #
# Main fixture: num_teams=2, slots PG:1 / C:1 / UTIL:1, weight pts:1
# so projected_points == pts. Worked example:
#
#   PG pool  (desc): p1=50, p2=40, p3=30   -> last starter = 2nd = p2=40
#   C  pool  (desc): p4=45, p5=20, p6=10   -> last starter = 2nd = p5=20
#   UTIL pool(all):  50,45,40,30,25,20,10  -> last starter = 2nd = 45
#   replacement = {PG:40, C:20, UTIL:45}
#
#   values (points - min replacement over eligible slots):
#     p1 PG : 50 - min(40,45)=40 -> 10   (slot PG)
#     p2 PG : 40 - 40           ->  0
#     p3 PG : 30 - 40           -> -10
#     p4 C  : 45 - min(20,45)=20 -> 25   (slot C)
#     p5 C  : 20 - 20           ->  0
#     p6 C  : 10 - 20           -> -10
#     p7 SF : 25 - 45 (UTIL only)-> -20  (slot UTIL)
#   ranked ids: [4, 1, 2, 5, 3, 6, 7]
# --------------------------------------------------------------------------- #
@pytest.fixture
def league():
    return LeagueSettings(
        scoring_format="points", num_teams=2,
        roster_slots={"PG": 1, "C": 1, "UTIL": 1}, point_weights={"pts": 1.0},
    )


@pytest.fixture
def pool():
    return [
        P(1, ["PG"], 50), P(2, ["PG"], 40), P(3, ["PG"], 30),
        P(4, ["C"], 45), P(5, ["C"], 20), P(6, ["C"], 10),
        P(7, ["SF"], 25),
    ]


class TestReplacementLevels:
    def test_replacement_is_last_starter_per_slot(self, league, pool):
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        repl = compute_replacement_levels(pool, league, points)
        assert repl == {"PG": 40.0, "C": 20.0, "UTIL": 45.0}

    def test_scarcer_position_has_lower_replacement(self, league, pool):
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        repl = compute_replacement_levels(pool, league, points)
        # C drops off faster than PG here, so C is scarcer -> lower replacement.
        assert repl["C"] < repl["PG"]


class TestValueOverReplacement:
    def test_full_ranking_matches_hand_computation(self, league, pool):
        ranked = value_over_replacement(pool, league)
        assert [v.player.espn_player_id for v in ranked] == [4, 1, 2, 5, 3, 6, 7]
        assert [v.value for v in ranked] == [25.0, 10.0, 0.0, 0.0, -10.0, -10.0, -20.0]

    def test_top_player_beats_higher_scorer_due_to_scarcity(self, league, pool):
        ranked = value_over_replacement(pool, league)
        top = ranked[0]
        # p4 (45 pts, scarce C) outranks p1 (50 pts, deep PG).
        assert top.player.espn_player_id == 4
        assert top.assigned_slot == "C"

    def test_position_only_eligible_for_util_is_valued_against_util(self, league, pool):
        ranked = value_over_replacement(pool, league)
        p7 = next(v for v in ranked if v.player.espn_player_id == 7)
        assert p7.assigned_slot == "UTIL"
        assert p7.value == -20.0  # 25 - 45

    def test_rejects_categories_league(self, pool):
        cats = LeagueSettings(scoring_format="categories", num_teams=2,
                              roster_slots={"PG": 1}, point_weights={})
        with pytest.raises(ValueError):
            value_over_replacement(pool, cats)


class TestMultiEligibility:
    def test_player_credited_at_scarcest_eligible_slot(self):
        # num_teams=1, slots PG:1 / C:1 / UTIL:1, weight pts:1
        #   PG pool: a=30, c=25 -> last starter = 1st = a=30
        #   C  pool: c=25, b=20 -> last starter = 1st = c=25
        #   UTIL   : 30,25,20   -> last starter = 1st = 30
        #   replacement = {PG:30, C:25, UTIL:30}
        #   c is PG/C: min(30,25,30)=25 (C) -> value 25-25=0, slot C
        #   if c were forced to PG: 25-30 = -5, so choosing C is correct.
        settings = LeagueSettings(
            scoring_format="points", num_teams=1,
            roster_slots={"PG": 1, "C": 1, "UTIL": 1}, point_weights={"pts": 1.0},
        )
        players = [P("a", ["PG"], 30), P("b", ["C"], 20), P("c", ["PG", "C"], 25)]
        ranked = value_over_replacement(players, settings)
        c = next(v for v in ranked if v.player.espn_player_id == "c")
        assert c.assigned_slot == "C"
        assert c.value == 0.0


class TestUnderfilledPosition:
    def test_position_with_fewer_players_than_starting_slots(self):
        # slots C:2, num_teams=2 -> 4 starters needed, but only 2 centers exist.
        # replacement clamps to the worst available center (index -1).
        settings = LeagueSettings(
            scoring_format="points", num_teams=2,
            roster_slots={"C": 2}, point_weights={"pts": 1.0},
        )
        players = [P(1, ["C"], 40), P(2, ["C"], 10)]
        points = {p.espn_player_id: p.stats["pts"] for p in players}
        repl = compute_replacement_levels(players, settings, points)
        assert repl["C"] == 10.0  # worst center, not an IndexError


class TestFakeDataSource:
    def test_fake_source_round_trips_and_feeds_engine(self, league, pool):
        src = FakePlayerDataSource(settings=league, players=pool)
        settings = src.get_league_settings(espn_league_id=123, season=2026)
        players = src.get_player_pool(espn_league_id=123, season=2026)
        ranked = value_over_replacement(players, settings)
        assert ranked[0].player.espn_player_id == 4
        # returns a copy, not the internal list
        assert players is not src._players
