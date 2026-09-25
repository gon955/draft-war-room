"""Reconciling ESPN's projected stats with a league's scored ones (SPEC 2.2).

These exist because of a bug that shipped and was invisible. A real 10-team
league scores oreb 2.0 and dreb 1.0; ESPN projects only `reb`. project_points
looked both up, found neither, multiplied by zero, and produced a confident
ranking in which rebounding counted for nothing. Correcting it moved nine of
the top twenty players and Rudy Gobert by 69 places.

Nothing was wrong with the arithmetic. The failure was that `.get(stat, 0.0)`
cannot tell "this league does not score rebounds" from "ESPN did not project
them", so the whole point of this module is that the difference is now carried
in the output and cannot be lost again.
"""

from typing import ClassVar

import pytest

from warroom.valuation.domain import PlayerProjection
from warroom.valuation.stats import (
    BASELINE_BUCKETS,
    BASELINE_FLOOR,
    DEFAULT_OFFENSIVE_SHARE,
    ESTIMATOR_RELATIVE_SD,
    OFFENSIVE_REBOUND_SHARE,
    OREB_SHARE_PRIOR_REBOUNDS,
    OREB_SHARE_SEASONS,
    UNMEASURED_ESTIMATOR_SD,
    Provenance,
    StatCoverage,
    _double_doubles,
    baseline_bucket,
    baseline_for,
    comparative_sd,
    offensive_share,
    player_uncertainty,
    pool_uncertainty,
    positional_offensive_share,
    resolve_pool,
    untrustworthy_share,
)


def player(stats, positions=("C",), espn_player_id=1, name="Test Player"):
    return PlayerProjection(
        espn_player_id=espn_player_id, name=name, positions=positions, stats=dict(stats)
    )


def by_stat(coverage):
    return {c.stat: c for c in coverage}


class TestExactPassThrough:
    def test_a_projected_stat_is_used_as_is(self):
        players, cov = resolve_pool([player({"ast": 700.0})], {"ast": 1.0})

        assert players[0].stats["ast"] == 700.0
        assert by_stat(cov)["ast"].provenance is Provenance.EXACT
        assert by_stat(cov)["ast"].trustworthy

    def test_stats_the_league_does_not_score_are_left_alone(self):
        """Resolving must not invent work for stats nobody is paid for."""
        players, cov = resolve_pool([player({"ast": 700.0, "blk": 50.0})], {"ast": 1.0})

        assert [c.stat for c in cov] == ["ast"]
        assert players[0].stats["blk"] == 50.0


class TestExactDerivations:
    @pytest.mark.parametrize(
        ("stat", "stats", "expected"),
        [
            ("fgmi", {"fga": 1000.0, "fgm": 400.0}, 600.0),
            ("ftmi", {"fta": 500.0, "ftm": 420.0}, 80.0),
            ("3pmi", {"3pa": 300.0, "3pm": 120.0}, 180.0),
            # 2 points per non-three plus 3 per three plus free throws.
            ("pts", {"fgm": 500.0, "3pm": 100.0, "ftm": 200.0}, 2 * 400 + 3 * 100 + 200),
            ("reb", {"oreb": 200.0, "dreb": 600.0}, 800.0),
        ],
    )
    def test_derivation(self, stat, stats, expected):
        players, cov = resolve_pool([player(stats)], {stat: 1.0})

        assert players[0].stats[stat] == expected
        assert by_stat(cov)[stat].provenance is Provenance.EXACT

    def test_a_derivation_missing_an_input_is_not_invented(self):
        """fgmi needs fga AND fgm; one alone must not silently become zero."""
        players, cov = resolve_pool([player({"fgm": 400.0})], {"fgmi": 1.0})

        assert "fgmi" not in players[0].stats
        assert by_stat(cov)["fgmi"].provenance is Provenance.UNAVAILABLE


