"""Unit tests for the points-league valuation engine.

Every expected number here is computed by hand in the comments, so a failure
points at a real logic change, not a mystery. No DB, no network.
"""

import collections
from typing import ClassVar

import pytest

from warroom.valuation.data_source import FakePlayerDataSource
from warroom.valuation.domain import LeagueSettings, PlayerProjection
from warroom.valuation.engine import (
    ReplacementBasis,
    compute_replacement_levels,
    default_slot_eligibility,
    marginal_replacements,
    optimal_seating,
    project_points,
    value_over_replacement,
    waiver_replacement_levels,
)


def P(pid, positions, pts, **stats):
    """Terse player builder; `pts` is the 'pts' stat, extras via kwargs."""
    return PlayerProjection(
        espn_player_id=pid,
        name=f"P{pid}",
        positions=tuple(positions),
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
        clean = P(1, ["PG"], 20, to=1)  # 20 - 1 = 19
        loose = P(2, ["PG"], 20, to=5)  # 20 - 5 = 15
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
        scoring_format="points",
        num_teams=2,
        roster_slots={"PG": 1, "C": 1, "UTIL": 1},
        point_weights={"pts": 1.0},
    )


@pytest.fixture
def pool():
    return [
        P(1, ["PG"], 50),
        P(2, ["PG"], 40),
        P(3, ["PG"], 30),
        P(4, ["C"], 45),
        P(5, ["C"], 20),
        P(6, ["C"], 10),
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
        cats = LeagueSettings(
            scoring_format="categories", num_teams=2, roster_slots={"PG": 1}, point_weights={}
        )
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
            scoring_format="points",
            num_teams=1,
            roster_slots={"PG": 1, "C": 1, "UTIL": 1},
            point_weights={"pts": 1.0},
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
            scoring_format="points",
            num_teams=2,
            roster_slots={"C": 2},
            point_weights={"pts": 1.0},
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


# --------------------------------------------------------------------------- #
# Combo slots. ESPN roster slots like SG/SF, G/F, PF/C and F/C mean EITHER side.
# Matched literally they select nobody, so the slot quietly creates no starter
# demand and every replacement level is drawn from too shallow a pool.
# --------------------------------------------------------------------------- #
class TestComboSlotEligibility:
    def test_combo_of_two_positions_accepts_either_side(self):
        assert default_slot_eligibility("PF/C", ("PF",))
        assert default_slot_eligibility("PF/C", ("C",))
        assert not default_slot_eligibility("PF/C", ("PG",))

    def test_combo_of_flex_letters_expands_each_side(self):
        # G/F is guard OR forward, so the G and F rules apply per part.
        assert default_slot_eligibility("G/F", ("PG",))
        assert default_slot_eligibility("G/F", ("SG",))
        assert default_slot_eligibility("G/F", ("SF",))
        assert default_slot_eligibility("G/F", ("PF",))
        assert not default_slot_eligibility("G/F", ("C",))

    def test_combo_slot_is_not_a_literal_position(self):
        # The bug this guards: "SG/SF" is never an element of `positions`.
        assert default_slot_eligibility("SG/SF", ("SG",))
        assert default_slot_eligibility("SG/SF", ("SF",))
        assert not default_slot_eligibility("SG/SF", ("C",))

    def test_plain_slots_are_unchanged(self):
        assert default_slot_eligibility("PG", ("PG",))
        assert default_slot_eligibility("G", ("SG",))
        assert not default_slot_eligibility("G", ("C",))
        assert default_slot_eligibility("F", ("PF",))
        assert default_slot_eligibility("UTIL", ("C",))
        assert not default_slot_eligibility("C", ("PG",))

    def test_combo_slot_creates_starter_demand(self):
        # num_teams=1, slots PF/C:1, weight pts:1
        #   PF/C pool sorted: p1(PF)=30, p2(C)=20 -> last starter = 1st = 30
        # Before the fix the pool was empty and replacement fell back to 0.0.
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=1,
            roster_slots={"PF/C": 1},
            point_weights={"pts": 1.0},
        )
        players = [P(1, ["PF"], 30), P(2, ["C"], 20), P(3, ["PG"], 10)]
        points = {p.espn_player_id: p.stats["pts"] for p in players}
        repl = compute_replacement_levels(players, settings, points)
        assert repl["PF/C"] == 30.0


