"""Fit the per-player offensive rebound share: its shrinkage k and history depth.

    python -m scripts.calibration.oreb_share

The estimator under test is stats.offensive_share itself, called with swept
arguments, so what is fitted is exactly what valuation runs. Its OWN error is
isolated the same way the positional table's was: split each player's ACTUAL
rebounds and compare against their actual offensive rebounds, so ESPN's
projection error plays no part.

Fitted on one season and judged on the others. k is chosen on FIT_SEASON
only; the held-out rows are the evidence, the fit row is not.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from warroom.valuation.stats import OFFENSIVE_REBOUND_SHARE, offensive_share

from .common import MIN_REBOUNDS, Season, load, mae, projection, sd

FIT_SEASON = 2025
HELD_OUT = (2026, 2024)
PRIOR_REBOUNDS = (0, 25, 50, 75, 100, 150, 200, 300, 500, 1000)
DEPTHS = (1, 2, 3)


def boards(season: Season) -> list[dict]:
    return [p for p in season.players if p["actual"].get("reb", 0) >= MIN_REBOUNDS]


def share_mae(season: Season, k: float | None, depth: int) -> float:
    """k=None is the positional table alone — the current estimator."""
    pairs = []
    for p in boards(season):
        player = projection(p)
        if k is None:
            player = replace(player, history={})
        estimate = offensive_share(player, prior_rebounds=k or 0.0, seasons=depth)
        pairs.append((estimate, p["actual"]["oreb"] / p["actual"]["reb"]))
    return mae(pairs)


def relative_sds(season: Season, k: float | None, depth: int) -> tuple[float, float, int]:
    """ESTIMATOR_RELATIVE_SD for oreb and dreb, on the same population."""
    oreb, dreb = [], []
    for p in boards(season):
        player = projection(p)
        if k is None:
            player = replace(player, history={})
        share = offensive_share(player, prior_rebounds=k or 0.0, seasons=depth)
        reb = p["actual"]["reb"]
        if p["actual"].get("oreb"):
            oreb.append(reb * share / p["actual"]["oreb"] - 1)
        if p["actual"].get("dreb"):
            dreb.append(reb * (1 - share) / p["actual"]["dreb"] - 1)
    return sd(oreb), sd(dreb), len(oreb)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    seasons = {year: load(year) for year in (FIT_SEASON, *HELD_OUT)}

    # The positional table was itself measured on 2026 players, so its 2026
    # row is in-sample for the baseline — which flatters the baseline, and so
    # can only make the improvement below look SMALLER than it is.
    print(f"positional prior: {OFFENSIVE_REBOUND_SHARE}\n")

    header = "".join(f"{year:>10}" for year in seasons)
    print(f"share MAE (reb >= {MIN_REBOUNDS:.0f})      {header}")
    base = {year: share_mae(s, None, 1) for year, s in seasons.items()}
    print(f"  {'positional only (today)':<26}" + "".join(f"{base[y]:>10.4f}" for y in seasons))

    best = None
    for depth in DEPTHS:
        for k in PRIOR_REBOUNDS:
            maes = {year: share_mae(s, k, depth) for year, s in seasons.items()}
            print(f"  depth={depth} k={k:<14}" + "".join(f"{maes[y]:>10.4f}" for y in seasons))
            if best is None or maes[FIT_SEASON] < best[0]:
                best = (maes[FIT_SEASON], depth, k)
        print()

    _, depth, k = best
    print(f"chosen on {FIT_SEASON}: depth={depth}, k={k}\n")
    for year in HELD_OUT:
        s = seasons[year]
        with_history = sum(1 for p in boards(s) if any(projection(p).history.values()))
        old = relative_sds(s, None, 1)
        new = relative_sds(s, k, depth)
        print(
            f"{year} held out: share MAE {base[year]:.4f} -> {share_mae(s, k, depth):.4f}"
            f" ({1 - share_mae(s, k, depth) / base[year]:.0%} better); "
            f"{with_history}/{len(boards(s))} players have history"
        )
        print(f"  ESTIMATOR_RELATIVE_SD oreb {old[0]:.3f} -> {new[0]:.3f}   (n={new[2]})")
        print(f"  ESTIMATOR_RELATIVE_SD dreb {old[1]:.3f} -> {new[1]:.3f}")


if __name__ == "__main__":
    main()