class TestReboundEstimate:
    def test_rebounds_are_split_by_position(self):
        """The bug in one test: reb present, oreb/dreb scored, neither projected."""
        players, cov = resolve_pool(
            [player({"reb": 1000.0}, positions=("C",))], {"oreb": 2.0, "dreb": 1.0}
        )

        assert players[0].stats["oreb"] == pytest.approx(1000 * OFFENSIVE_REBOUND_SHARE["C"])
        assert players[0].stats["dreb"] == pytest.approx(1000 * (1 - OFFENSIVE_REBOUND_SHARE["C"]))
        assert by_stat(cov)["oreb"].provenance is Provenance.ESTIMATED

    def test_the_split_always_conserves_total_rebounds(self):
        players, _ = resolve_pool(
            [player({"reb": 873.0}, positions=("PG", "SG"))], {"oreb": 1.0, "dreb": 1.0}
        )

        assert players[0].stats["oreb"] + players[0].stats["dreb"] == pytest.approx(873.0)

    def test_guards_get_a_smaller_offensive_share_than_centres(self):
        """Measured over 290 real players; the gradient is the reason this beats
        one flat share, so it is worth a test rather than a comment."""
        share = positional_offensive_share
        assert share(("PG",)) < share(("SF",)) < share(("C",))

    def test_a_multi_position_player_blends_the_shares(self):
        blended = positional_offensive_share(("PG", "C"))

        assert blended == pytest.approx(
            (OFFENSIVE_REBOUND_SHARE["PG"] + OFFENSIVE_REBOUND_SHARE["C"]) / 2
        )

    def test_an_unknown_position_falls_back_to_the_pool_median(self):
        assert positional_offensive_share(("XX",)) == DEFAULT_OFFENSIVE_SHARE

    def test_no_history_is_exactly_the_positional_split(self):
        """The fallback the whole change rests on: a rookie, or a pool synced
        before history existed, is valued exactly as before."""
        assert offensive_share(player({}, positions=("C",))) == OFFENSIVE_REBOUND_SHARE["C"]

    def test_no_rebounds_projected_means_unavailable_not_zero(self):
        players, cov = resolve_pool([player({"ast": 10.0})], {"oreb": 2.0})

        assert "oreb" not in players[0].stats
        assert by_stat(cov)["oreb"].provenance is Provenance.UNAVAILABLE

    def test_estimate_false_refuses_to_model_anything(self):
        """The setting for asking 'how much of this league can we actually know?'"""
        players, cov = resolve_pool([player({"reb": 900.0})], {"oreb": 2.0}, estimate=False)

        assert "oreb" not in players[0].stats
        assert by_stat(cov)["oreb"].provenance is Provenance.UNAVAILABLE


class TestUnavailable:
    @pytest.mark.parametrize("stat", ["tf", "ej", "ff", "dq", "dd", "td"])
    def test_stats_espn_never_projects(self, stat):
        _, cov = resolve_pool([player({"ast": 10.0})], {stat: 5.0})

        assert by_stat(cov)[stat].provenance is Provenance.UNAVAILABLE
        assert by_stat(cov)[stat].detail  # says WHY, rather than leaving a hole

    def test_double_doubles_say_they_are_merely_unmodelled(self):
        """dd/td are estimable from rates; the report must not imply otherwise."""
        _, cov = resolve_pool([player({"ast": 10.0})], {"dd": 5.0})

        assert "estimable" in by_stat(cov)["dd"].detail


class TestPointShare:
    """Counting stats is useless and weight magnitude is misleading — an ejection
    carries the largest weight in the real league and is worth nothing."""

    def test_share_reflects_contribution_not_weight(self):
        pool = [player({"reb": 1000.0, "ast": 10.0})]

        _, cov = resolve_pool(pool, {"dreb": 1.0, "ast": 1.0, "ej": -8.0})
        shares = by_stat(cov)

        assert shares["dreb"].point_share > shares["ast"].point_share
        assert shares["ej"].point_share == 0.0
        assert shares["ej"].weight == -8.0  # biggest weight, no contribution

    def test_shares_sum_to_one(self):
        _, cov = resolve_pool([player({"reb": 500.0, "ast": 400.0})], {"dreb": 1.0, "ast": 1.5})

        assert sum(c.point_share for c in cov) == pytest.approx(1.0)

    def test_coverage_is_ordered_most_consequential_first(self):
        _, cov = resolve_pool(
            [player({"reb": 1000.0, "ast": 5.0})], {"ast": 1.0, "dreb": 1.0, "tf": 1.0}
        )

        order = [c.stat for c in cov]

        assert order == ["dreb", "ast", "tf"]

    def test_a_pool_scoring_nothing_does_not_divide_by_zero(self):
        _, cov = resolve_pool([player({})], {"tf": 1.0})

        assert all(c.point_share == 0.0 for c in cov)


