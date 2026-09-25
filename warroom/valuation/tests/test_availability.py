"""The availability model: projected totals scaled to games actually played.

The numbers are fitted (scripts/calibration/availability.py); what is pinned
here is the arithmetic and which player lands in which bucket.
"""

import pytest

from warroom.valuation.availability import (
    AVAILABILITY_BUCKETS,
    apply_availability,
    availability_bucket,
    availability_factor,
    prior_availability,
)
from warroom.valuation.domain import PlayerProjection
from warroom.valuation.stats import resolve_pool


def player(mpg=20.0, history=None, **stats):
    return PlayerProjection(
        espn_player_id=1,
        name="P",
        positions=("C",),
        stats={"mpg": mpg, **stats},
        history=history or {},
    )


class TestPriorAvailability:
    def test_recent_seasons_count_more(self):
        history = {2026: {"gp": 82.0}, 2025: {"gp": 41.0}, 2024: {"gp": 0.0}}
        assert prior_availability(player(history=history)) == pytest.approx(
            (3 * 1.0 + 2 * 0.5 + 1 * 0.0) / 6
        )

    def test_only_the_three_most_recent_seasons(self):
        history = {2026: {"gp": 82.0}, 2025: {"gp": 82.0}, 2024: {"gp": 82.0}, 2023: {"gp": 0.0}}
        assert prior_availability(player(history=history)) == pytest.approx(1.0)

    def test_a_short_season_is_measured_against_its_own_length(self):
        """2020-21 was 72 games: playing all of them is full availability."""
        assert prior_availability(player(history={2021: {"gp": 72.0}})) == pytest.approx(1.0)

    def test_a_season_missed_while_listed_counts_as_zero(self):
        assert prior_availability(player(history={2026: {}})) == 0.0

    def test_a_season_outside_the_nba_is_skipped_not_zero(self):
        history = {2026: None, 2025: {"gp": 82.0}}
        assert prior_availability(player(history=history)) == pytest.approx(1.0)

    @pytest.mark.parametrize("history", [{}, {2026: None, 2025: None}], ids=["unsynced", "rookie"])
    def test_no_nba_season_is_unknown(self, history):
        assert prior_availability(player(history=history)) is None


class TestBuckets:
    def test_heavy_minutes_ignore_history(self):
        assert availability_bucket(player(mpg=34, history={2026: {"gp": 10.0}})) == "mpg 30+"

    def test_a_frail_rotation_player(self):
        assert availability_bucket(player(mpg=26, history={2026: {"gp": 20.0}})) == "mpg <30 frail"

    def test_a_fragile_bench_player(self):
        assert availability_bucket(player(mpg=18, history={2026: {"gp": 50.0}})) == (
            "mpg <24 fragile"
        )

    @pytest.mark.parametrize("history", [{}, {2026: None}], ids=["unsynced", "rookie"])
    def test_no_history_reads_as_healthy(self, history):
        """No evidence of games missed is not evidence of fragility, and
        rookies measured like healthy veterans at the same minutes."""
        assert availability_bucket(player(mpg=18, history=history)) == "mpg <24 healthy"

    def test_fragility_costs_games(self):
        healthy = availability_factor(player(mpg=18, history={2026: {"gp": 80.0}}))
        fragile = availability_factor(player(mpg=18, history={2026: {"gp": 50.0}}))
        frail = availability_factor(player(mpg=18, history={2026: {"gp": 20.0}}))
        assert healthy > fragile > frail

    def test_every_bucket_has_a_factor_below_one(self):
        cases = [
            player(mpg=34),
            player(mpg=26),
            player(mpg=26, history={2026: {"gp": 50.0}}),
            player(mpg=18),
            player(mpg=18, history={2026: {"gp": 50.0}}),
            player(mpg=18, history={2026: {"gp": 10.0}}),
        ]
        assert {availability_bucket(c) for c in cases} == set(AVAILABILITY_BUCKETS)
        assert all(0 < f < 1 for f in AVAILABILITY_BUCKETS.values())


class TestApply:
    def test_totals_scale_and_rates_do_not(self):
        before = player(mpg=18, gp=70.0, pts=1400.0, reb=500.0, ppg=20.0, **{"fg%": 0.5})
        after = apply_availability(before)
        f = availability_factor(before)

        assert after.stats["gp"] == pytest.approx(70 * f)
        assert after.stats["pts"] == pytest.approx(1400 * f)
        assert after.stats["ppg"] == 20.0
        assert after.stats["fg%"] == 0.5
        assert after.stats["mpg"] == 18.0

    def test_per_game_rates_are_unchanged(self):
        before = player(mpg=18, gp=70.0, pts=1400.0)
        after = apply_availability(before)
        assert after.stats["pts"] / after.stats["gp"] == pytest.approx(20.0)

    def test_points_scale_exactly_including_estimated_stats(self):
        """Double-doubles are games x P(per-game rates reach 10): games scale,
        rates do not, so the estimate scales by exactly the same factor —
        which is what lets the calibration treat the adjustment as raw x f."""
        line = {"gp": 70.0, "pts": 1400.0, "reb": 700.0, "ast": 210.0, "stl": 70.0, "blk": 70.0}
        weights = {"pts": 1.0, "oreb": 2.0, "dreb": 1.0, "dd": 5.0}
        before = player(mpg=18, **line)
        after = apply_availability(before)
        (raw,), _ = resolve_pool([before], weights)
        (adj,), _ = resolve_pool([after], weights)

        score = lambda p: sum(w * p.stats.get(s, 0.0) for s, w in weights.items())
        assert score(adj) == pytest.approx(score(raw) * availability_factor(before))
