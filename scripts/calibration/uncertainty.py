"""Fit the per-player uncertainty baseline: stats.BASELINE_BUCKETS.

    python -m scripts.calibration.uncertainty

How far a season total lands from this app's projection, bucketed by what is
known before the season: projected minutes, and whether last season was cut
short. Bucketed rather than regressed — ~350 usable players a season will not
carry a model with more parameters than this.

WHAT THE BAND IS FITTED TO. The band is drawn around the projection, so it is
fitted to cover the projection: each bucket's width is the 68.3rd percentile of
|actual / projected - 1|. For a centred normal that IS one sd; for the real,
fat-tailed and downward-biased errors it is the number that makes "68% inside
±1 band" true by construction, where a textbook sd around the mean would not.
The bias itself (projections run ~20% high, mostly games missed) is item 4's
to model — when that shifts the centre, rerun this and the widths will shrink.

HOW IT IS JUDGED. Leave one season out: fit on two, measure coverage bucket by
bucket on the third. An overall 68% can hide every bucket being wrong in a
different direction, so it is the per-bucket rows that count.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from warroom.valuation.stats import (
    BASELINE_RELATIVE_SD,
    baseline_bucket,
)

from .common import load, projection

SEASONS = (2024, 2025, 2026)
# Below this the relative error is dominated by a small denominator and no
# draft decision turns on it; such players still get a band at runtime, from
# the widest bucket.
MIN_PROJECTED = 300.0
TARGET = 0.6827


def observations() -> list[tuple[int, str, float]]:
    """(season, bucket, relative error) for every usable player-season."""
    out = []
    for year in SEASONS:
        s = load(year)
        for p in s.players:
            projected = s.projected_points[p["id"]]
            if projected < MIN_PROJECTED:
                continue
            bucket = baseline_bucket(projection(p))
            out.append((year, bucket, s.actual_points[p["id"]] / projected - 1))
    return out


def width(errors: list[float]) -> float:
    ranked = sorted(abs(e) for e in errors)
    return ranked[int(TARGET * (len(ranked) - 1))]


def fit(rows: list[tuple[int, str, float]]) -> dict[str, float]:
    by_bucket: dict[str, list[float]] = defaultdict(list)
    for _, bucket, error in rows:
        by_bucket[bucket].append(error)
    return {bucket: width(errors) for bucket, errors in by_bucket.items()}


def coverage(rows: list[tuple[int, str, float]], widths: dict[str, float] | float) -> dict:
    hits: dict[str, list[bool]] = defaultdict(list)
    for _, bucket, error in rows:
        band = widths if isinstance(widths, float) else widths[bucket]
        hits[bucket].append(abs(error) <= band)
    return {bucket: (sum(h) / len(h), len(h)) for bucket, h in hits.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    rows = observations()
    buckets = sorted({b for _, b, _ in rows})

    print(f"{len(rows)} player-seasons, projected >= {MIN_PROJECTED:.0f}\n")
    print("coverage of +/-1 band, each season held out (target 68%)")
    print(f"  {'bucket':<22}" + "".join(f"{f'{y} const':>12}{f'{y} fit':>10}" for y in SEASONS))
    held_out: dict[int, dict] = {}
    constant: dict[int, dict] = {}
    for year in SEASONS:
        train = [r for r in rows if r[0] != year]
        test = [r for r in rows if r[0] == year]
        held_out[year] = coverage(test, fit(train))
        constant[year] = coverage(test, BASELINE_RELATIVE_SD)
    for bucket in [*buckets, "ALL"]:
        cells = []
        for year in SEASONS:
            if bucket == "ALL":
                test = [r for r in rows if r[0] == year]
                c = sum(abs(e) <= BASELINE_RELATIVE_SD for _, _, e in test) / len(test)
                train_widths = fit([r for r in rows if r[0] != year])
                f = sum(abs(e) <= train_widths[b] for _, b, e in test) / len(test)
                n = len(test)
            else:
                c, n = constant[year].get(bucket, (float("nan"), 0))
                f, _ = held_out[year].get(bucket, (float("nan"), 0))
            cells.append(f"{c:>8.0%} n{n:<3}{f:>9.0%} ")
        print(f"  {bucket:<22}" + "".join(cells))

    final = fit(rows)
    print("\nfitted on all seasons (paste into stats.BASELINE_BUCKETS):")
    for bucket in buckets:
        n = sum(1 for _, b, _ in rows if b == bucket)
        print(f'    "{bucket}": {final[bucket]:.3f},  # n={n}')


if __name__ == "__main__":
    main()