class TestUntrustworthyShare:
    def test_an_all_exact_league_is_fully_trustworthy(self):
        _, cov = resolve_pool([player({"ast": 400.0, "stl": 100.0})], {"ast": 1.0, "stl": 3.0})

        assert untrustworthy_share(cov) == pytest.approx(0.0)

    def test_estimated_scoring_counts_against_it(self):
        """The real league's shape: rebounds are a third of the scoring and are
        estimated, so a third of the board is a model."""
        _, cov = resolve_pool(
            [player({"reb": 900.0, "ast": 400.0})], {"oreb": 2.0, "dreb": 1.0, "ast": 1.0}
        )

        assert 0.5 < untrustworthy_share(cov) < 0.8

    def test_a_worthless_missing_stat_barely_moves_it(self):
        _, cov = resolve_pool([player({"ast": 400.0})], {"ast": 1.0, "ej": -8.0})

        assert untrustworthy_share(cov) == pytest.approx(0.0)


class TestPoolWideProvenance:
    def test_one_player_missing_an_input_downgrades_the_whole_stat(self):
        """Provenance describes the LEAGUE's valuation, not one row of it: if a
        single player's rebounds had to be estimated, the ranking they sit in is
        estimated too."""
        pool = [
            player({"oreb": 100.0, "dreb": 300.0}, espn_player_id=1),
            player({"reb": 400.0}, espn_player_id=2),
        ]

        _, cov = resolve_pool(pool, {"oreb": 2.0})

        assert by_stat(cov)["oreb"].provenance is Provenance.ESTIMATED

    def test_players_with_no_projections_at_all_are_ignored(self):
        """ESPN projects a few hundred players and skips the rest — 76 of 400 in
        one real league. Those say nothing about whether a stat CODE exists, and
        counting them marked fgm and ast unavailable for a league where both are
        projected for everyone who has projections, reporting 100% of the
        scoring as untrustworthy.
        """
        pool = [
            player({"fgm": 500.0}, espn_player_id=1),
            player({}, espn_player_id=2, name="Deep Bench"),
        ]

        _, cov = resolve_pool(pool, {"fgm": 2.0})

        assert by_stat(cov)["fgm"].provenance is Provenance.EXACT

    def test_one_thin_projection_does_not_veto_the_league(self):
        """The bug this rule replaced.

        A real 400-player pool contained exactly one rookie carrying 9 stat keys
        where everyone else had 31. Taking the worst provenance any single
        player produced marked fgm, ast and blk UNAVAILABLE for the whole
        league and reported 86% of scoring as untrustworthy, when the true
        figure was 15%. A thin projection is a fact about that player, not
        evidence that a stat code does not exist.
        """
        pool = [player({"fgm": 500.0, "ast": 300.0}, espn_player_id=i) for i in range(20)]
        pool.append(player({"gp": 40.0}, espn_player_id=99, name="Thin Rookie"))

        _, cov = resolve_pool(pool, {"fgm": 2.0, "ast": 1.0})

        assert by_stat(cov)["fgm"].provenance is Provenance.EXACT
        assert by_stat(cov)["ast"].provenance is Provenance.EXACT

    def test_a_tie_breaks_toward_the_worse_provenance(self):
        """A genuinely split pool is reported pessimistically."""
        pool = [
            player({"oreb": 100.0, "dreb": 300.0}, espn_player_id=1),
            player({"reb": 400.0}, espn_player_id=2),
        ]

        _, cov = resolve_pool(pool, {"oreb": 2.0})

        assert by_stat(cov)["oreb"].provenance is Provenance.ESTIMATED

    def test_a_pool_of_nothing_but_unprojected_players_still_reports(self):
        _, cov = resolve_pool([player({}), player({})], {"fgm": 2.0})

        assert by_stat(cov)["fgm"].provenance is Provenance.UNAVAILABLE
        assert by_stat(cov)["fgm"].detail


