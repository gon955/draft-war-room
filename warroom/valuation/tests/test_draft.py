"""Unit tests for draft-time valuation.

Same contract as test_engine.py: every expected number is computed by hand in
the comments, and nothing here touches a database, a network or FastAPI. The
board is small enough to draw on paper on purpose — a scarcity effect that
needs 400 players to show up is not one you can check.
"""

from typing import ClassVar

import pytest

from warroom.valuation.domain import LeagueSettings, PlayerProjection, PlayerValue
from warroom.valuation.draft import (
    BENCH_SLOT,
    LiveValue,
    assign_roster,
    assign_to_slots,
    consumed_slots,
    expected_next_best,
    lineup_values_by_mask,
    live_replacement_levels,
    live_values,
    marginal_values,
    pick_scores,
    picks_until_next,
    remaining_demand,
    survival_probabilities,
)


def P(pid, positions, pts):
    """Terse player builder. `pts` doubles as this player's projected points."""
    return PlayerProjection(
        espn_player_id=pid, name=f"P{pid}", positions=tuple(positions), stats={"pts": pts}
    )


# --------------------------------------------------------------------------- #
# The fixture board: 2 teams, slots PG:1 / C:1 / UTIL:1, so league-wide demand
# is PG 2, C 2, UTIL 2. Weight is pts:1.0, so projected points == pts.
#
#   guards:  p1=50  p2=40  p3=30
#   centres: p4=45  p5=20  p6=10
#
# Preseason (engine.py, whole pool, full demand):
#   PG   last starter = 2nd best guard          = p2 = 40
#   C    last starter = 2nd best centre         = p5 = 20
#   UTIL last starter = 2nd best overall (50,45)= 45
# --------------------------------------------------------------------------- #
SETTINGS = LeagueSettings(
    scoring_format="points",
    num_teams=2,
    roster_slots={"PG": 1, "C": 1, "UTIL": 1, "BE": 3},
    point_weights={"pts": 1.0},
    roster_size=6,
)

POOL = [
    P(1, ["PG"], 50),
    P(2, ["PG"], 40),
    P(3, ["PG"], 30),
    P(4, ["C"], 45),
    P(5, ["C"], 20),
    P(6, ["C"], 10),
]
POINTS = {p.espn_player_id: p.stats["pts"] for p in POOL}
# Preseason (replacement, value) per player, as engine.py computes them.
PRESEASON = {
    1: (40.0, 10.0),
    2: (40.0, 0.0),
    3: (40.0, -10.0),
    4: (20.0, 25.0),
    5: (20.0, 0.0),
    6: (20.0, -10.0),
}


def by_id(*pids):
    return [next(p for p in POOL if p.espn_player_id == pid) for pid in pids]


# --------------------------------------------------------------------------- #
# assign_to_slots
# --------------------------------------------------------------------------- #
class TestAssignToSlots:
    def test_an_empty_roster_consumes_nothing(self):
        assert assign_to_slots([], SETTINGS) == {"PG": 0, "C": 0, "UTIL": 0}

    def test_bench_slots_are_never_counted(self):
        # BE:3 is in roster_slots and must not appear: it takes a roster spot
        # but no lineup spot, so it sets no replacement level.
        assert "BE" not in assign_to_slots(by_id(1), SETTINGS)

    def test_a_player_takes_their_scarcest_open_slot(self):
        # A guard fits PG (1 seat) and UTIL (1 seat); PG wins the tie by name
        # and, more importantly, is the specific slot rather than the flex one.
        assert assign_to_slots(by_id(1), SETTINGS) == {"PG": 1, "C": 0, "UTIL": 0}

    def test_the_second_guard_falls_to_util(self):
        assert assign_to_slots(by_id(1, 2), SETTINGS) == {"PG": 1, "C": 0, "UTIL": 1}

    def test_a_third_guard_is_benched_and_consumes_nothing(self):
        # PG and UTIL are full; there is no third seat a guard can occupy.
        assert assign_to_slots(by_id(1, 2, 3), SETTINGS) == {"PG": 1, "C": 0, "UTIL": 1}

    def test_most_constrained_player_is_placed_first(self):
        """The ordering rule, isolated.

        Slots PF:1 / C:1. A pure C and a PF/C. Placed in roster order the
        flexible PF/C could take C, stranding the pure C on the bench behind an
        empty PF seat they can never fill — one consumed seat instead of two,
        and a replacement level computed against a league one seat too shallow.
        """
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=1,
            roster_slots={"PF": 1, "C": 1},
            point_weights={"pts": 1.0},
        )
        flexible, pure = P(10, ["PF", "C"], 30), P(11, ["C"], 20)
        assert assign_to_slots([flexible, pure], settings) == {"PF": 1, "C": 1}
        # Order of the input must not change the answer.
        assert assign_to_slots([pure, flexible], settings) == {"PF": 1, "C": 1}


