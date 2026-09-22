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
    BASELINE_RELATIVE_SD,
    DEFAULT_OFFENSIVE_SHARE,
    ESTIMATOR_RELATIVE_SD,
    OFFENSIVE_REBOUND_SHARE,
    UNMEASURED_ESTIMATOR_SD,
    Provenance,
    StatCoverage,
    offensive_share,
    player_uncertainty,
    pool_uncertainty,
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
        assert offensive_share(("PG",)) < offensive_share(("SF",)) < offensive_share(("C",))

    def test_a_multi_position_player_blends_the_shares(self):
        blended = offensive_share(("PG", "C"))

        assert blended == pytest.approx(
            (OFFENSIVE_REBOUND_SHARE["PG"] + OFFENSIVE_REBOUND_SHARE["C"]) / 2
        )

    def test_an_unknown_position_falls_back_to_the_pool_median(self):
        assert offensive_share(("XX",)) == DEFAULT_OFFENSIVE_SHARE

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
        assert u.relative_sd == pytest.approx(BASELINE_RELATIVE_SD)

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