# --------------------------------------------------------------------------- #
# Uncertainty bands
# --------------------------------------------------------------------------- #
class TestUncertainty:
    WEIGHTS: ClassVar[dict[str, float]] = {"pts": 1.0, "reb": 1.0, "oreb": 2.0, "dreb": 1.0}
    PROV: ClassVar[dict[str, Provenance]] = {
        "pts": Provenance.EXACT,
        "reb": Provenance.EXACT,
        "oreb": Provenance.ESTIMATED,
        "dreb": Provenance.ESTIMATED,
    }

    def _p(self, **stats):
        return PlayerProjection(espn_player_id=1, name="P", positions=("C",), stats=stats)

    def test_a_player_on_measured_stats_only_gets_the_baseline(self):
        u = player_uncertainty(self._p(pts=1000.0), self.WEIGHTS, self.PROV)
        assert u.model_share == 0.0
        assert u.model_points_sd == 0.0
        assert u.relative_sd == pytest.approx(baseline_for(self._p(pts=1000.0)))

    def test_the_baseline_widens_the_band_but_not_the_model_part(self):
        """The confident sort reads model_sd; a player's baseline must not leak
        into it, or a volatile role would be double-charged as model error."""
        player = self._p(pts=500.0, reb=500.0, oreb=150.0, dreb=350.0)
        tight = player_uncertainty(player, self.WEIGHTS, self.PROV, baseline=lambda _: 0.2)
        wide = player_uncertainty(player, self.WEIGHTS, self.PROV, baseline=lambda _: 0.6)

        assert wide.points_sd > tight.points_sd
        assert wide.model_points_sd == pytest.approx(tight.model_points_sd)

    def test_estimated_stats_widen_the_band(self):
        clean = player_uncertainty(self._p(pts=1000.0), self.WEIGHTS, self.PROV)
        modelled = player_uncertainty(
            self._p(pts=500.0, oreb=100.0, dreb=300.0), self.WEIGHTS, self.PROV
        )
        assert modelled.model_share > 0
        assert modelled.relative_sd > clean.relative_sd

    def test_the_band_scales_with_how_much_the_model_carries(self):
        """A centre whose score leans on inferred rebounds is less knowable
        than a guard whose score is measured end to end."""
        guard = player_uncertainty(
            self._p(pts=900.0, oreb=10.0, dreb=60.0), self.WEIGHTS, self.PROV
        )
        centre = player_uncertainty(
            self._p(pts=300.0, oreb=150.0, dreb=400.0), self.WEIGHTS, self.PROV
        )
        assert centre.model_share > guard.model_share
        assert centre.relative_sd > guard.relative_sd

    def test_offensive_rebounds_are_costlier_than_defensive_ones(self):
        """Measured: the oreb split errs at sd 40.1%, dreb at 15.1%. Equal
        point contributions must therefore not widen the band equally."""
        off = player_uncertainty(self._p(pts=500.0, oreb=100.0), self.WEIGHTS, self.PROV)
        deff = player_uncertainty(self._p(pts=500.0, dreb=200.0), self.WEIGHTS, self.PROV)
        assert off.model_share == pytest.approx(deff.model_share)
        assert off.model_points_sd > deff.model_points_sd

    def test_an_unmeasured_estimator_is_treated_pessimistically(self):
        """An estimator nobody has backtested should widen the band MORE than
        one that has been, not less."""
        weights = {"pts": 1.0, "td": 7.0}
        prov = {"pts": Provenance.EXACT, "td": Provenance.ESTIMATED}
        u = player_uncertainty(self._p(pts=500.0, td=20.0), weights, prov)
        assert u.model_points_sd > 0
        assert UNMEASURED_ESTIMATOR_SD > max(ESTIMATOR_RELATIVE_SD.values())

    def test_points_sd_is_the_relative_band_in_points(self):
        u = player_uncertainty(self._p(pts=1000.0), self.WEIGHTS, self.PROV)
        assert u.points_sd == pytest.approx(1000.0 * u.relative_sd)
        assert u.low == -u.high

    def test_a_player_who_scores_nothing_has_no_band(self):
        u = player_uncertainty(self._p(), self.WEIGHTS, self.PROV)
        assert u.points_sd == 0.0

    def test_pool_uncertainty_keys_by_espn_player_id(self):
        pool = [
            PlayerProjection(espn_player_id=7, name="A", positions=("C",), stats={"pts": 100.0}),
            PlayerProjection(espn_player_id=9, name="B", positions=("PG",), stats={"pts": 200.0}),
        ]
        coverage = [
            StatCoverage("pts", 1.0, Provenance.EXACT, "projected directly", 1.0),
        ]
        got = pool_uncertainty(pool, {"pts": 1.0}, coverage)
        assert set(got) == {7, 9}
        assert got[9].points_sd > got[7].points_sd

    def test_both_bands_are_in_points_not_fractions(self):
        """They are stored beside the value and compared against it, so a
        fraction in one of them reads as "no uncertainty" rather than as a
        unit error. Caught exactly that way once."""
        u = player_uncertainty(self._p(pts=500.0, oreb=150.0, dreb=400.0), self.WEIGHTS, self.PROV)
        points = 500.0 + 150.0 * 2.0 + 400.0 * 1.0
        assert u.points_sd == pytest.approx(u.relative_sd * points)
        # In points, so both are large numbers on a 1200-point projection.
        assert u.points_sd > 100
        assert u.model_points_sd > 10
        assert u.model_points_sd < u.points_sd


