"""Reconcile what ESPN projects with what a league actually scores.

project_points looks each weighted stat up with `stats.get(stat, 0.0)`, and that
default silently conflates two unrelated situations:

    this league does not score rebounds      -> 0.0 is correct
    ESPN did not project rebounds by that name -> 0.0 is a LIE

Which is not hypothetical. Of ESPN's 46 scoreable stat codes only 31 are ever
projected; the other 15 (oreb, dreb, dd, td, tf, ej, ff, dq, pf, gs, …) can
never be looked up directly. A league scoring any of them gets a silently wrong
valuation. Measured against one real 10-team league that scores oreb 2.0 and
dreb 1.0: rebounding contributed exactly nothing, and correcting it moved nine
of the top twenty players — Rudy Gobert by 69 places.

So this module sits between the adapter and the engine and answers, for each
stat the league scores, "can we know this?" in one of three ways:

    EXACT        identity, or lossless arithmetic over projected inputs
    ESTIMATED    a model stands in for data ESPN does not project
    UNAVAILABLE  no input exists at any price; 0.0 is the honest answer

Nothing here touches the database, the network or FastAPI — same rule as
engine.py, so it is testable against hand-computed numbers.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from .domain import PlayerProjection


class Provenance(str, Enum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StatCoverage:
    """How one scored stat was obtained, and how much it matters.

    `point_share` is the fraction of the pool's total ABSOLUTE scoring this stat
    accounts for, and it is the number worth reading. Counting stats is
    useless — "7 of 17 missing" says nothing. Weight magnitude is barely better:
    an ejection is weighted -8.0, the largest in that real league, and is worth
    nothing because nobody is ejected. Rebounds at 1.0-2.0 are a third of a
    centre's score.
    """

    stat: str
    weight: float
    provenance: Provenance
    detail: str
    point_share: float

    @property
    def trustworthy(self) -> bool:
        return self.provenance is Provenance.EXACT


# --------------------------------------------------------------------------- #
# EXACT: lossless arithmetic over stats ESPN does project.
# --------------------------------------------------------------------------- #

Derivation = tuple[tuple[str, ...], Callable[[Mapping[str, float]], float], str]

DERIVATIONS: dict[str, Derivation] = {
    "fgmi": (("fga", "fgm"), lambda s: s["fga"] - s["fgm"], "fga - fgm"),
    "ftmi": (("fta", "ftm"), lambda s: s["fta"] - s["ftm"], "fta - ftm"),
    "3pmi": (("3pa", "3pm"), lambda s: s["3pa"] - s["3pm"], "3pa - 3pm"),
    "pts": (
        ("fgm", "3pm", "ftm"),
        lambda s: 2 * (s["fgm"] - s["3pm"]) + 3 * s["3pm"] + s["ftm"],
        "2*(fgm - 3pm) + 3*3pm + ftm",
    ),
    "reb": (("oreb", "dreb"), lambda s: s["oreb"] + s["dreb"], "oreb + dreb"),
}


# --------------------------------------------------------------------------- #
# ESTIMATED: a model, stated out loud.
# --------------------------------------------------------------------------- #

# Offensive share of total rebounds, by position. Measured over 290 players with
# a complete prior season in one real league: the gradient is exactly what
# basketball sense predicts, and using it rather than one flat share cuts mean
# absolute error from 0.0776 to 0.0675 — about 13%.
#
# ESPN publishes OREB/DREB for COMPLETED seasons but projects only REB, so this
# gap cannot be closed with projection data alone. Apportioning a player's own
# projected rebounds by their own prior-season split would be better still; it
# needs a second season fetch in the adapter, which this deliberately does not do.
OFFENSIVE_REBOUND_SHARE: dict[str, float] = {
    "PG": 0.178,
    "SG": 0.211,
    "SF": 0.228,
    "PF": 0.247,
    "C": 0.318,
}
# Pool-wide median, for a player with no usable position.
DEFAULT_OFFENSIVE_SHARE = 0.246


def offensive_share(positions: Iterable[str]) -> float:
    """Blend the per-position shares for a multi-eligible player."""
    known = [
        OFFENSIVE_REBOUND_SHARE[p.upper()]
        for p in positions
        if p.upper() in OFFENSIVE_REBOUND_SHARE
    ]
    return sum(known) / len(known) if known else DEFAULT_OFFENSIVE_SHARE


def _split_rebounds(player: PlayerProjection, stat: str) -> float | None:
    total = player.stats.get("reb")
    if total is None:
        return None
    share = offensive_share(player.positions)
    return total * share if stat == "oreb" else total * (1.0 - share)


Estimator = tuple[Callable[[PlayerProjection, str], float | None], str]

ESTIMATORS: dict[str, Estimator] = {
    "oreb": (_split_rebounds, "reb x position offensive share"),
    "dreb": (_split_rebounds, "reb x (1 - position offensive share)"),
}

# Stats no model here attempts. Listed rather than merely absent, so the report
# can say WHY instead of leaving a silent hole: double-doubles and
# triple-doubles are estimable from per-game rates and simply are not estimated
# yet; the rest have no projected input of any kind.
UNMODELLED: dict[str, str] = {
    "dd": "not projected; estimable from ppg/rpg/apg rates, not attempted",
    "td": "not projected; estimable from ppg/rpg/apg rates, not attempted",
    "tf": "not projected and not estimable",
    "ej": "not projected and not estimable",
    "ff": "not projected and not estimable",
    "dq": "not projected and not estimable",
    "pf": "not projected and not estimable",
    "gs": "not projected and not estimable",
}


def _resolve_one(
    player: PlayerProjection, needed: Iterable[str], estimate: bool
) -> tuple[dict[str, float], dict[str, Provenance], dict[str, str]]:
    stats = dict(player.stats)
    provenance: dict[str, Provenance] = {}
    detail: dict[str, str] = {}

    for stat in needed:
        if stat in stats:
            provenance[stat] = Provenance.EXACT
            detail[stat] = "projected directly"
            continue

        rule = DERIVATIONS.get(stat)
        if rule is not None:
            inputs, fn, expr = rule
            if all(i in stats for i in inputs):
                stats[stat] = fn(stats)
                provenance[stat] = Provenance.EXACT
                detail[stat] = f"derived: {expr}"
                continue

        estimator = ESTIMATORS.get(stat) if estimate else None
        if estimator is not None:
            fn_est, how = estimator
            value = fn_est(player, stat)
            if value is not None:
                stats[stat] = value
                provenance[stat] = Provenance.ESTIMATED
                detail[stat] = f"estimated: {how}"
                continue

        provenance[stat] = Provenance.UNAVAILABLE
        detail[stat] = UNMODELLED.get(stat, "no projected input and no estimator")

    return stats, provenance, detail


def resolve_pool(
    players: Iterable[PlayerProjection],
    weights: Mapping[str, float],
    *,
    estimate: bool = True,
) -> tuple[list[PlayerProjection], list[StatCoverage]]:
    """Fill in every stat the league scores, and report how each was obtained.

    Returns the pool with resolved stats plus a coverage report ordered by how
    much of the scoring each stat accounts for — most consequential first, so
    the first line a human reads is the one that matters.

    `estimate=False` turns the models off, which is the setting to use when the
    question is "how much of this league can we actually know?"
    """
    players = list(players)
    needed = list(weights)

    resolved: list[PlayerProjection] = []
    provenance: dict[str, Provenance] = {}
    detail: dict[str, str] = {}
    contribution: dict[str, float] = dict.fromkeys(needed, 0.0)

    # Per stat, how many projected players resolved it each way, and one detail
    # string per way. Provenance is decided by MAJORITY, not by veto — see below.
    votes: dict[str, Counter[Provenance]] = {stat: Counter() for stat in needed}
    details: dict[str, dict[Provenance, str]] = {stat: {} for stat in needed}

    for player in players:
        stats, prov, det = _resolve_one(player, needed, estimate)
        resolved.append(replace(player, stats=stats))

        for stat in needed:
            contribution[stat] += abs(stats.get(stat, 0.0) * weights[stat])

        # Players ESPN projects NOTHING for say nothing about the stat
        # vocabulary — they are deep-bench names the projections skip, and a
        # real pool is full of them (76 of 400 in one live league).
        if not player.stats:
            continue

        for stat in needed:
            votes[stat][prov[stat]] += 1
            details[stat].setdefault(prov[stat], det[stat])

    # Majority, not veto. The first version took the WORST provenance any single
    # player produced, and one rookie — Noa Essengue, carrying 9 stat keys where
    # everyone else had 31 — marked fgm, ast, blk and the rest UNAVAILABLE for a
    # whole 400-player league, reporting 86% of the scoring as untrustworthy
    # when the true figure was 15%. A thin projection for one player is a fact
    # about that player; it is not evidence that a stat code does not exist.
    #
    # Ties break toward the worse provenance, so a genuinely split pool is
    # reported pessimistically rather than optimistically.
    rank = {Provenance.EXACT: 0, Provenance.ESTIMATED: 1, Provenance.UNAVAILABLE: 2}
    for stat in needed:
        if not votes[stat]:
            # Nobody in the pool had any projections at all.
            provenance[stat] = Provenance.UNAVAILABLE
            detail[stat] = "no player in this pool has any projected stats"
            continue
        top = max(votes[stat].values())
        winner = max((p for p, n in votes[stat].items() if n == top), key=lambda p: rank[p])
        provenance[stat] = winner
        detail[stat] = details[stat][winner]

    total = sum(contribution.values())
    coverage = [
        StatCoverage(
            stat=stat,
            weight=weights[stat],
            provenance=provenance[stat],
            detail=detail[stat],
            point_share=(contribution[stat] / total) if total else 0.0,
        )
        for stat in needed
    ]
    coverage.sort(key=lambda c: (-c.point_share, c.stat))
    return resolved, coverage


def untrustworthy_share(coverage: Iterable[StatCoverage]) -> float:
    """Fraction of scoring that is estimated or missing outright.

    The one number worth alarming on: it answers "how much of this league's
    scoring am I guessing at?" rather than "how many stat codes are absent".
    """
    return sum(c.point_share for c in coverage if not c.trustworthy)


# --------------------------------------------------------------------------- #
# How wrong is this number likely to be?
# --------------------------------------------------------------------------- #
#
# A value with no error bar invites a precision it does not have. Every
# constant below was MEASURED against ESPN's own 2026 projections and 2026
# actuals, both of which ride in the same kona_player_info request the adapter
# already makes — so these are backtests, not priors somebody liked the look
# of. Re-measure them when a season ends; the script lives in the commit that
# added this block.
#
#   BASELINE_RELATIVE_SD    season-total fantasy points, projected vs actual,
#                           n=321, sd 36.7% around the mean error. Decomposes
#                           into a per-game rate error (sd 26.2%) and an
#                           availability error (sd ~29%), which compose to
#                           38.9% — close enough to the directly measured
#                           36.7% to trust the decomposition.
#
#   ESTIMATOR_RELATIVE_SD   each estimator's OWN error, isolated by splitting
#                           ACTUAL rebounds and comparing against actual
#                           oreb/dreb, so ESPN's projection error is not
#                           double-counted. oreb n=292 sd 40.1%; dreb n=342
#                           sd 15.1%. Offensive rebounds are far harder to
#                           infer from a total than defensive ones, which is
#                           why they get their own number.
BASELINE_RELATIVE_SD = 0.367
ESTIMATOR_RELATIVE_SD: dict[str, float] = {"oreb": 0.401, "dreb": 0.151}
# An estimator nobody has backtested yet. Deliberately pessimistic: an
# unmeasured model should widen the band more than a measured one, not less.
UNMEASURED_ESTIMATOR_SD = 0.50
# An UNAVAILABLE stat is scored as 0.0, so the whole of its true contribution
# is missing rather than merely mis-sized. There is no share of it in the
# player's points to scale, which is why it cannot be handled here — see
# `unavailable_share` on the coverage report instead.


@dataclass(frozen=True)
class ValueUncertainty:
    """How much confidence one player's projected total deserves.

    `relative_sd` is the whole band; `model_sd` is the part contributed by
    this app's own estimators rather than by ESPN.

    The split matters more than the total. The baseline is common to every
    player — "projections are hard" — so it very largely cancels when you
    compare two players, which is the only thing a draft board is for. The
    model component does NOT cancel: it depends on a player's stat mix, so a
    rebounding centre whose score is 40% inferred is genuinely less knowable
    than a guard whose score is measured end to end, and that difference
    survives the comparison.
    """

    # Both sds are in POINTS, not fractions. They get stored in the same
    # column family as the value itself and compared against it, and a mixed
    # pair of units there is the kind of bug that reads as "no uncertainty"
    # rather than as an error.
    relative_sd: float
    points_sd: float
    model_points_sd: float
    model_share: float

    @property
    def low(self) -> float:
        return -self.points_sd

    @property
    def high(self) -> float:
        return self.points_sd


def player_uncertainty(
    player: PlayerProjection,
    weights: Mapping[str, float],
    provenance: Mapping[str, Provenance],
    baseline: float = BASELINE_RELATIVE_SD,
) -> ValueUncertainty:
    """The error bar on one player's projected points.

    Estimated stats add variance in proportion to how much of THIS player's
    scoring they carry, so the same league gives a guard a tighter band than a
    centre. Added in quadrature, which assumes the estimators' errors are
    independent of ESPN's — they are computed from different inputs by
    different people, so that is the reasonable default.

    The band applies unchanged to value as well as to points: value is
    points minus a replacement level, and that level is a pool-wide order
    statistic, far better determined than any single projection.
    """
    total = sum(abs(player.stats.get(stat, 0.0) * weight) for stat, weight in weights.items())
    if total <= 0:
        return ValueUncertainty(baseline, 0.0, 0.0, 0.0)

    model_variance = 0.0
    model_points = 0.0
    for stat, weight in weights.items():
        if provenance.get(stat) is not Provenance.ESTIMATED:
            continue
        contribution = abs(player.stats.get(stat, 0.0) * weight)
        if not contribution:
            continue
        model_points += contribution
        sd = ESTIMATOR_RELATIVE_SD.get(stat, UNMEASURED_ESTIMATOR_SD)
        model_variance += (contribution / total * sd) ** 2

    relative = math.sqrt(baseline**2 + model_variance)
    points = sum(player.stats.get(stat, 0.0) * weight for stat, weight in weights.items())
    return ValueUncertainty(
        relative_sd=relative,
        points_sd=relative * abs(points),
        model_points_sd=math.sqrt(model_variance) * abs(points),
        model_share=model_points / total,
    )


def pool_uncertainty(
    players: Iterable[PlayerProjection],
    weights: Mapping[str, float],
    coverage: Iterable[StatCoverage],
    baseline: float = BASELINE_RELATIVE_SD,
) -> dict[int, ValueUncertainty]:
    """player_uncertainty for a whole pool, keyed by espn_player_id."""
    provenance = {c.stat: c.provenance for c in coverage}
    return {p.espn_player_id: player_uncertainty(p, weights, provenance, baseline) for p in players}