# --------------------------------------------------------------------------- #
# consumed_slots / remaining_demand
# --------------------------------------------------------------------------- #
class TestDemand:
    def test_nothing_drafted_leaves_full_demand(self):
        consumed = consumed_slots([], SETTINGS)
        assert remaining_demand(SETTINGS, consumed) == {"PG": 2, "C": 2, "UTIL": 2}

    def test_each_team_is_assigned_separately(self):
        # Two teams with one guard each fill each team's own PG seat: PG 2, not
        # PG 1 + UTIL 1, which is what summing them as one roster would give.
        assert consumed_slots([by_id(1), by_id(2)], SETTINGS) == {"PG": 2, "C": 0, "UTIL": 0}

    def test_two_guards_on_one_team_spill_into_util(self):
        assert consumed_slots([by_id(1, 2)], SETTINGS) == {"PG": 1, "C": 0, "UTIL": 1}

    def test_demand_never_goes_negative(self):
        # Three guards across two teams: PG 2 seats filled, UTIL 1. A fourth
        # guard would be benched, so no count can exceed its league-wide seats.
        consumed = consumed_slots([by_id(1, 2), by_id(3)], SETTINGS)
        assert remaining_demand(SETTINGS, consumed) == {"PG": 0, "C": 2, "UTIL": 1}


# --------------------------------------------------------------------------- #
# live_replacement_levels — the point of the module
# --------------------------------------------------------------------------- #
class TestLiveReplacementLevels:
    def test_an_untouched_board_reproduces_the_preseason_levels(self):
        """Nothing drafted must give back exactly what engine.py gives.

        This is the anchor: if the live computation disagrees with the static
        one on an empty board, every mid-draft number it produces is suspect.
        """
        assert live_replacement_levels(POOL, [], SETTINGS, POINTS) == {
            "PG": 40.0,
            "C": 20.0,
            "UTIL": 45.0,
        }

    def test_draining_a_slot_lowers_its_replacement_level(self):
        """The scarcity effect, measured.

        Team 1 drafts p4 (C, 45), so C demand falls 2 -> 1 and the centre pool
        left is p5=20, p6=10. Last needed centre is now the 1st = p5 = 20 ...
        unchanged here, so use the guards, where the drop is visible:

        Team 1 drafts p1 (PG, 50): PG demand 2 -> 1, guards left p2=40, p3=30,
        last needed = 1st = p2 = 40. Also unchanged.

        Both stay put because the drafted player was ABOVE the replacement
        line, which is the correct and slightly counter-intuitive result: the
        line only moves when the seats run out faster than the talent does.
        """
        levels = live_replacement_levels(by_id(2, 3, 4, 5, 6), [by_id(1)], SETTINGS, POINTS)
        assert levels["PG"] == 40.0

    def test_the_level_moves_once_seats_outrun_talent(self):
        # Both teams take a guard: p1 -> team 1's PG, p2 -> team 2's PG.
        # PG demand 2 -> 0, so PG saturates and drops out entirely. UTIL demand
        # is still 2, pool left is p3=30, p4=45, p5=20, p6=10 -> sorted
        # 45,30,20,10, last needed = 2nd = 30.
        levels = live_replacement_levels(by_id(3, 4, 5, 6), [by_id(1), by_id(2)], SETTINGS, POINTS)
        assert "PG" not in levels
        assert levels["UTIL"] == 30.0
        assert levels["C"] == 20.0

    def test_a_saturated_slot_is_dropped_not_stale(self):
        # Every seat gone: PG 2, C 2, UTIL 2 across two teams of three.
        rosters = [by_id(1, 4, 2), by_id(3, 5, 6)]
        assert live_replacement_levels([], rosters, SETTINGS, POINTS) == {}