class TestDoubleDoubles:
    """Estimating a stat ESPN scores but never projects."""

    WEIGHTS: ClassVar[dict[str, float]] = {"dd": 5.0, "td": 7.0}

    def _p(self, gp, **per_game):
        return PlayerProjection(
            espn_player_id=1,
            name="P",
            positions=("C",),
            stats={"gp": gp, **{k: v * gp for k, v in per_game.items()}},
        )

    def test_a_nightly_double_double_is_projected_most_games(self):
        big = self._p(70, pts=22.0, reb=12.0, ast=3.0, stl=0.9, blk=1.0)
        assert _double_doubles(big, "dd") > 45

    def test_a_player_who_fills_one_column_posts_almost_none(self):
        scorer = self._p(70, pts=24.0, reb=3.0, ast=2.0, stl=0.8, blk=0.2)
        assert _double_doubles(scorer, "dd") < 3

    def test_triple_doubles_are_rarer_than_double_doubles(self):
        """A triple-double is also a double-double — measured, not assumed:
        across 353 players with 2026 actuals, none had td > dd."""
        allrounder = self._p(70, pts=27.0, reb=12.0, ast=10.0, stl=1.4, blk=0.8)
        assert _double_doubles(allrounder, "td") < _double_doubles(allrounder, "dd")

    def test_a_triple_double_threat_gets_a_real_count(self):
        jokic = self._p(72, pts=28.2, reb=12.7, ast=10.1, stl=1.6, blk=0.8)
        assert 20 < _double_doubles(jokic, "td") < 55

    def test_it_scales_with_games_played(self):
        rate = {"pts": 22.0, "reb": 12.0, "ast": 3.0, "stl": 0.9, "blk": 1.0}
        half = _double_doubles(self._p(40, **rate), "dd")
        full = _double_doubles(self._p(80, **rate), "dd")
        assert full == pytest.approx(half * 2)

    def test_a_player_with_no_games_cannot_be_estimated(self):
        assert _double_doubles(self._p(0, pts=20.0, reb=11.0), "dd") is None

    def test_a_missing_category_declines_rather_than_guessing(self):
        """Every category is needed; scoring a missing one as zero would
        quietly report a rebounder as never posting a double-double."""
        partial = PlayerProjection(
            espn_player_id=1, name="P", positions=("C",), stats={"gp": 70, "pts": 1500}
        )
        assert _double_doubles(partial, "dd") is None

    def test_resolve_pool_now_fills_them_instead_of_zeroing(self):
        pool = [
            PlayerProjection(
                espn_player_id=1,
                name="Big",
                positions=("C",),
                stats={"gp": 70, "pts": 1540, "reb": 840, "ast": 210, "stl": 63, "blk": 70},
            )
        ]
        resolved, coverage = resolve_pool(pool, self.WEIGHTS)
        assert resolved[0].stats["dd"] > 0
        provenance = {c.stat: c.provenance for c in coverage}
        assert provenance["dd"] is Provenance.ESTIMATED
        assert provenance["td"] is Provenance.ESTIMATED

    def test_the_band_widens_for_players_who_lean_on_it(self):
        pool = [
            PlayerProjection(
                espn_player_id=1,
                name="Big",
                positions=("C",),
                stats={"gp": 70, "pts": 1540, "reb": 840, "ast": 210, "stl": 63, "blk": 70},
            )
        ]
        resolved, coverage = resolve_pool(pool, self.WEIGHTS)
        band = pool_uncertainty(resolved, self.WEIGHTS, coverage)[1]
        assert band.model_share > 0.9  # this league scores nothing else
        assert band.model_points_sd > 0


