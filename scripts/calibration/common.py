"""Shared plumbing for the calibration scripts: load the cache, score it, slice it.

Everything here reads scripts/calibration/data/, written by fetch.py, and
nothing here touches the network or the database. The point of keeping these in
one place is that every measurement uses the SAME definition of "projected
points", "rookie" and "the relevant pool" — the constants in stats.py are only
comparable with each other if they were measured the same way.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from warroom.valuation.domain import PlayerProjection
from warroom.valuation.stats import resolve_pool

DATA = Path(__file__).parent / "data"

# "The pool that matters" for points-error measurements: players this app
# projects at 650+ fantasy points. It is the threshold that reproduces the
# n=321 population BASELINE_RELATIVE_SD was originally measured on (320 in the
# 2026 cache), and it sits well below any drafted player in a 10x13 league, so
# nothing that gets drafted is excluded. Below it, relative error is dominated
# by denominators near zero — a 40-point projection that scores 120 is a +200%
# "miss" that no draft decision ever turned on.
RELEVANT_POINTS = 650.0

# A share of rebounds is only a measurement once there are enough of them:
# at 30 rebounds a single offensive board moves the share by three points.
MIN_REBOUNDS = 100.0


@dataclass
class Season:
    season: int
    weights: dict[str, float]
    players: list[dict[str, Any]]

    @cached_property
    def by_id(self) -> dict[int, dict[str, Any]]:
        return {p["id"]: p for p in self.players}

    @cached_property
    def projected_points(self) -> dict[int, float]:
        """This app's projected fantasy points: ESPN's projected line, resolved
        by stats.py exactly as valuation resolves it, scored by this season's
        weights. Not ESPN's applied total — the app's constants describe the
        app's own numbers."""
        resolved, _ = resolve_pool((projection(p) for p in self.players), self.weights)
        return {r.espn_player_id: score(r.stats, self.weights) for r in resolved}

    @cached_property
    def actual_points(self) -> dict[int, float]:
        """What each player actually scored under this season's weights.

        Computed from the actual line rather than read from ESPN's applied
        total so both sides of every comparison use identical scoring."""
        return {p["id"]: score(p["actual"], self.weights) for p in self.players}

    def relevant(self) -> list[dict[str, Any]]:
        return [p for p in self.players if self.projected_points[p["id"]] >= RELEVANT_POINTS]


def load(season: int) -> Season:
    path = DATA / f"{season}.json"
    if not path.exists():
        raise SystemExit(f"{path} missing — run: python -m scripts.calibration.fetch")
    raw = json.loads(path.read_text())
    return Season(season, raw["settings"]["point_weights"], raw["players"])


def score(stats: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return sum(weight * stats.get(stat, 0.0) for stat, weight in weights.items())


def history_of(player: Mapping[str, Any]) -> dict[int, dict[str, float] | None]:
    """The cached history with integer seasons, as to_projection produces it."""
    return {int(season): line for season, line in player["history"].items()}


def projection(player: Mapping[str, Any], line: str = "projected") -> PlayerProjection:
    """A cached player as engine input. line="actual" feeds a model the true
    stat line, which is how an estimator's OWN error is isolated from ESPN's
    projection error."""
    return PlayerProjection(
        espn_player_id=player["id"],
        name=player["name"],
        positions=tuple(player["positions"]),
        stats=dict(player[line]),
        history=history_of(player),
    )


def played(line: Mapping[str, float] | None) -> bool:
    return bool(line) and line.get("gp", 0) > 0


def is_rookie(player: Mapping[str, Any], season: int) -> bool:
    """No NBA record the season before. Only callable where that season was
    fetched — an unfetched season is unknown, not evidence of anything."""
    history = history_of(player)
    return season - 1 in history and history[season - 1] is None


def experience(player: Mapping[str, Any], season: int) -> int:
    """Prior seasons with games played, counted back from `season - 1`."""
    return sum(1 for line in history_of(player).values() if played(line))


def games_and_rate(
    player: Mapping[str, Any], projected_points: float, actual_points: float
) -> tuple[float, float] | None:
    """actual/projected split into (games ratio, per-game ratio).

    The two multiply back to the whole ratio. None when either side has no
    games, where the per-game rate is undefined."""
    projected_gp = player["projected"].get("gp", 0)
    actual_gp = player["actual"].get("gp", 0)
    if not projected_gp or not actual_gp or not projected_points:
        return None
    return (
        actual_gp / projected_gp,
        (actual_points / actual_gp) / (projected_points / projected_gp),
    )


def sd(values: Iterable[float]) -> float:
    """Population sd — around the mean error, as every constant was measured."""
    return statistics.pstdev(list(values))


def mae(pairs: Iterable[tuple[float, float]]) -> float:
    return statistics.fmean(abs(a - b) for a, b in pairs)


def log_sd(ratios: Iterable[float]) -> float:
    return sd(math.log(r) for r in ratios if r > 0)
