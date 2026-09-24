"""The optimizations in engine.py, pinned by their correctness arguments.

None of these change an answer. Each replaces work that was being redone, and
each rests on a claim that is easy to state and easy to break by accident later:

  * default_slot_eligibility is pure, so it can be cached.
  * the chair ordering inside optimal_seating cannot change during a seating,
    so it can be hoisted out of the recursion.
  * once every chair is taken, no further player can be seated, so the greedy
    pass can stop.
  * only the best unseated player is ever read, so the bench needs a max()
    rather than a sort.

The first three came out of profiling a full 12-team, 13-round auto-draft:
the marginal basis spent its time in 2,090,725 sorts and 2,086,429
augmenting-path calls, for a board of 400 players. Afterwards: 4,296 and
23,491, and the same draft.
"""

import random

import pytest

from warroom.valuation.domain import LeagueSettings, PlayerProjection
from warroom.valuation.engine import (
    _seat_player,
    default_slot_eligibility,
    marginal_replacements,
    optimal_seating,
)

SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UTIL": 3}
POSSETS = [
    ("PG",),
    ("SG",),
    ("SF",),
    ("PF",),
    ("C",),
    ("PG", "SG"),
    ("SF", "PF"),
    ("PF", "C"),
    ("SG", "SF"),
]


def pool(n: int) -> list[PlayerProjection]:
    return [
        PlayerProjection(
            espn_player_id=1000 + i,
            name=f"P{i}",
            positions=POSSETS[i % len(POSSETS)],
            stats={"pts": 0.0},
            pro_team="BOS",
        )
        for i in range(n)
    ]


def scores(players, seed=7) -> dict[int, float]:
    rng = random.Random(seed)
    return {
        p.espn_player_id: round(2500 * (0.995**i) + rng.random() * 20, 2)
        for i, p in enumerate(players)
    }


def settings_for(teams: int) -> LeagueSettings:
    return LeagueSettings(
        scoring_format="points",
        num_teams=teams,
        roster_slots=SLOTS,
        roster_size=13,
        point_weights={"pts": 1.0},
    )


def seat_everyone(players, points, capacity):
    """optimal_seating WITHOUT the early exit — the reference implementation.

    Deliberately a copy rather than a flag on the real function: a flag would
    let the two share whatever bug the exit introduced, which is the one thing
    this comparison exists to rule out.
    """
    occupants = {slot: [] for slot in capacity}
    chairs = sorted(capacity.items(), key=lambda kv: (kv[1], kv[0]))
    for player in sorted(players, key=lambda p: (-points[p.espn_player_id], p.espn_player_id)):
        _seat_player(player, chairs, occupants, default_slot_eligibility, set())
    return {p.espn_player_id: slot for slot, seated in occupants.items() for p in seated}


class TestEligibilityCache:
    def test_the_cache_is_actually_used(self):
        """If the decorator is dropped the answers stay right and the profile
        regresses silently — which is exactly how it got there the first time."""
        default_slot_eligibility.cache_clear()
        for _ in range(50):
            default_slot_eligibility("PF/C", ("PF", "C"))

        assert default_slot_eligibility.cache_info().hits >= 49

    @pytest.mark.parametrize(
        ("slot", "positions", "expected"),
        [
            ("UTIL", ("C",), True),
            ("UT", (), True),
            ("G", ("PG",), True),
            ("G", ("C",), False),
            ("F", ("PF",), True),
            ("PF/C", ("C",), True),
            ("PF/C", ("PG",), False),
            ("SG/SF", ("SF",), True),
            ("pg", ("PG",), True),
            ("PG", ("pg",), True),
        ],
    )
    def test_caching_did_not_change_any_answer(self, slot, positions, expected):
        assert default_slot_eligibility(slot, positions) is expected

    def test_distinct_arguments_are_not_conflated(self):
        """A cache keyed on too little would make this pair agree."""
        assert default_slot_eligibility("C", ("PG",)) is False
        assert default_slot_eligibility("C", ("C",)) is True


class TestOptimalSeatingEarlyExit:
    @pytest.mark.parametrize("n_players", [20, 96, 150, 400])
    def test_matches_seating_every_player(self, n_players):
        """The claim: players walked after every chair is taken could not have
        been seated anyway. Checked against the implementation that walks
        them."""
        players = pool(n_players)
        points = scores(players)
        capacity = {slot: 12 * count for slot, count in SLOTS.items()}

        assert optimal_seating(players, points, capacity) == seat_everyone(
            players, points, capacity
        )

    def test_fills_every_chair_when_the_pool_is_deep(self):
        players = pool(400)
        capacity = {slot: 12 * count for slot, count in SLOTS.items()}

        seated = optimal_seating(players, scores(players), capacity)

        assert len(seated) == sum(capacity.values())

    def test_a_pool_smaller_than_the_chairs_still_seats_everyone_it_can(self):
        """The early exit must not fire on `seated_count >= total_chairs` when
        the pool runs out first — that path ends by exhausting the players."""
        players = pool(10)
        capacity = {slot: 12 * count for slot, count in SLOTS.items()}

        seated = optimal_seating(players, scores(players), capacity)

        assert len(seated) == 10

    def test_every_seated_player_is_eligible_for_their_chair(self):
        players = pool(400)
        capacity = {slot: 12 * count for slot, count in SLOTS.items()}

        seated = optimal_seating(players, scores(players), capacity)

        by_id = {p.espn_player_id: p for p in players}
        assert all(
            default_slot_eligibility(slot, by_id[pid].positions) for pid, slot in seated.items()
        )

    def test_no_chair_is_over_filled(self):
        players = pool(400)
        capacity = {slot: 12 * count for slot, count in SLOTS.items()}

        seated = optimal_seating(players, scores(players), capacity)

        used: dict[str, int] = {}
        for slot in seated.values():
            used[slot] = used.get(slot, 0) + 1
        assert all(used[slot] <= capacity[slot] for slot in used)


class TestMarginalBenchLevel:
    def test_bench_level_is_the_best_unseated_player(self):
        """max() replaced a full sort whose result was indexed at [0]. Ties
        break on the lowest espn_player_id, as the sort did."""
        players = pool(200)
        points = scores(players)
        settings = settings_for(12)

        out = marginal_replacements(players, settings, points)

        seated = optimal_seating(
            players, points, {s: 12 * c for s, c in SLOTS.items()}, default_slot_eligibility
        )
        unseated = [p for p in players if p.espn_player_id not in seated]
        expected = max(points[p.espn_player_id] for p in unseated)

        benched = [repl for slot, repl in out.values() if slot == "BENCH"]
        assert benched and all(level == expected for level in benched)

    def test_tie_on_points_breaks_to_the_lower_id(self):
        players = [
            PlayerProjection(espn_player_id=pid, name=f"P{pid}", positions=("C",), stats={})
            for pid in (30, 10, 20)
        ]
        points = dict.fromkeys((30, 10, 20), 100.0)
        settings = settings_for(1)

        out = marginal_replacements(
            players, settings, points, demand={"C": 1, "PG": 0, "SG": 0, "SF": 0, "PF": 0}
        )

        # id 10 wins the single chair; 20 and 30 are benched against 100.0.
        assert out[10][0] == "C"
        assert out[20][1] == 100.0
