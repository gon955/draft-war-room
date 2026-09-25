"""How many of their projected games each player will actually play.

This app's projections ran 13-23% above what players then scored in each of
2024, 2025 and 2026 (median, players projected 650+), and most of that is
games: players appeared in ~88% of the games ESPN projected for them (median;
the mean is lower, ~80%, because missed games only ever subtract). That is not
spread evenly, and the unevenness is what matters to a draft — a flat haircut
moves every value and every replacement level alike and changes no ranking.

Measured by scripts/calibration/availability.py against this app's OWN
projected points. Not ESPN's projected total: that scores rebounds and
double-doubles as zero in this league, so actuals beat it by construction, and
the handoff's "double-count trap" — the idea that ESPN's per-game rates were
conservative enough to cover the missed games — was an artefact of it. Against
the full projected line, per-game output also comes in low (~0.93), so nothing
offsets the games; they are the correction, not a double count.

Applied to the projected stat line BEFORE the engine runs (services/valuation),
so replacement levels are computed on adjusted numbers too. Behind the
AVAILABILITY_MODEL setting, off by default.
"""

from __future__ import annotations

from dataclasses import replace

from .domain import PlayerProjection
from .stats import projected_minutes

# Regular-season length by ESPN season id (the year a season ends). 2019-20
# stopped at 63-75 games a team and 2020-21 was scheduled for 72; availability
# is games played over games there were to play, so they are not out of 82.
SEASON_GAMES: dict[int, int] = {2020: 72, 2021: 72}
FULL_SEASON = 82

# Most recent season first. Recent seasons say more about a player's body and
# role now; three because that is how much history sync keeps.
HISTORY_WEIGHTS = (3.0, 2.0, 1.0)

# Stats that are rates or averages, not season totals. Everything else in a
# projected line is a count over the season and scales with games played.
RATE_STATS = frozenset(
    {
        "3pg", "apg", "bpg", "ppg", "rpg", "spg", "topg", "mpg", "ppm",
        "fg%", "ft%", "3pt%", "afg%", "a/to", "str", "ftr",
    }
)  # fmt: skip

# Expected share of projected games actually played, by bucket. Means, not
# medians: the objective is expected points, and a player's expected games
# include the season they lose to injury. Fitted by
# scripts/calibration/availability.py over 1,049 player-seasons, 2024-2026.
#
# WHY THIS SHIPS OFF. Judged on each season held out from the fit, by rank
# correlation between projected and actual points over the 130 players a
# 10x13 draft takes — the only thing an availability model can improve over a
# flat haircut, which fixes the level just as well and reorders nobody:
#
#                     2024     2025     2026
#     flat           0.669    0.547    0.516
#     this model     0.663    0.556    0.534      mean +0.007
#
# Worse in one season, marginally better in two. Season-wide swings dwarf
# the differences between buckets: heavy-minutes players played 95%, 84% and
# 81% of their projected games in the three seasons, frail rotation players
# 39%, 71% and 68%. Alternatives did no better held out — history alone
# -0.023, ESPN's projected games alone -0.017, projected games x minutes
# +0.012 but also worse in 2024 — so none replaced this one.
#
# What DOES hold up is the level: this app's projections ran 20-25% high, and
# any of these takes that to within ~7%. A level shift cannot change a single
# ranking or recommendation, though, since value and replacement move
# together. Turn this on for honest point totals, not for better picks, and
# rerun the script when a season closes.
AVAILABILITY_BUCKETS: dict[str, float] = {
    "mpg 30+": 0.868,  # n=337
    "mpg 24-30 healthy": 0.881,  # n=229
    "mpg 24-30 fragile": 0.752,  # n=49
    "mpg <24 healthy": 0.779,  # n=203
    "mpg <24 fragile": 0.664,  # n=129
    "mpg <30 frail": 0.599,  # n=102
}

HEALTHY = 0.70
FRAGILE = 0.50


def season_length(season: int) -> int:
    return SEASON_GAMES.get(season, FULL_SEASON)


def prior_availability(player: PlayerProjection) -> float | None:
    """Weighted share of available games played over the last three seasons.

    None when there is no NBA season to judge: a rookie, or a pool whose
    history was never synced. A season spent outside the NBA (None) is
    skipped rather than counted as zero; a season listed with no line ({})
    counts as zero games, because the player was in the league and missed it.
    """
    total = weights = 0.0
    for weight, season in zip(HISTORY_WEIGHTS, sorted(player.history, reverse=True), strict=False):
        line = player.history[season]
        if line is None:
            continue
        total += weight * min(1.0, line.get("gp", 0.0) / season_length(season))
        weights += weight
    return total / weights if weights else None


def availability_bucket(player: PlayerProjection) -> str:
    """Which AVAILABILITY_BUCKETS row describes this player.

    Heavy minutes first, and alone: above 30 projected minutes a player's
    history made no difference to the share of games they played. Below it,
    history does, and a missing history reads as healthy — no evidence of
    games missed is not evidence of fragility.
    """
    minutes = projected_minutes(player)
    if minutes is not None and minutes >= 30:
        return "mpg 30+"

    history = prior_availability(player)
    if history is not None and history < FRAGILE:
        return "mpg <30 frail"
    health = "fragile" if history is not None and history < HEALTHY else "healthy"
    band = "mpg 24-30" if minutes is not None and minutes >= 24 else "mpg <24"
    return f"{band} {health}"


def availability_factor(player: PlayerProjection) -> float:
    return AVAILABILITY_BUCKETS.get(availability_bucket(player), 1.0)


def apply_availability(player: PlayerProjection) -> PlayerProjection:
    """The projection with every season total scaled to expected games.

    Totals and games scale together, so every per-game rate is unchanged —
    which is what keeps the double-double estimator, a function of per-game
    rates times games, consistent with the rest of the line.
    """
    factor = availability_factor(player)
    if factor == 1.0:
        return player
    stats = {
        stat: value if stat in RATE_STATS else value * factor
        for stat, value in player.stats.items()
    }
    return replace(player, stats=stats)