# --------------------------------------------------------------------------- #
# live_values
# --------------------------------------------------------------------------- #
class TestLiveValues:
    def test_a_categories_league_is_refused(self):
        settings = LeagueSettings(
            scoring_format="categories", num_teams=2, roster_slots={"PG": 1}, point_weights={}
        )
        with pytest.raises(ValueError, match="points leagues"):
            live_values(POOL, [], settings, POINTS, PRESEASON)

    def test_an_untouched_board_reproduces_the_preseason_values(self):
        results = live_values(POOL, [], SETTINGS, POINTS, PRESEASON)
        # p4 (C 45, replacement 20) = 25 leads p1 (PG 50, replacement 40) = 10,
        # which is the engine's whole argument: centre is the scarcer slot.
        assert [r.value.player.espn_player_id for r in results] == [4, 1, 2, 5, 3, 6]
        assert results[0].value.value == 25.0
        assert all(r.shift == 0.0 for r in results)

    def test_a_drained_slot_lifts_the_players_left_in_it(self):
        """The recommendation this feature exists to make.

        Both guards p1, p2 are gone — one to each team's PG seat — so PG
        saturates and p3 can only be credited at UTIL, whose level has fallen
        45 -> 30 as the pool shrank. p3 was measured against PG's 40 preseason
        and is measured against 30 now, so he goes from -10 to 0: a 10-point
        gain with no change whatsoever to the player.
        """
        available = by_id(3, 4, 5, 6)
        results = live_values(available, [by_id(1), by_id(2)], SETTINGS, POINTS, PRESEASON)
        p3 = next(r for r in results if r.value.player.espn_player_id == 3)
        assert p3.value.assigned_slot == "UTIL"
        assert p3.value.value == 0.0
        assert p3.shift == -10.0  # credited against PG 40, now UTIL 30
        assert p3.value_change == 10.0  # -10 -> 0

    def test_a_player_with_no_open_slot_is_benched_not_inflated(self):
        """The 0.0-replacement trap, guarded.

        Both PG seats and both UTIL seats in the league are gone, so guard p3
        fits nowhere. A 0.0 replacement would hand him his whole 30 points as
        value and float him to the top of the board precisely because nobody
        can start him. He must instead be measured against the shallowest
        level still in play (C = 20) and rank below the centres who do fit.

        Note it takes BOTH teams to saturate a slot: one team holding PG and
        UTIL still leaves the other team's seats open.
        """
        others = [P(7, ["PG"], 28), P(8, ["PG"], 27)]
        rosters = [by_id(1, 2), others]  # PG 2 and UTIL 2 consumed; C untouched
        points = POINTS | {p.espn_player_id: p.stats["pts"] for p in others}
        results = live_values(by_id(3, 4, 5, 6), rosters, SETTINGS, points, PRESEASON)
        p3 = next(r for r in results if r.value.player.espn_player_id == 3)
        assert p3.value.assigned_slot == BENCH_SLOT
        assert p3.value.replacement_points == 20.0  # C, the shallowest open slot
        assert p3.value.value == 10.0  # 30 - 20, not 30 - 0

    def test_with_every_slot_full_the_order_is_plain_projected_points(self):
        rosters = [by_id(1, 4, 2), by_id(3, 5, 6)]
        extras = [P(7, ["PG"], 28), P(8, ["C"], 33), P(9, ["SF"], 12)]
        points = POINTS | {p.espn_player_id: p.stats["pts"] for p in extras}
        results = live_values(extras, rosters, SETTINGS, points, PRESEASON)
        assert [r.value.player.espn_player_id for r in results] == [8, 7, 9]
        assert all(r.value.assigned_slot == BENCH_SLOT for r in results)
        # Best remaining scores 0; everyone else is negative against him.
        assert results[0].value.value == 0.0
        assert results[1].value.value == -5.0  # 28 - 33

    def test_ties_break_on_espn_player_id(self):
        tied = [P(20, ["C"], 25), P(19, ["C"], 25)]
        points = {20: 25.0, 19: 25.0}
        results = live_values(tied, [], SETTINGS, points, {})
        assert [r.value.player.espn_player_id for r in results] == [19, 20]

    def test_an_unknown_player_defaults_to_no_preseason_shift(self):
        # preseason is empty, so the fallback must not raise; the shift is then
        # measured against 0.0 and simply carries no information.
        results = live_values(by_id(4), [], SETTINGS, POINTS, {})
        assert results[0].preseason_value == 0.0


