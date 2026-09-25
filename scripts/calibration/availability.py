"""Fit the availability model (availability.AVAILABILITY_BUCKETS) and judge it.

    python -m scripts.calibration.availability

Each bucket's factor is the MEAN share of projected games actually played —
the mean because the objective is expected points, and an expectation has to
carry the season a player loses to injury, which a median ignores.

JUDGED ON WHAT A DRAFT USES. The model scales every total by its factor, so an
adjusted projection is the raw one times the factor. Three projections of
each held-out season are compared against what players scored:

    raw     the projection as the app has it today
    flat    raw times the pooled mean games share: fixes the level, and by
            construction changes no ranking
    model   raw times each player's bucket factor

`flat` is the bar to clear. Any availability adjustment fixes the level; the
only thing this model can add over a haircut is ORDER, so the column that
decides it is the rank correlation, on the players a 10x13 draft takes.
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict

from warroom.valuation.availability import availability_bucket

from .common import load, projection

SEASONS = (2024, 2025, 2026)
MIN_PROJECTED = 300.0
DRAFTED = 130


def observations() -> list[dict]:
    out = []
    for year in SEASONS:
        s = load(year)
        for p in s.players:
            raw = s.projected_points[p["id"]]
            games = p["projected"].get("gp", 0)
            if raw < MIN_PROJECTED or not games:
                continue
            out.append(
                {
                    "year": year,
                    "bucket": availability_bucket(projection(p)),
                    "games": p["actual"].get("gp", 0) / games,
                    "raw": raw,
                    "actual": s.actual_points[p["id"]],
                }
            )
    return out


def fit(rows: list[dict]) -> dict[str, float]:
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by[r["bucket"]].append(r["games"])
    return {bucket: statistics.fmean(v) for bucket, v in by.items()}


def judge(test: list[dict], factor, label: str) -> str:
    projected = [factor(r) * r["raw"] for r in test]
    actual = [r["actual"] for r in test]
    drafted = sorted(range(len(test)), key=lambda i: -test[i]["raw"])[:DRAFTED]
    mae = statistics.fmean(abs(p - a) for p, a in zip(projected, actual, strict=True))
    bias = statistics.fmean(a / p - 1 for p, a in zip(projected, actual, strict=True))
    rank = statistics.correlation(
        [projected[i] for i in drafted], [actual[i] for i in drafted], method="ranked"
    )
    return (
        f"    {label:<6} MAE {mae:6.0f}   bias {bias:+6.1%}   rank corr (top {DRAFTED}) {rank:.3f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()
    rows = observations()
    buckets = sorted({r["bucket"] for r in rows})

    print(f"{len(rows)} player-seasons, projected >= {MIN_PROJECTED:.0f}\n")
    print("each season held out; factors fitted on the other two")
    for year in SEASONS:
        train = [r for r in rows if r["year"] != year]
        test = [r for r in rows if r["year"] == year]
        factors = fit(train)
        flat = statistics.fmean(r["games"] for r in train)
        print(f"\n  {year} (n={len(test)})")
        print(judge(test, lambda r: 1.0, "raw"))
        print(judge(test, lambda r, f=flat: f, "flat"))
        print(judge(test, lambda r, f=factors: f[r["bucket"]], "model"))
        print("    bucket calibration, predicted -> realised share of projected games:")
        for bucket in buckets:
            got = [r["games"] for r in test if r["bucket"] == bucket]
            if got:
                print(
                    f"      {bucket:<22} {factors[bucket]:.3f} -> {statistics.fmean(got):.3f}"
                    f"  (n={len(got)})"
                )

    final = fit(rows)
    print("\nfitted on all seasons (paste into availability.AVAILABILITY_BUCKETS):")
    for bucket in buckets:
        n = sum(1 for r in rows if r["bucket"] == bucket)
        print(f'    "{bucket}": {final[bucket]:.3f},  # n={n}')


if __name__ == "__main__":
    main()