class TestRealLeagueRoster:
    """Regression guard for the live league: 10 teams, 8 starters, 3 combo slots.

    Slot labels and counts read from ESPN's rosterSettings.lineupSlotCounts,
    so "UT" is ESPN's own label - not the "UTIL" spelling used elsewhere.

    Every configured starting slot must find eligible players; a slot that
    matches nobody is the signature of an unhandled slot label.
    """

    SLOTS: ClassVar[dict[str, int]] = {
        "PG": 1,
        "G": 1,
        "SG/SF": 1,
        "G/F": 1,
        "PF/C": 2,
        "UT": 2,
    }

    def test_every_starting_slot_has_eligible_players(self):
        squad = [
            P(1, ["PG"], 40),
            P(2, ["SG"], 35),
            P(3, ["SF"], 30),
            P(4, ["PF"], 25),
            P(5, ["C"], 20),
        ]
        for slot in self.SLOTS:
            assert any(default_slot_eligibility(slot, p.positions) for p in squad), (
                f"no player eligible for {slot!r} - unhandled slot label"
            )

    def test_replacement_reflects_all_eight_starters(self):
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=10,
            roster_slots=self.SLOTS,
            point_weights={"pts": 1.0},
        )
        pool = [P(i, ["PG", "SG", "SF", "PF", "C"], 100 - i) for i in range(100)]
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        repl = compute_replacement_levels(pool, settings, points)
        # Every slot is populated, none silently defaulted to 0.0.
        assert set(repl) == set(self.SLOTS)
        assert all(v > 0 for v in repl.values())
        # PF/C:2 over 10 teams -> 20 starters -> the 20th best (index 19).
        assert repl["PF/C"] == points[19]


# --------------------------------------------------------------------------- #
# waiver_replacement_levels — the draft-time baseline
# --------------------------------------------------------------------------- #
class TestWaiverBasis:
    """2 teams, PG:1/C:1/UTIL:1 starting, roster_size 2 -> 4 players drafted.

    Pool of 6: guards 50/40/30, centres 45/20/10. Starter basis measures
    against the LAST STARTER (2nd best eligible); waiver basis measures
    against the best player left after 4 come off the board.
    """

    SETTINGS: ClassVar[LeagueSettings] = LeagueSettings(
        scoring_format="points",
        num_teams=2,
        roster_slots={"PG": 1, "C": 1, "UTIL": 1},
        point_weights={"pts": 1.0},
        roster_size=2,
    )
    POOL: ClassVar[list] = [
        P(1, ["PG"], 50), P(2, ["PG"], 40), P(3, ["PG"], 30),
        P(4, ["C"], 45), P(5, ["C"], 20), P(6, ["C"], 10),
    ]  # fmt: skip

    @property
    def points(self):
        return {p.espn_player_id: p.stats["pts"] for p in self.POOL}

    def test_the_waiver_baseline_sits_below_the_starter_baseline(self):
        """The whole argument: the marginal STARTER is rostered by somebody,
        so he is not what you fall back to. The best UNDRAFTED player is."""
        starter = compute_replacement_levels(self.POOL, self.SETTINGS, self.points)
        waiver = waiver_replacement_levels(self.POOL, self.SETTINGS, self.points)
        assert all(waiver[s] <= starter[s] for s in starter)
        assert any(waiver[s] < starter[s] for s in starter)

    def test_it_measures_against_the_best_player_nobody_drafted(self):
        # 2 teams x roster 2 = 4 drafted: by value that is p1, p4, p2, p5.
        # Left over: p3 (PG 30) and p6 (C 10). So the best free guard is 30
        # and the best free centre is 10, and UTIL takes the better of the two.
        waiver = waiver_replacement_levels(self.POOL, self.SETTINGS, self.points)
        assert waiver == {"PG": 30.0, "C": 10.0, "UTIL": 30.0}

    def test_values_rise_by_the_drop_in_the_baseline(self):
        starter = value_over_replacement(self.POOL, self.SETTINGS)
        waiver = value_over_replacement(self.POOL, self.SETTINGS, basis=ReplacementBasis.WAIVER)
        by_id = {v.player.espn_player_id: v for v in waiver}
        for v in starter:
            assert by_id[v.player.espn_player_id].value >= v.value

    def test_a_league_with_no_roster_size_falls_back_to_the_starter_basis(self):
        """roster_size defaults to 0 for a hand-built settings object, and a
        pool size of zero means there is no draft to be outside of. Guessing
        one would be worse than declining to."""
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=2,
            roster_slots={"PG": 1, "C": 1, "UTIL": 1},
            point_weights={"pts": 1.0},
        )
        assert waiver_replacement_levels(
            self.POOL, settings, self.points
        ) == compute_replacement_levels(self.POOL, settings, self.points)

    def test_a_pool_smaller_than_the_draft_has_no_free_players(self):
        """Everyone is rostered, so the floor is the worst player at the slot
        rather than 0.0 — which would hand everyone their whole projection."""
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=10,
            roster_slots={"PG": 1},
            point_weights={"pts": 1.0},
            roster_size=13,
        )
        pool = [P(1, ["PG"], 50), P(2, ["PG"], 40)]
        levels = waiver_replacement_levels(pool, settings, {1: 50.0, 2: 40.0})
        assert levels == {"PG": 40.0}

    def test_the_default_basis_leaves_existing_numbers_alone(self):
        assert value_over_replacement(self.POOL, self.SETTINGS) == value_over_replacement(
            self.POOL, self.SETTINGS, basis=ReplacementBasis.STARTER
        )