def with_history(history, positions=("C",)):
    return PlayerProjection(
        espn_player_id=1, name="Test Big", positions=positions, stats={}, history=history
    )


class TestOwnOffensiveShare:
    """A player's own prior split, shrunk toward their position's by sample
    size: (oreb + k * positional) / (reb + k)."""

    K = OREB_SHARE_PRIOR_REBOUNDS
    C = OFFENSIVE_REBOUND_SHARE["C"]

    def test_the_formula(self):
        share = offensive_share(with_history({2026: {"oreb": 200.0, "reb": 600.0}}))
        assert share == pytest.approx((200 + self.K * self.C) / (600 + self.K))

    def test_a_big_sample_is_mostly_the_players_own(self):
        """A centre who rebounds offensively far above centres as a whole:
        with three full seasons behind them the prior should barely move them."""
        seasons = {y: {"oreb": 300.0, "reb": 750.0} for y in (2024, 2025, 2026)}
        assert offensive_share(with_history(seasons)) == pytest.approx(0.4, abs=0.01)

    def test_a_small_sample_is_mostly_the_position(self):
        tiny = offensive_share(with_history({2026: {"oreb": 10.0, "reb": 12.0}}))
        assert abs(tiny - self.C) < abs(10 / 12 - self.C) / 2

    def test_seasons_pool_rather_than_average(self):
        """Rebounds are the sample size, so a 700-rebound season outweighs a
        70-rebound cameo instead of counting the same."""
        history = {2026: {"oreb": 70.0, "reb": 700.0}, 2025: {"oreb": 35.0, "reb": 70.0}}
        share = offensive_share(with_history(history), prior_rebounds=0.0)
        assert share == pytest.approx(105 / 770)

    def test_only_the_most_recent_seasons_count(self):
        recent = {2026 - i: {"oreb": 100.0, "reb": 500.0} for i in range(OREB_SHARE_SEASONS)}
        stale = {2026 - OREB_SHARE_SEASONS: {"oreb": 500.0, "reb": 500.0}}
        share = offensive_share(with_history(recent | stale), prior_rebounds=0.0)
        assert share == pytest.approx(0.2)

    @pytest.mark.parametrize(
        "line",
        [None, {}, {"gp": 0.0}, {"reb": 0.0, "oreb": 0.0}],
        ids=["not in the NBA", "listed, no line", "no games", "no rebounds"],
    )
    def test_an_empty_season_contributes_nothing(self, line):
        assert offensive_share(with_history({2026: line})) == pytest.approx(self.C)

    def test_the_split_uses_the_players_own_share(self):
        big = with_history({2026: {"oreb": 300.0, "reb": 750.0}})
        big = PlayerProjection(
            espn_player_id=1,
            name="Big",
            positions=("C",),
            stats={"reb": 1000.0},
            history=big.history,
        )
        players, _ = resolve_pool([big], {"oreb": 2.0, "dreb": 1.0})

        share = offensive_share(big)
        assert players[0].stats["oreb"] == pytest.approx(1000 * share)
        assert players[0].stats["oreb"] > 1000 * self.C
        assert players[0].stats["oreb"] + players[0].stats["dreb"] == pytest.approx(1000.0)


