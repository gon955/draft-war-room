"""Play whole drafts with different strategies for YOUR team and compare lineups.

    python -m scripts.calibration.strategy [--drafts 20] [--strategies value greedy rollout]

The pool is the real league's, read from the database (players + this
league's valuations); nothing is written. The nine other teams are autodraft
bots drafting on ESPN's projected totals — the room as it will actually behave
— with reach 3 and a seed, so they disagree run to run. Every strategy faces
the SAME seeds from the same draft slot, so a difference between strategies
is the strategy's and not the dice's.

The score is the objective in a season-total league: your best starting
lineup's projected points once the draft is over.

    value     take the highest live value that fills a seat you need
    greedy    the API's default order (advice.advise, depth=greedy)
    rollout   the API's depth=rollout order
"""

from __future__ import annotations

import argparse
import random
import statistics
import time

from sqlalchemy import select

from warroom.config import get_settings
from warroom.db import SessionLocal
from warroom.models import League, Player, Valuation
from warroom.services.valuation import settings_for, to_projection
from warroom.valuation.advice import Advice, RolloutField, advise
from warroom.valuation.domain import LeagueSettings, PlayerProjection
from warroom.valuation.draft import (
    _best_seating,
    autodraft,
    lineup_values_by_mask,
    seat_slots,
)
from warroom.valuation.engine import ReplacementBasis

LEAGUE_ID = 19048
BOT_REACH = 3


def snake(num_teams: int, rounds: int, my_slot: int) -> list[tuple[int, int, bool]]:
    order = []
    for r in range(rounds):
        teams = range(1, num_teams + 1) if r % 2 == 0 else range(num_teams, 0, -1)
        for team in teams:
            order.append((len(order) + 1, team, team == my_slot))
    return order


def lineup_points(roster, settings: LeagueSettings, points) -> float:
    seats = seat_slots(settings)
    table = lineup_values_by_mask([(p, points[p.espn_player_id]) for p in roster], seats)
    return _best_seating(table, len(seats))


def _greedy_key(a: Advice):
    return (
        -a.score,
        -a.marginal,
        not a.fills_open_seat,
        -a.lineup_delta,
        -a.live.value.value,
        a.live.value.player.espn_player_id,
    )


def choose(strategy: str, advice: list[Advice]) -> PlayerProjection:
    if strategy == "value":
        fits = [a for a in advice if a.fills_open_seat] or advice
        return min(
            fits, key=lambda a: (-a.live.value.value, a.live.value.player.espn_player_id)
        ).live.value.player
    if strategy == "greedy":
        return min(advice, key=_greedy_key).live.value.player
    if strategy == "rollout":
        return min(
            advice,
            key=lambda a: (
                a.rollout is None,
                -(a.rollout.value if a.rollout else 0.0),
                *_greedy_key(a),
            ),
        ).live.value.player
    raise ValueError(strategy)


def draft(
    strategy: str,
    my_slot: int,
    seed: int,
    pool: list[PlayerProjection],
    settings: LeagueSettings,
    points: dict[int, float],
    espn: dict[int, float],
    preseason: dict[int, tuple[float, float]],
    basis: ReplacementBasis,
    timings: list[float],
) -> float:
    rounds = settings.roster_size
    board = snake(settings.num_teams, rounds, my_slot)
    rosters: dict[int, list[PlayerProjection]] = {t: [] for t in range(1, settings.num_teams + 1)}
    available = list(pool)
    rng = random.Random(seed)
    market_rank = {pid: r for r, pid in enumerate(sorted(espn, key=lambda i: -espn[i]))}
    weight = get_settings().draft_market_weight

    i = 0
    while i < len(board):
        is_mine = board[i][2]
        if not is_mine:
            made = autodraft(
                upcoming=board[i:],
                rosters=rosters,
                available=available,
                settings=settings,
                points=espn,
                preseason={},
                rng=rng,
                reach=BOT_REACH,
                stop_at_my_pick=True,
            )
            taken = {m.espn_player_id for m in made}
            by_id = {p.espn_player_id: p for p in available}
            for m in made:
                rosters[m.team_slot].append(by_id[m.espn_player_id])
            available = [p for p in available if p.espn_player_id not in taken]
            i += len(made)
            if not made:
                break
            continue

        started = time.perf_counter()
        advice, _ = advise(
            available=available,
            rosters=rosters,
            mine=rosters[my_slot],
            upcoming=board[i:],
            settings=settings,
            points=points,
            preseason=preseason,
            basis=basis,
            market_rank=market_rank,
            market_weight=weight,
            rollout=RolloutField(points=espn) if strategy == "rollout" else None,
        )
        timings.append(time.perf_counter() - started)
        pick = choose(strategy, advice)
        rosters[my_slot].append(pick)
        available = [p for p in available if p.espn_player_id != pick.espn_player_id]
        i += 1

    return lineup_points(rosters[my_slot], settings, points)


def load():
    with SessionLocal() as db:
        league = db.scalar(select(League).where(League.espn_league_id == LEAGUE_ID))
        rows = db.execute(
            select(Player, Valuation)
            .join(
                Valuation, (Valuation.player_id == Player.id) & (Valuation.league_id == league.id)
            )
            .where(Player.season == league.season)
        ).all()
        settings = settings_for(league)
        pool = [to_projection(p) for p, _ in rows]
        points = {p.espn_player_id: v.projected_points for p, v in rows}
        espn = {p.espn_player_id: float(p.espn_projected_points or 0.0) for p, _ in rows}
        preseason = {p.espn_player_id: (v.replacement_points, v.value) for p, v in rows}
    return pool, settings, points, espn, preseason


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--drafts", type=int, default=20)
    parser.add_argument("--strategies", nargs="+", default=["value", "greedy", "rollout"])
    args = parser.parse_args()

    pool, settings, points, espn, preseason = load()
    basis = ReplacementBasis(settings.replacement_basis)
    print(
        f"{len(pool)} players, {settings.num_teams} teams x {settings.roster_size} rounds, "
        f"basis {basis.value}, {args.drafts} drafts per strategy\n"
    )

    results: dict[str, list[float]] = {}
    timing: dict[str, list[float]] = {}
    for strategy in args.strategies:
        results[strategy], timing[strategy] = [], []
        for n in range(args.drafts):
            my_slot = n % settings.num_teams + 1
            results[strategy].append(
                draft(
                    strategy,
                    my_slot,
                    1000 + n,
                    pool,
                    settings,
                    points,
                    espn,
                    preseason,
                    basis,
                    timing[strategy],
                )
            )

    base = results[args.strategies[0]]
    print(
        f"{'strategy':<10}{'mean':>10}{'median':>10}{'min':>10}{'max':>10}   vs {args.strategies[0]} (paired)   pick latency p50/max"
    )
    for strategy, scores in results.items():
        diffs = [a - b for a, b in zip(scores, base, strict=True)]
        wins = sum(d > 0 for d in diffs)
        t = sorted(timing[strategy])
        print(
            f"{strategy:<10}{statistics.fmean(scores):>10.0f}{statistics.median(scores):>10.0f}"
            f"{min(scores):>10.0f}{max(scores):>10.0f}   "
            f"{statistics.fmean(diffs):>+7.0f} ({wins}/{len(diffs)} better)"
            f"       {t[len(t) // 2] * 1000:>5.0f} / {t[-1] * 1000:.0f} ms"
        )


if __name__ == "__main__":
    main()