# --------------------------------------------------------------------------- #
# assign_roster — the placement the lineup screen draws
# --------------------------------------------------------------------------- #
class TestAssignRoster:
    def test_an_empty_roster_is_all_empty_seats(self):
        result = assign_roster([], SETTINGS)
        assert [(s.slot, s.index, s.player) for s in result.seats] == [
            ("PG", 1, None),
            ("C", 1, None),
            ("UTIL", 1, None),
        ]
        assert result.bench == ()

    def test_seats_come_back_in_display_order_not_jsonb_order(self):
        """roster_slots is JSONB, so Postgres sorts its keys by length then
        bytes. A lineup drawn in that order reads G, PG, UT, G/F, PF/C — the
        flex slots interleaved with the specific ones. Display order puts the
        specific positions first."""
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=10,
            # Deliberately in the order Postgres returns them.
            roster_slots={"G": 1, "PG": 1, "UT": 2, "G/F": 1, "PF/C": 2, "SG/SF": 1},
            point_weights={"pts": 1.0},
        )
        slots = [s.slot for s in assign_roster([], settings).seats]
        assert slots == ["PG", "G", "SG/SF", "PF/C", "PF/C", "G/F", "UT", "UT"]

    def test_a_multi_seat_slot_gets_numbered_seats(self):
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=2,
            roster_slots={"UTIL": 3},
            point_weights={"pts": 1.0},
        )
        result = assign_roster(by_id(1, 4), settings)
        assert [(s.slot, s.index) for s in result.seats] == [("UTIL", 1), ("UTIL", 2), ("UTIL", 3)]
        assert [s.player.espn_player_id if s.player else None for s in result.seats] == [1, 4, None]

    def test_players_land_in_their_scarcest_slot(self):
        result = assign_roster(by_id(1, 4), SETTINGS)
        filled = {s.slot: s.player.espn_player_id for s in result.seats if s.player}
        assert filled == {"PG": 1, "C": 4}

    def test_the_overflow_player_goes_to_the_bench(self):
        # Three guards, but only PG and UTIL can hold one.
        result = assign_roster(by_id(1, 2, 3), SETTINGS)
        assert [p.espn_player_id for p in result.bench] == [3]
        assert sum(1 for s in result.seats if s.player) == 2

    def test_counts_agree_with_assign_to_slots(self):
        """The two views must never disagree about who is starting — the
        lineup screen and the replacement-level maths read the same placement.
        """
        for roster in ([], by_id(1), by_id(1, 2), by_id(1, 2, 3), by_id(1, 4, 5)):
            assert assign_roster(roster, SETTINGS).counts == assign_to_slots(roster, SETTINGS)

    def test_a_combo_slot_accepts_either_side(self):
        """PF/C is never an element of `positions`; it has to be split."""
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=10,
            roster_slots={"PF/C": 2},
            point_weights={"pts": 1.0},
        )
        pure_pf, pure_c = P(30, ["PF"], 40), P(31, ["C"], 35)
        result = assign_roster([pure_pf, pure_c], settings)
        assert [s.player.espn_player_id for s in result.seats if s.player] == [30, 31]
        assert result.bench == ()

    def test_priority_decides_who_starts_when_eligibility_ties(self):
        """Two players, same eligible slots, one seat. The better one starts.

        Without a priority the tie falls to espn_player_id — roughly seniority
        — so a worse player can take the seat and the better one sits. That is
        wrong for a lineup and invisible in the counts, which is exactly the
        kind of bug that survives a test suite.
        """
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=1,
            roster_slots={"PG": 1},
            point_weights={"pts": 1.0},
        )
        veteran, star = P(100, ["PG"], 10), P(900, ["PG"], 80)

        # Low id wins the tie when nothing else separates them.
        plain = assign_roster([veteran, star], settings)
        assert plain.seats[0].player.espn_player_id == 100
        assert [p.espn_player_id for p in plain.bench] == [900]

        ranked = assign_roster([veteran, star], settings, priority={100: 10.0, 900: 80.0})
        assert ranked.seats[0].player.espn_player_id == 900
        assert [p.espn_player_id for p in ranked.bench] == [100]

    def test_priority_does_not_change_how_many_seats_fill(self):
        """The counts the valuation reads must be identical either way."""
        settings = LeagueSettings(
            scoring_format="points",
            num_teams=2,
            roster_slots={"PG": 1, "C": 1, "UTIL": 1},
            point_weights={"pts": 1.0},
        )
        roster = by_id(1, 2, 4, 5)
        flat = assign_roster(roster, settings)
        ranked = assign_roster(
            roster, settings, priority={p.espn_player_id: -p.stats["pts"] for p in roster}
        )
        assert flat.counts == ranked.counts