def prospect(mpg=None, history=None, **stats):
    if mpg is not None:
        stats["mpg"] = mpg
    return PlayerProjection(
        espn_player_id=1, name="P", positions=("SF",), stats=stats, history=history or {}
    )


class TestBaselineBuckets:
    """baseline_for replaces one pool-wide band with one per kind of projection.
    The widths are fitted (scripts/calibration/uncertainty.py); what is pinned
    here is which player lands in which bucket, and the ordering the data
    showed every season."""

    def test_fewer_minutes_means_a_wider_band(self):
        assert baseline_for(prospect(34)) < baseline_for(prospect(27)) < baseline_for(prospect(18))

    def test_a_short_season_widens_a_rotation_player(self):
        short = prospect(20, {2026: {"gp": 31.0}})
        full = prospect(20, {2026: {"gp": 70.0}})
        assert baseline_for(short) > baseline_for(full)

    def test_but_not_a_heavy_minutes_one(self):
        assert baseline_bucket(prospect(34, {2026: {"gp": 31.0}})) == "mpg 30+"

    def test_only_the_most_recent_season_counts(self):
        history = {2026: {"gp": 70.0}, 2025: {"gp": 10.0}}
        assert baseline_bucket(prospect(20, history)) == "mpg <24"

    def test_a_whole_season_missed_is_short(self):
        """{} is listed-but-no-line: the season lost entirely."""
        assert baseline_bucket(prospect(26, {2026: {}})) == "mpg <30 short"

    def test_a_rookie_is_not_penalised(self):
        """No previous season is not evidence of games missed, and rookies'
        errors measured narrower than veterans' at the same minutes."""
        rookie = prospect(20, {2026: None, 2025: None})
        veteran = prospect(20, {2026: {"gp": 72.0}})
        assert baseline_for(rookie) == baseline_for(veteran)

    def test_no_synced_history_is_not_a_short_season(self):
        assert baseline_bucket(prospect(20)) == "mpg <24"

    def test_minutes_fall_back_to_total_over_games(self):
        assert baseline_bucket(prospect(min=2400.0, gp=75.0)) == "mpg 30+"

    def test_no_minutes_at_all_gets_the_widest_full_season_band(self):
        assert baseline_bucket(prospect()) == "mpg <24"

    def test_every_bucket_has_a_width(self):
        buckets = {
            baseline_bucket(p)
            for p in (prospect(34), prospect(27), prospect(18), prospect(18, {2026: {"gp": 5.0}}))
        }
        assert buckets == set(BASELINE_BUCKETS)


class TestComparativeSd:
    """What the confident sort subtracts: the band minus the floor everyone
    shares, which is the only part of it that cancels in a comparison."""

    def test_a_starter_on_measured_stats_has_nothing_to_distinguish(self):
        assert comparative_sd(BASELINE_FLOOR * 2000, 2000) == pytest.approx(0.0)

    def test_a_wider_baseline_leaves_its_excess(self):
        sd = comparative_sd(0.6 * 1000, 1000)
        assert sd == pytest.approx(1000 * (0.6**2 - BASELINE_FLOOR**2) ** 0.5)

    def test_matches_the_model_part_for_a_floor_player(self):
        """A starter whose score is part-modelled: exactly the model sd."""
        weights = {"pts": 1.0, "oreb": 2.0}
        prov = {"pts": Provenance.EXACT, "oreb": Provenance.ESTIMATED}
        big = PlayerProjection(
            espn_player_id=1,
            name="B",
            positions=("C",),
            stats={"pts": 1500.0, "oreb": 200.0, "mpg": 34.0},
        )
        u = player_uncertainty(big, weights, prov)
        assert comparative_sd(u.points_sd, 1900.0) == pytest.approx(u.model_points_sd)

    def test_never_negative_for_a_band_below_the_floor(self):
        """Rows computed before per-player baselines, or a caller's own baseline."""
        assert comparative_sd(10.0, 1000) == 0.0