class TestMarginalBasis:
    """Value as what the league loses when a player is removed.

    2 teams, PG:1 / C:1 / UTIL:1 -> six chairs. Guards 50/40/30, centres
    45/20/10, so six players fit exactly six chairs with nobody spare.
    """

    SETTINGS: ClassVar[LeagueSettings] = LeagueSettings(
        scoring_format="points",
        num_teams=2,
        roster_slots={"PG": 1, "C": 1, "UTIL": 1},
        point_weights={"pts": 1.0},
        roster_size=3,
    )

    def pool(self, *players):
        return list(players)

    def test_every_chair_is_filled_exactly_once(self):
        """The flaw this basis exists to fix: the scarcest-slot rule let 111
        players claim 10 chairs. Here six chairs seat six players."""
        pool = [
            P(1, ["PG"], 50), P(2, ["PG"], 40), P(3, ["PG"], 30),
            P(4, ["C"], 45), P(5, ["C"], 20), P(6, ["C"], 10),
        ]  # fmt: skip
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        seating = optimal_seating(pool, points, {"PG": 2, "C": 2, "UTIL": 2})
        assert len(seating) == 6
        assert sorted(collections.Counter(seating.values()).items()) == [
            ("C", 2),
            ("PG", 2),
            ("UTIL", 2),
        ]

    def test_the_best_players_get_the_chairs(self):
        pool = [P(1, ["PG"], 50), P(2, ["PG"], 40), P(3, ["PG"], 30)]
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        seating = optimal_seating(pool, points, {"PG": 1, "C": 1})
        assert set(seating) == {1}  # only one chair a guard can take

    def test_a_player_is_valued_against_the_worst_starter_at_their_slot(self):
        """Per slot, read off the seating — not a single league-wide number.

        An earlier version asked "remove this player, who comes off the
        bench", which is the textbook shadow price and which collapsed: on a
        real league all 80 starters came back with the SAME replacement,
        because flex chairs let one bench player reach any vacancy through an
        alternating path, and the board became a ranking by projected points.
        That path needs other managers to rearrange their lineups to backfill
        your vacancy, which ten independent teams do not do.
        """
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=1,
            roster_slots={"PG": 1, "C": 2},
            point_weights={"pts": 1.0},
            roster_size=4,
        )
        pool = [P(1, ["PG"], 50), P(4, ["C"], 45), P(5, ["C"], 20), P(6, ["C"], 10)]
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        got = marginal_replacements(pool, settings, points)
        # Two C chairs seat 45 and 20, so the marginal centre is 20.
        assert got[4] == ("C", 20.0)
        assert got[5] == ("C", 20.0)
        # The one PG chair seats only him, so he is his own marginal starter.
        assert got[1] == ("PG", 50.0)
        # Unseated, so measured against the best player also on the bench.
        assert got[6][0] == "BENCH"

    def test_scarcity_survives_as_different_levels_per_slot(self):
        """The property the shadow-price version destroyed. If every slot
        shares one baseline, value is just projected points and the engine
        has no reason to exist."""
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=1,
            roster_slots={"PG": 1, "C": 1},
            point_weights={"pts": 1.0},
            roster_size=4,
        )
        pool = [P(1, ["PG"], 50), P(2, ["PG"], 48), P(4, ["C"], 30), P(5, ["C"], 5)]
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        got = marginal_replacements(pool, settings, points)
        levels = {slot: repl for slot, repl in got.values() if slot != "BENCH"}
        assert len(set(levels.values())) > 1

    def test_it_does_not_depend_on_the_input_order(self):
        """Where the cascade failed: four processing orders, four boards."""
        pool = [
            P(1, ["PG"], 50), P(2, ["PG", "SG"], 40), P(3, ["SF"], 30),
            P(4, ["C"], 45), P(5, ["C", "PF"], 20), P(6, ["SG"], 10),
        ]  # fmt: skip
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        first = marginal_replacements(pool, self.SETTINGS, points)
        second = marginal_replacements(list(reversed(pool)), self.SETTINGS, points)
        assert first == second

    def test_demand_narrows_the_chairs(self):
        """What the draft-time engine passes once seats start filling."""
        pool = [P(1, ["PG"], 50), P(2, ["PG"], 40), P(3, ["PG"], 30)]
        points = {p.espn_player_id: p.stats["pts"] for p in pool}
        full = marginal_replacements(pool, self.SETTINGS, points)
        narrowed = marginal_replacements(
            pool, self.SETTINGS, points, demand={"PG": 1, "C": 0, "UTIL": 0}
        )
        assert sum(1 for s, _ in full.values() if s != "BENCH") > sum(
            1 for s, _ in narrowed.values() if s != "BENCH"
        )

    def test_value_over_replacement_accepts_the_basis(self):
        pool = [P(1, ["PG"], 50), P(4, ["C"], 45), P(5, ["C"], 20)]
        got = value_over_replacement(pool, self.SETTINGS, basis=ReplacementBasis.MARGINAL)
        assert {v.player.espn_player_id for v in got} == {1, 4, 5}
        assert [v.value for v in got] == sorted((v.value for v in got), reverse=True)
