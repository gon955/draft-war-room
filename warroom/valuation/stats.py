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
# gap cannot be closed with projection data alone. This table is now the PRIOR
# for offensive_share below, and the whole answer only for a player with no
# rebounding history.
OFFENSIVE_REBOUND_SHARE: dict[str, float] = {
    "PG": 0.178,
    "SG": 0.211,
    "SF": 0.228,
    "PF": 0.247,
    "C": 0.318,
}
# Pool-wide median, for a player with no usable position.
DEFAULT_OFFENSIVE_SHARE = 0.246


def positional_offensive_share(positions: Iterable[str]) -> float:
    """Blend the per-position shares for a multi-eligible player."""
    known = [
        OFFENSIVE_REBOUND_SHARE[p.upper()]
        for p in positions
        if p.upper() in OFFENSIVE_REBOUND_SHARE
    ]
    return sum(known) / len(known) if known else DEFAULT_OFFENSIVE_SHARE


# A player's own offensive share, shrunk toward their position's:
#
#     share = (oreb + k * positional) / (reb + k)
#
# which is (n * own + k * prior) / (n + k) with n counted in rebounds, so the
# pull toward the position fades exactly as the sample grows. Fitted by
# scripts/calibration/oreb_share.py, both chosen on 2025 alone and judged on
# seasons the fit never saw. Share MAE over players with 100+ rebounds:
#
#                   positional only    this
#     2026 held out      0.0682       0.0430   (37% lower)
#     2024 held out      0.0647       0.0432   (33% lower)
#
# k=50 is about a third of a starting big's season: a player with 600 career
# rebounds is ~92% their own number, a two-way player with 40 is mostly their
# position's. Depth 2 and 3 were within 0.001 of each other on every season;
# 3 won the fit season and is kept rather than re-picked on held-out data.
OREB_SHARE_PRIOR_REBOUNDS = 50.0
OREB_SHARE_SEASONS = 3


def offensive_share(
    player: PlayerProjection,
    prior_rebounds: float | None = None,
    seasons: int | None = None,
) -> float:
    """This player's expected offensive share of their rebounds.

    Pools the most recent `seasons` of history. A season the player missed,
    spent outside the NBA or that was never fetched simply contributes no
    rebounds, so a player with no history at all gets the positional share
    unchanged — today's behaviour, and never worse than it.

    The two keyword arguments exist for the calibration script, which sweeps
    them; valuation always runs on the fitted constants.
    """
    k = OREB_SHARE_PRIOR_REBOUNDS if prior_rebounds is None else prior_rebounds
    depth = OREB_SHARE_SEASONS if seasons is None else seasons
    prior = positional_offensive_share(player.positions)

    offensive = total = 0.0
    for season in sorted(player.history, reverse=True)[:depth]:
        line = player.history[season]
        if line and line.get("reb") and "oreb" in line:
            offensive += line["oreb"]
            total += line["reb"]

    if total + k <= 0:
        return prior
    return (offensive + k * prior) / (total + k)


def _split_rebounds(player: PlayerProjection, stat: str) -> float | None:
    total = player.stats.get("reb")
    if total is None:
        return None
    share = offensive_share(player)
    return total * share if stat == "oreb" else total * (1.0 - share)


# The box-score categories a double-double is counted over. Note these are
# fixed by the STAT, not by the league: a league that does not score points
# still counts a 10-point game toward a double-double.
DOUBLE_CATEGORIES = ("pts", "reb", "ast", "stl", "blk")
DOUBLE_THRESHOLD = 10


def _p_reaches_threshold(per_game: float) -> float:
    """P(a category clears 10 in one game), modelling it as Poisson.

    Poisson is wrong in detail — points come in twos and threes, and minutes
    vary — but it is wrong where it does not matter. For a category averaging
    well under 10 the probability is near zero and for one averaging well over
    it is near one; the only players where the shape matters are those sitting
    near the threshold, and there the error is symmetric. Validated below.
    """
    if per_game <= 0:
        return 0.0
    term = math.exp(-per_game)
    cumulative = term
    for k in range(1, DOUBLE_THRESHOLD):
        term *= per_game / k
        cumulative += term
    return max(0.0, 1.0 - cumulative)


