"""Re-derive the handoff's measurements from the cache, to trust the harness.

Before any constant is refitted with these scripts, they have to reproduce the
numbers the current constants came from. A harness that cannot is measuring
something else, and anything it then "improves" is unanchored.

    python -m scripts.calibration.baseline [--season 2026]

Two kinds of line. EXACT reproductions confirm the plumbing — the cache, the
joins, the rookie flag. APPROX ones were originally measured with filters that
were never written down; what is printed is the closest principled definition,
and the gap is reported rather than tuned away.
"""

from __future__ import annotations

import argparse
import statistics

from warroom.valuation.stats import (
    DEFAULT_OFFENSIVE_SHARE,
    _double_doubles,
    positional_offensive_share,
)

from .common import (
    MIN_REBOUNDS,
    games_and_rate,
    is_rookie,
    load,
    log_sd,
    mae,
    projection,
    score,
    sd,
)


def row(label: str, handoff: str, measured: str, n: int, note: str = "") -> None:
    print(f"  {label:<38} {handoff:>12} {measured:>12} {n:>6}  {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()
    s = load(args.season)
    print(f"season {s.season}: {len(s.players)} players cached\n")
    row("measurement", "handoff", "measured", 0, "")
    print("  " + "-" * 80)

    # ESPN's own projected pool: everyone ESPN put a fantasy total on.
    espn_pool = [p for p in s.players if (p["projected_points"] or 0) > 0]

    projected_gp = [p["projected"]["gp"] for p in espn_pool if p["projected"].get("gp")]
    actual_gp = [p["actual"].get("gp", 0) for p in espn_pool if p["projected"].get("gp")]
    row(
        "median games, projected -> actual",
        "69 -> 59",
        f"{statistics.median(projected_gp):.0f} -> {statistics.median(actual_gp):.0f}",
        len(projected_gp),
        "EXACT",
    )

    top = sorted(espn_pool, key=lambda p: -p["projected_points"])[: len(espn_pool) // 2]
    for label, group, want_median, want_sd in (
        ("veterans", [p for p in top if not is_rookie(p, s.season)], "1.14", "0.66"),
        ("rookies", [p for p in top if is_rookie(p, s.season)], "1.36", "0.31"),
    ):
        ratios = [(p["actual_points"] or 0) / p["projected_points"] for p in group]
        row(
            f"top half {label}: median actual/proj",
            want_median,
            f"{statistics.median(ratios):.2f}",
            len(group),
            "EXACT",
        )
        row(
            f"top half {label}: sd log ratio", want_sd, f"{log_sd(ratios):.2f}", len(group), "EXACT"
        )

    # ESPN's projected fantasy total is this league's weights on ESPN's own
    # projected line with every unprojected stat scored as zero. Checked here
    # so the claim in data_source.espn_applied_total stays re-verifiable.
    unprojected = {"oreb", "dreb", "dd", "td", "qd"}
    truncated = {k: v for k, v in s.weights.items() if k not in unprojected}
    matches = sum(
        abs(score(p["projected"], truncated) - p["projected_points"])
        <= 0.001 * p["projected_points"]
        for p in espn_pool
    )
    row(
        "ESPN total == line minus unprojected",
        "(new)",
        f"{matches}/{len(espn_pool)}",
        len(espn_pool),
        "EXACT: oreb/dreb/dd/td scored as 0",
    )

    relevant = s.relevant()
    errors = [s.actual_points[p["id"]] / s.projected_points[p["id"]] - 1 for p in relevant]
    row(
        "BASELINE_RELATIVE_SD (points)",
        "0.367",
        f"{sd(errors):.3f}",
        len(errors),
        "APPROX: engine points >= 650",
    )

    split = [
        g
        for p in espn_pool
        if (g := games_and_rate(p, p["projected_points"], p["actual_points"] or 0))
    ]
    row(
        "availability sd (games ratio)",
        "~0.29",
        f"{sd(g - 1 for g, _ in split):.3f}",
        len(split),
        "EXACT: ESPN pool, played",
    )
    relevant_split = [
        g
        for p in relevant
        if (g := games_and_rate(p, s.projected_points[p["id"]], s.actual_points[p["id"]]))
    ]
    row(
        "rate sd (per-game ratio)",
        "0.262",
        f"{sd(r - 1 for _, r in relevant_split):.3f}",
        len(relevant_split),
        "APPROX: engine points >= 650",
    )

    # Estimator error, isolated: split ACTUAL rebounds and compare to actual
    # oreb/dreb, so ESPN's projection error plays no part.
    boards = [p for p in s.players if p["actual"].get("reb", 0) >= MIN_REBOUNDS]
    share = {p["id"]: positional_offensive_share(p["positions"]) for p in boards}
    truth = {p["id"]: p["actual"]["oreb"] / p["actual"]["reb"] for p in boards}
    row(
        "oreb share MAE, by position",
        "0.0675",
        f"{mae((share[i], truth[i]) for i in truth):.4f}",
        len(boards),
        "APPROX: reb >= 100",
    )
    row(
        "oreb share MAE, flat",
        "0.0776",
        f"{mae((DEFAULT_OFFENSIVE_SHARE, truth[i]) for i in truth):.4f}",
        len(boards),
        "APPROX: reb >= 100",
    )
    oreb = [
        p["actual"]["reb"] * share[p["id"]] / p["actual"]["oreb"] - 1
        for p in boards
        if p["actual"].get("oreb")
    ]
    dreb = [
        p["actual"]["reb"] * (1 - share[p["id"]]) / p["actual"]["dreb"] - 1
        for p in boards
        if p["actual"].get("dreb")
    ]
    # Not reproducible, and worth knowing why: relative error on a small count
    # explodes, so this sd swings from 0.28 to 0.54 with the minimum-rebound
    # filter, and the original filter was not recorded. Compare estimators on
    # ONE stated population (reb >= 100) rather than against 0.401.
    row(
        "ESTIMATOR_RELATIVE_SD oreb",
        "0.401",
        f"{sd(oreb):.3f}",
        len(oreb),
        "UNANCHORED: filter-sensitive, see source",
    )
    row("ESTIMATOR_RELATIVE_SD dreb", "0.151", f"{sd(dreb):.3f}", len(dreb), "APPROX: reb >= 100")

    doubles = [p for p in s.players if p["actual"].get("dd", 0) >= 5]
    predicted = [_double_doubles(projection(p, "actual"), "dd") or 0.0 for p in doubles]
    actual = [p["actual"]["dd"] for p in doubles]
    row(
        "ESTIMATOR_RELATIVE_SD dd",
        "0.302",
        f"{sd(e / a - 1 for e, a in zip(predicted, actual, strict=True)):.3f}",
        len(doubles),
        "players with 5+ dd",
    )
    row(
        "dd correlation / MAE",
        "0.975 / 1.34",
        f"{statistics.correlation(predicted, actual):.3f} / {mae(zip(predicted, actual)):.2f}",
        len(doubles),
        "APPROX: correlation holds, MAE population unknown",
    )


if __name__ == "__main__":
    main()