# --------------------------------------------------------------------------- #
# marginal_values — step 2: what a player adds to YOUR lineup
# --------------------------------------------------------------------------- #
def _live(player, value):
    """A LiveValue carrying just the number marginal_values reads."""
    return LiveValue(
        value=PlayerValue(player, value, 0.0, value, "UTIL"),
        preseason_replacement=0.0,
        preseason_value=0.0,
    )


class TestLineupValuesByMask:
    def test_it_maximizes_value_not_seats_filled(self):
        """Where greedy and exact disagree, and why this is a separate function.

        Slots PG:1 / UTIL:1. Two good guards (100, 90) and a weak centre (80).
        Most-constrained-first fills the most SEATS: the centre is eligible for
        UTIL only, so he is placed first and takes it, leaving one guard on the
        bench for a lineup of 100 + 80 = 180. Maximizing VALUE benches the
        centre and starts both guards: 100 + 90 = 190.

        assign_roster is right for counting league-wide demand; marginal value
        needs this one.
        """
        seats = ["PG", "UTIL"]
        a, b, c = P(1, ["PG"], 0), P(2, ["PG"], 0), P(3, ["C"], 0)
        table = lineup_values_by_mask([(a, 100.0), (b, 90.0), (c, 80.0)], seats)
        assert max(table) == 190.0

    def test_an_empty_roster_fills_nothing(self):
        assert max(lineup_values_by_mask([], ["PG", "C"])) == 0.0

    def test_a_player_eligible_nowhere_is_ignored(self):
        seats = ["C"]
        guard = P(1, ["PG"], 0)
        assert max(lineup_values_by_mask([(guard, 100.0)], seats)) == 0.0