def _double_doubles(player: PlayerProjection, stat: str) -> float | None:
    """Projected double-doubles (or triple-doubles) over a season.

    ESPN scores both and projects neither, so without this a league that pays
    for them scores them at zero for everybody. On one real 10-team league
    that was 1831 double-doubles across the pool — 9,155 unscored points, and
    513 of them on Jokic alone, about 15% of his projection. It is also
    position-biased: it silently penalises exactly the bigs and playmakers who
    accumulate them.

    How many categories clear 10 in a game is a sum of five indicator
    variables, so its distribution comes from one convolution rather than an
    inclusion-exclusion mess. A double-double is two or more, a triple-double
    three or more — and ESPN counts a triple-double as BOTH, which is not an
    assumption here but a measurement: across 353 players with 2026 actuals,
    not one had td > dd.

    Categories are treated as independent, which they are not (a heavy
    rebounding night tends to be a heavy minutes night). Measured, that leaves
    the estimate slightly low on players who rarely post one, while the
    least-squares slope through the high-volume players is 1.005 — close
    enough to 1 that scaling it would be fitting noise.
    """
    games = player.stats.get("gp")
    if not games:
        return None

    distribution = [1.0]
    for category in DOUBLE_CATEGORIES:
        total = player.stats.get(category)
        if total is None:
            return None
        probability = _p_reaches_threshold(total / games)
        nxt = [0.0] * (len(distribution) + 1)
        for count, weight in enumerate(distribution):
            nxt[count] += weight * (1.0 - probability)
            nxt[count + 1] += weight * probability
        distribution = nxt

    needed = 3 if stat == "td" else 2
    return games * sum(distribution[needed:])


Estimator = tuple[Callable[[PlayerProjection, str], float | None], str]

