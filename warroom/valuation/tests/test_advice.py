"""The rollout: a candidate's pick played forward to your next one.

The failure it exists for, from a real league: with one centre seat and a deep
pool of bigs, greedy priced every big as though each got the seat, and could
not see that the guards it was passing on would be gone by the time the wheel
came back. These tests build that board in miniature.
"""

import pytest

from warroom.valuation.advice import RolloutField, advise, lineup_value, rollout_values
from warroom.valuation.domain import LeagueSettings, PlayerProjection
from warroom.valuation.draft import bench_replacement, live_replacement_levels

SETTINGS = LeagueSettings(
    scoring_format="points",
    num_teams=3,
    roster_slots={"G": 1, "C": 1},
    point_weights={"pts": 1.0},
    roster_size=4,
)


def p(pid, pos, pts):
    return PlayerProjection(
        espn_player_id=pid, name=f"{pos}{pid}", positions=(pos,), stats={"pts": pts}
    )


# Bigs are deep and flat; guards fall off a cliff after the first.
BIGS = [p(1, "C", 100), p(2, "C", 97), p(3, "C", 94), p(4, "C", 91), p(5, "C", 88)]
GUARDS = [p(11, "PG", 92), p(12, "PG", 60), p(13, "PG", 45), p(14, "PG", 40), p(15, "PG", 35)]
POOL = BIGS + GUARDS
POINTS = {x.espn_player_id: x.stats["pts"] for x in POOL}
# The room sees guards as better than bigs, the way ESPN's display does in a
# league that scores rebounds ESPN does not project.
ROOM = {**{b.espn_player_id: 10.0 for b in BIGS}, **{g.espn_player_id: 50.0 for g in GUARDS}}

# Snake, three teams, you pick first: 1, then 2 3 3 2, then you again at 6.
UPCOMING = [(1, 1, True), (2, 2, False), (3, 3, False), (4, 3, False), (5, 2, False), (6, 1, True)]


def played(candidates, field=ROOM, upcoming=UPCOMING, mine=()):
    return rollout_values(
        candidates,
        mine=list(mine),
        rosters={},
        available=POOL,
        upcoming=upcoming,
        settings=SETTINGS,
        points=POINTS,
        field_points=field,
    )


class TestRolloutValues:
    def test_the_second_big_is_not_priced_into_a_seat_that_is_gone(self):
        """Take a big, and next turn's response cannot be another big: the
        one centre seat is already yours."""
        response = played([BIGS[0]])[1].next_player
        assert "C" not in response.positions

    def test_a_scarce_guard_now_beats_a_better_big_now(self):
        """The top big is worth more in isolation. But the room takes guards,
        so passing on the one good guard means starting a 45 at G — while a
        big barely worse than the top one is still there next turn."""
        rollouts = played([BIGS[0], GUARDS[0]])
        assert rollouts[11].value > rollouts[1].value
        assert "C" in rollouts[11].next_player.positions

    def test_a_room_that_overlooks_the_guard_lets_him_fall_to_you(self):
        """Same board, a room that rates bigs and has the good guard LAST
        among guards: he comes back round, so the better big now is right."""
        overlooks = {
            **{b.espn_player_id: 50.0 for b in BIGS},
            **{g.espn_player_id: 5.0 for g in GUARDS},
            11: 1.0,
        }
        rollouts = played([BIGS[0], GUARDS[0]], field=overlooks)
        assert rollouts[1].next_player.espn_player_id == 11
        assert rollouts[1].value > rollouts[11].value

    def test_your_last_pick_has_no_response(self):
        last = [(1, 1, True), (2, 2, False)]
        rollout = played([BIGS[0]], upcoming=last)[1]
        assert rollout.next_player is None

        board = [[]]
        replacement = live_replacement_levels(POOL, board, SETTINGS, POINTS)
        bench = bench_replacement(POOL, replacement, POINTS)
        expected = lineup_value([BIGS[0]], SETTINGS, replacement, bench, POINTS)
        assert rollout.value == pytest.approx(expected)

    def test_depth_never_lowers_a_lineup(self):
        """A full lineup plus a bench response is worth the lineup, not less."""
        rollouts = played([BIGS[1]], mine=[GUARDS[0]])
        now = played([BIGS[1]], mine=[GUARDS[0]], upcoming=UPCOMING[:1])[2].value
        assert rollouts[2].value >= now

    def test_no_pick_of_yours_left_means_nothing_to_roll(self):
        assert played([BIGS[0]], upcoming=[(1, 2, False)]) == {}

    def test_deterministic(self):
        assert played(POOL) == played(POOL)


class TestAdvise:
    def _advise(self, rollout=None):
        return advise(
            available=POOL,
            rosters={},
            mine=[],
            upcoming=UPCOMING,
            settings=SETTINGS,
            points=POINTS,
            preseason={},
            rollout=rollout,
        )

    def test_greedy_carries_no_rollout(self):
        advice, waiting = self._advise()
        assert all(a.rollout is None for a in advice)
        assert waiting == 4

    def test_rollout_plays_forward_only_the_shortlist(self):
        advice, _ = self._advise(RolloutField(points=ROOM, candidates=3))
        rolled = [a for a in advice if a.rollout is not None]
        assert len(rolled) == 3
        best_greedy = sorted(advice, key=lambda a: -a.score)[:3]
        assert {a.live.value.player.espn_player_id for a in rolled} == {
            a.live.value.player.espn_player_id for a in best_greedy
        }

    def test_rollout_leaves_the_greedy_numbers_alone(self):
        plain, _ = self._advise()
        rolled, _ = self._advise(RolloutField(points=ROOM))
        assert [(a.score, a.survival, a.lineup_delta) for a in plain] == [
            (a.score, a.survival, a.lineup_delta) for a in rolled
        ]