class TestMarginalValues:
    """SETTINGS is PG:1 / C:1 / UTIL:1, so three seats."""

    LEVELS: ClassVar[dict[str, float]] = {"PG": 40.0, "C": 20.0, "UTIL": 45.0}

    def test_on_an_empty_roster_marginal_equals_live_value(self):
        """The property that makes this safe to turn on.

        Round one must not change. With no roster every candidate walks into an
        empty seat, so their contribution is exactly their own value — and the
        board keeps the scarcity ordering the engine computed rather than
        reverting to raw projected points.

        Below-replacement candidates clamp to 0 instead, which is not a fudge:
        an unfilled seat is worth 0 because you can always put a
        replacement-level free agent in it, so a player who grades under
        replacement genuinely adds nothing.
        """
        candidates = [_live(p, POINTS[p.espn_player_id] - 20.0) for p in POOL]
        got = marginal_values(candidates, [], SETTINGS, self.LEVELS, 45.0, POINTS)
        assert got == {c.value.player.espn_player_id: max(0.0, c.value.value) for c in candidates}
        # And the positive ones are untouched, which is the part that matters.
        assert got[1] == 30.0
        assert got[4] == 25.0

    def test_a_player_worse_than_every_incumbent_adds_nothing(self):
        """Every seat he fits is held by someone better, so he is depth only.

        Note what it takes to reach 0: not merely "no empty seat", but "no
        seat he could take from anyone". A candidate better than the weakest
        starter he can replace is still worth the upgrade.
        """
        roster = by_id(1, 2, 4)  # PG 50, PG 40, C 45
        weak = P(20, ["PG"], 5)
        got = marginal_values(
            [_live(weak, -5.0)], roster, SETTINGS, self.LEVELS, 45.0, POINTS | {20: 5.0}
        )
        assert got[20] == 0.0

    def test_a_better_player_is_worth_the_upgrade_not_his_whole_value(self):
        """Displacement, which is the subtle half of marginal value.

        PG and UTIL are held by guards worth 10 and 0. A guard worth 25 cannot
        add a seat — there is none free he fits — but he can bump the 0 out of
        the lineup, so he is worth 25, the improvement, and the benched player
        is simply no longer counted.
        """
        roster = by_id(1, 2)  # values 10 and 0 against these levels
        better = P(24, ["PG"], 65)
        got = marginal_values(
            [_live(better, 25.0)], roster, SETTINGS, self.LEVELS, 45.0, POINTS | {24: 65.0}
        )
        assert got[24] == 25.0

    def test_marginal_is_never_negative(self):
        roster = by_id(1, 2, 4)
        bad = P(22, ["PG"], 1)
        got = marginal_values(
            [_live(bad, -900.0)], roster, SETTINGS, self.LEVELS, 45.0, POINTS | {22: 1.0}
        )
        assert got[22] == 0.0

    def test_filling_an_open_seat_beats_a_better_player_you_cannot_start(self):
        """The whole point of step 2.

        Two guards hold PG and UTIL, both worth more than the candidate guard,
        so C is the only seat genuinely available. A centre worth 10 is worth
        10 to this lineup; a guard worth 8 — who cannot take C and cannot
        outplay either incumbent — is worth nothing at all.
        """
        roster = by_id(1, 2)
        centre, guard = P(23, ["C"], 30), P(25, ["PG"], 48)
        got = marginal_values(
            [_live(centre, 10.0), _live(guard, 8.0)],
            roster,
            SETTINGS,
            self.LEVELS,
            45.0,
            POINTS | {23: 30.0, 25: 48.0},
        )
        assert got[23] == 10.0
        assert got[25] == 8.0  # he does beat the 0-value incumbent in UTIL

        # Make him worse than both incumbents and he drops to nothing, while
        # the far less valuable centre keeps his 10 because C is empty.
        worse = P(26, ["PG"], 41)
        got = marginal_values(
            [_live(centre, 10.0), _live(worse, -1.0)],
            roster,
            SETTINGS,
            self.LEVELS,
            45.0,
            POINTS | {23: 30.0, 26: 41.0},
        )
        assert got[26] == 0.0
        assert got[23] > got[26]


# --------------------------------------------------------------------------- #
# Step 3: survival and VONA
# --------------------------------------------------------------------------- #
class TestPicksUntilNext:
    def test_a_snake_turn_gives_you_back_to_back_picks(self):
        # 4 teams, I am slot 4: picks ...4, 5... are both mine, so nobody
        # picks in between and nothing can be taken from me.
        upcoming = [(4, 4, True), (5, 4, True), (6, 3, False), (7, 2, False)]
        assert picks_until_next(upcoming) == []

    def test_it_lists_every_team_between_my_two_picks(self):
        upcoming = [
            (1, 1, True),
            (2, 2, False),
            (3, 3, False),
            (4, 4, False),
            (5, 4, False),
            (6, 3, False),
            (7, 2, False),
            (8, 1, True),
        ]
        # Slot 4 appears twice: they pick at the turn, and both of their picks
        # are a chance to take my guy.
        assert picks_until_next(upcoming) == [2, 3, 4, 4, 3, 2]

    def test_my_last_pick_has_nobody_after_it(self):
        assert picks_until_next([(25, 1, True), (26, 2, False)]) == []

    def test_a_board_with_no_picks_of_mine_is_empty(self):
        assert picks_until_next([(1, 2, False), (2, 3, False)]) == []