ESTIMATORS: dict[str, Estimator] = {
    "oreb": (_split_rebounds, "reb x own offensive share, shrunk to position"),
    "dreb": (_split_rebounds, "reb x (1 - own offensive share, shrunk to position)"),
    "dd": (_double_doubles, "P(2+ categories reach 10) x games"),
    "td": (_double_doubles, "P(3+ categories reach 10) x games"),
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
#                           36.7% to trust the decomposition. Now only the
#                           fallback for a caller that passes no baseline;
#                           valuation uses baseline_for, below.
#
#   ESTIMATOR_RELATIVE_SD   each estimator's OWN error, isolated by splitting
#                           ACTUAL rebounds and comparing against actual
#                           oreb/dreb, so ESPN's projection error is not
#                           double-counted. Measured by
#                           scripts/calibration/oreb_share.py over 2026
#                           players with 100+ rebounds (n=351), held out from
#                           the fit: oreb 25.4%, dreb 8.8%. The positional
#                           split this replaced measured 47.6% / 12.9% on the
#                           same players. (The 40.1% / 15.1% it was previously
#                           credited with came from a population nobody
#                           recorded; oreb's sd swings 0.28-0.54 with the
#                           minimum-rebound filter alone, so only same-
#                           population comparisons mean anything.)
#                           Offensive rebounds are still far harder to infer
#                           from a total than defensive ones, which is why
#                           they get their own number. One sd covers players
#                           with and without history; the ~10% without are
#                           on the positional split and err more.
BASELINE_RELATIVE_SD = 0.367
ESTIMATOR_RELATIVE_SD: dict[str, float] = {
    "oreb": 0.254,
    "dreb": 0.088,
    # Double-doubles, over the 110 players who posted five or more in
    # 2026. Correlation with actuals 0.975, mean absolute error 1.34
    # against 5.19 for the zero this replaced.
    "dd": 0.302,
    # No entry for "td": only six players cleared five triple-doubles,
    # which is a sample too small to call a measurement, so it falls to
    # UNMEASURED_ESTIMATOR_SD and widens the band more, not less.
}
# An estimator nobody has backtested yet. Deliberately pessimistic: an
# unmeasured model should widen the band more than a measured one, not less.
UNMEASURED_ESTIMATOR_SD = 0.50

# --------------------------------------------------------------------------- #
# The per-player baseline.
# --------------------------------------------------------------------------- #
#
# One pool-wide baseline assumed every projection is equally hard, and it is
# not: over 2024-2026 a player projected under 24 minutes lands twice as far
# from his projection as one projected over 30. Fitted by
# scripts/calibration/uncertainty.py; see there for method and held-out
# coverage. Two findings shaped the buckets as much as the numbers did:
#
#   * Minutes dominate. Every season, the same order, by a wide margin.
#   * A short previous season (under SHORT_SEASON_GAMES) widens the band, but
#     only below 30 minutes — a heavy-minutes player's role survives a missed
#     stretch, a rotation player's may not. Above 30 it made no difference, so
#     that band is not split.
#
# Experience is NOT a feature, and rookies get no separate bucket. Rookies'
# errors were narrower than veterans' at the same minutes (0.49 vs 0.60-0.75
# under 24 mpg), matching the handoff's finding that ESPN projects them
# conservatively. They land in the not-short bucket for their minutes: "no
# previous season" is not evidence of games missed. Nor is an unsynced history.
#
# Widths are the 68.3rd percentile of |actual/projected - 1| — the half-width
# that covers the projection 68% of the time — not an sd around the mean. The
# band is drawn around the projection, and projections currently run ~20% high
# (games missed, mostly: item 4). Rerun the script when that is corrected.
#
# Coverage of the +/-1 band on each season held out from the fit, against the
# old constant on the same players (projected 300+ points, 1,049 player-seasons):
#
#                      2024          2025          2026
#                   const  fit    const  fit    const  fit
#   mpg 30+          85%   83%     68%   61%     71%   63%
#   mpg 24-30        66%   67%     73%   77%     60%   58%
#   mpg <24          39%   63%     43%   70%     52%   69%
#   mpg <30 short    19%   46%     43%   81%     47%   79%
#   all              57%   67%     58%   70%     59%   66%
#
# The constant was right only for starters and missed the bench by up to 50
# points of coverage. The worst remaining cell, short seasons in 2024, is that
# season rather than the bucket: low-minute errors ran ~35% wider in 2024 than
# in 2025 or 2026 across the board, which no pre-season feature can see. A
# short-season split within 24-30 mpg was tried and dropped: 44 player-seasons,
# coverage swinging 50-88%, and a width within 0.01 of the bucket it now shares.
SHORT_SEASON_GAMES = 50
BASELINE_BUCKETS: dict[str, float] = {
    "mpg 30+": 0.313,  # n=337
    "mpg 24-30": 0.376,  # n=254
    "mpg <24": 0.623,  # n=270
    "mpg <30 short": 0.726,  # n=188
}


# The part of every player's band that NO player escapes: the width of the
# tightest bucket. Comparisons between players are unaffected by it, which is
# the sense in which "the baseline cancels" was ever true — and it is only
# this floor, not a player's whole baseline, that cancels.
BASELINE_FLOOR = min(BASELINE_BUCKETS.values())


def comparative_sd(value_sd: float, projected_points: float) -> float:
    """The part of a stored band that distinguishes this player from others.

    value_sd^2 = (baseline^2 + model variance) * points^2, so taking out the
    floor every player shares leaves the model component and the player's
    excess baseline together, already combined in quadrature. A heavy-minutes
    starter on measured stats reads 0; a rotation big whose score is part
    modelled reads both of his reasons for doubt.

    From the stored columns alone, so the confident sort needs no migration.
    """
    shared = BASELINE_FLOOR * abs(projected_points)
    return math.sqrt(max(0.0, value_sd**2 - shared**2))


def projected_minutes(player: PlayerProjection) -> float | None:
    mpg = player.stats.get("mpg")
    if mpg:
        return mpg
    minutes, games = player.stats.get("min"), player.stats.get("gp")
    return minutes / games if minutes and games else None


def baseline_bucket(player: PlayerProjection) -> str:
    """Which BASELINE_BUCKETS row describes this player's projection."""
    minutes = projected_minutes(player)
    if minutes is not None and minutes >= 30:
        return "mpg 30+"

    if player.history:
        last = player.history[max(player.history)]
        # None is "not in the NBA" — a rookie — and is not a short season.
        # {} is "listed, no line": the whole season missed, the shortest.
        if last is not None and last.get("gp", 0.0) < SHORT_SEASON_GAMES:
            return "mpg <30 short"
    return "mpg 24-30" if minutes is not None and minutes >= 24 else "mpg <24"


def baseline_for(player: PlayerProjection) -> float:
    """The relative sd this player's projection deserves, before modelling.

    A player ESPN gives no minutes lands in the widest band, which is the
    honest answer; it rarely matters, since they are projected near zero.
    """
    return BASELINE_BUCKETS.get(baseline_bucket(player), BASELINE_RELATIVE_SD)


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
    baseline: Callable[[PlayerProjection], float] = baseline_for,
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
    base = baseline(player)
    total = sum(abs(player.stats.get(stat, 0.0) * weight) for stat, weight in weights.items())
    if total <= 0:
        return ValueUncertainty(base, 0.0, 0.0, 0.0)

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

    relative = math.sqrt(base**2 + model_variance)
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
    baseline: Callable[[PlayerProjection], float] = baseline_for,
) -> dict[int, ValueUncertainty]:
    """player_uncertainty for a whole pool, keyed by espn_player_id."""
    provenance = {c.stat: c.provenance for c in coverage}
    return {p.espn_player_id: player_uncertainty(p, weights, provenance, baseline) for p in players}