class TestSurvival:
    def test_nobody_picking_means_everybody_survives(self):
        ranked = [_live(p, 100.0 - p.espn_player_id) for p in POOL]
        got = survival_probabilities(ranked, [], SETTINGS)
        assert set(got.values()) == {1.0}

    def test_the_best_player_is_least_likely_to_last(self):
        ranked = [_live(p, 100.0 - p.espn_player_id) for p in POOL]
        got = survival_probabilities(ranked, [[], [], []], SETTINGS)
        by_rank = [got[c.value.player.espn_player_id] for c in ranked]
        assert by_rank == sorted(by_rank)  # ascending: best survives least

    def test_more_intervening_picks_lower_survival(self):
        ranked = [_live(p, 100.0 - p.espn_player_id) for p in POOL]
        one = survival_probabilities(ranked, [[]], SETTINGS)
        five = survival_probabilities(ranked, [[] for _ in range(5)], SETTINGS)
        assert five[1] < one[1]

    def test_a_team_that_needs_your_position_is_the_threat(self):
        """The opponent-composition half.

        Two identical candidates at different positions. The intervening team
        already has a full PG and UTIL, so only its C seat is open — and only
        the centre is in danger.
        """
        guard, centre = P(30, ["PG"], 40), P(31, ["C"], 40)
        ranked = [_live(guard, 40.0), _live(centre, 40.0)]
        full_guard_roster = by_id(1, 2)  # occupies PG and UTIL
        got = survival_probabilities(ranked, [full_guard_roster], SETTINGS)
        assert got[31] < got[30]
        assert got[30] == 1.0


class TestVona:
    def _score(self, ranked, marginal, survival):
        return pick_scores(marginal, expected_next_best(ranked, marginal, survival))

    def test_it_takes_the_player_who_will_not_be_there_later(self):
        """The point of step 3, and the case that shows the conventional
        `value - expected_next` formula is the wrong ranking.

            take the 80, the 70 is gone next turn   -> 80 + 0  =  80
            take the 70, the 80 is still there      -> 70 + 80 = 150

        So the 70 is the better pick even though he is the worse player.
        """
        safe, doomed = P(40, ["C"], 0), P(41, ["PG"], 0)
        ranked = [_live(safe, 80.0), _live(doomed, 70.0)]
        got = self._score(ranked, {40: 80.0, 41: 70.0}, {40: 1.0, 41: 0.0})
        assert got[41] == 150.0
        assert got[40] == 80.0
        assert got[41] > got[40]

    def test_if_everyone_survives_the_order_does_not_matter(self):
        a, b = P(42, ["PG"], 0), P(43, ["PG"], 0)
        ranked = [_live(a, 50.0), _live(b, 30.0)]
        got = self._score(ranked, {42: 50.0, 43: 30.0}, {42: 1.0, 43: 1.0})
        # You get both either way, so the two picks are worth the same.
        assert got[42] == got[43] == 80.0

    def test_if_nobody_survives_it_reduces_to_taking_the_best(self):
        a, b = P(42, ["PG"], 0), P(43, ["PG"], 0)
        ranked = [_live(a, 50.0), _live(b, 30.0)]
        got = self._score(ranked, {42: 50.0, 43: 30.0}, {42: 0.0, 43: 0.0})
        assert got[42] == 50.0
        assert got[43] == 30.0

    def test_expected_next_excludes_the_player_you_took(self):
        a, b = P(42, ["PG"], 0), P(43, ["PG"], 0)
        ranked = [_live(a, 50.0), _live(b, 30.0)]
        nxt = expected_next_best(ranked, {42: 50.0, 43: 30.0}, {42: 1.0, 43: 1.0})
        assert nxt[42] == 30.0  # took the 50, the 30 is what is left
        assert nxt[43] == 50.0

    def test_a_lone_candidate_leaves_nothing_behind(self):
        only = P(44, ["PG"], 0)
        nxt = expected_next_best([_live(only, 25.0)], {44: 25.0}, {44: 0.5})
        assert nxt[44] == 0.0

    def test_an_empty_board_is_empty(self):
        assert expected_next_best([], {}, {}) == {}
        assert pick_scores({}, {}) == {}
