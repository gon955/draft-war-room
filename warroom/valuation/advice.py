"""Who to take with your next pick: the draft-time ranking, framework-free.

services/recommendation.py loads a mock from the database and hands the board
to `advise`; the strategy harness in scripts/calibration drives the same
function across thousands of simulated picks with no database at all. One
implementation, so a strategy measured offline is the one the API serves.

Two depths:

  GREEDY    the closed-form answer, draft.py steps 1-3. Each candidate is
            priced against your roster as it stands, and what you can expect
            next turn is estimated from survival probabilities.

  ROLLOUT   greedy, then the top candidates re-scored by actually playing the
            draft forward: take the candidate, let the field draft to your
            next turn, take your best response, and value the lineup the two
            picks leave you with. See `rollout_values` for why that matters.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from .domain import LeagueSettings, PlayerProjection
from .draft import (
    Eligibility,
    LiveValue,
    _best_seating,
    _valuation_of,
    autodraft,
    bench_replacement,
    default_slot_eligibility,
    expected_next_best,
    lineup_deltas,
    lineup_values_by_mask,
    live_replacement_levels,
    live_values,
    open_slots,
    pick_scores,
    picks_until_next,
    seat_slots,
    survival_probabilities,
)
from .engine import ReplacementBasis

# How many of greedy's best candidates the rollout re-scores. Past ten, the
# candidates are ones greedy already rates well below the top, and each one
# costs a simulated stretch of draft.
ROLLOUT_CANDIDATES = 10

# The simulated field takes its argmax, once. Survival already models the
# room's randomness in the greedy score; the rollout's job is the part that
# model cannot see — who takes what, in order, and what that leaves YOU — and
# one deterministic consensus run answers that without seed noise deciding
# between two close candidates.
ROLLOUT_REACH = 1


@dataclass(frozen=True)
class Rollout:
    """What one candidate's pick leads to, played forward."""

    # Your starting lineup's value after this pick and your best response at
    # the next one, in live-value units against today's replacement levels.
    value: float
    # Who that best response was: the player the rollout expects you to take
    # next turn. None when this is your last pick.
    next_player: PlayerProjection | None


@dataclass(frozen=True)
class Advice:
    """One undrafted player, ranked for your next pick."""

    live: LiveValue
    fills_open_seat: bool
    survival: float
    expected_next: float
    lineup_delta: float
    score: float
    marginal: float
    rollout: Rollout | None = None


@dataclass(frozen=True)
class RolloutField:
    """Who the simulated field is, for a rollout.

    `points` is the field's opinion of every available player — ESPN's
    projected total in production, since that is what the room sees. Defaults
    to our own when the caller has nothing better, which is a field that
    agrees with you and is the least informative choice.
    """

    points: Mapping[int, float] = field(default_factory=dict)
    candidates: int = ROLLOUT_CANDIDATES
    reach: int = ROLLOUT_REACH
    seed: int = 0


def advise(
    available: Sequence[PlayerProjection],
    rosters: Mapping[int, Sequence[PlayerProjection]],
    mine: Sequence[PlayerProjection],
    upcoming: Sequence[tuple[int, int, bool]],
    settings: LeagueSettings,
    points: Mapping[int, float],
    preseason: Mapping[int, tuple[float, float]],
    basis: ReplacementBasis = ReplacementBasis.STARTER,
    market_rank: Mapping[int, int] | None = None,
    market_weight: float = 1.0,
    rollout: RolloutField | None = None,
) -> tuple[list[Advice], int]:
    """Every available player, ranked for your next pick.

    `points` covers every player the lineup maths may meet — the available
    pool AND everyone drafted, since your incumbents are priced on the same
    basis as the candidates. `upcoming` is the unfilled board in pick order as
    (pick_number, team_slot, is_mine). Returns the advice in live-value order
    plus how many picks sit between your next turn and the one after.
    """
    available = list(available)
    board = [list(r) for r in rosters.values()]

    results = live_values(available, board, settings, points, preseason, basis=basis)

    # Re-read against YOUR seats. The replacement levels are recomputed rather
    # than passed out of live_values because the rostered players need valuing
    # on the same basis as the candidates — a lineup that compares one
    # player's VOR against another's raw projection would rate every incumbent
    # replaceable.
    replacement = live_replacement_levels(available, board, settings, points)
    needed = open_slots(mine, settings)
    delta = lineup_deltas(
        candidates=results,
        roster=mine,
        settings=settings,
        replacement=replacement,
        bench=bench_replacement(available, replacement, points),
        points=points,
    )
    # The clamp, for display. `delta` keeps the ordering the clamp destroys.
    marginal = {pid: max(0.0, d) for pid, d in delta.items()}

    # Who picks between this pick and your next one, and what each of those
    # teams is short of.
    intervening = picks_until_next(upcoming)
    survival = survival_probabilities(
        results,
        [rosters.get(slot, []) for slot in intervening],
        settings,
        market_rank=market_rank,
        market_weight=market_weight,
    )
    expected = expected_next_best(results, marginal, survival)
    scores = pick_scores(marginal, expected)

    out = []
    for result in results:
        pid = result.value.player.espn_player_id
        out.append(
            Advice(
                live=result,
                fills_open_seat=any(
                    default_slot_eligibility(slot, result.value.player.positions) for slot in needed
                ),
                survival=survival.get(pid, 1.0),
                expected_next=expected.get(pid, 0.0),
                lineup_delta=delta.get(pid, 0.0),
                score=scores.get(pid, 0.0),
                marginal=marginal.get(pid, 0.0),
            )
        )

    if rollout is not None:
        shortlist = sorted(
            out, key=lambda a: (-a.score, -a.lineup_delta, a.live.value.player.espn_player_id)
        )[: rollout.candidates]
        played = rollout_values(
            [a.live.value.player for a in shortlist],
            mine=mine,
            rosters=rosters,
            available=available,
            upcoming=upcoming,
            settings=settings,
            points=points,
            field_points={**points, **rollout.points} if rollout.points else points,
            reach=rollout.reach,
            seed=rollout.seed,
        )
        out = [replace(a, rollout=played.get(a.live.value.player.espn_player_id)) for a in out]

    return out, len(intervening)


def lineup_value(
    roster: Sequence[PlayerProjection],
    settings: LeagueSettings,
    replacement: Mapping[str, float],
    bench: float,
    points: Mapping[int, float],
    eligibility: Eligibility = default_slot_eligibility,
) -> float:
    """The best starting lineup this roster can field, in value units.

    An empty seat counts 0: it will be filled later at about replacement,
    which is what a value of 0 means. That is what lets two rosters with
    different holes be compared on one number.
    """
    seats = seat_slots(settings)
    weighted = [
        (p, _valuation_of(p, replacement, bench, points, eligibility).value) for p in roster
    ]
    return _best_seating(lineup_values_by_mask(weighted, seats, eligibility), len(seats))


def rollout_values(
    candidates: Sequence[PlayerProjection],
    mine: Sequence[PlayerProjection],
    rosters: Mapping[int, Sequence[PlayerProjection]],
    available: Sequence[PlayerProjection],
    upcoming: Sequence[tuple[int, int, bool]],
    settings: LeagueSettings,
    points: Mapping[int, float],
    field_points: Mapping[int, float],
    reach: int = ROLLOUT_REACH,
    seed: int = 0,
    eligibility: Eligibility = default_slot_eligibility,
) -> dict[int, Rollout]:
    """Each candidate's pick, played forward to your next one.

    Greedy prices every candidate against your roster AS IT STANDS. With one
    open PF/C seat and four eligible bigs, all four are priced as though each
    gets the seat — and "what you can expect next turn" is also priced against
    today's roster, so it cannot see that taking a big now means next turn's
    big is a bench piece. Playing it forward fixes both: after the candidate
    is on your roster, the field drafts the picks in between, and your best
    response is chosen against the roster you would actually have.

    The candidate is placed at your next pick, and picks before it are not
    simulated — the same assumption greedy makes, so the two rank the same
    question.

    The field drafts on `field_points` (ESPN's view, in production) with the
    STARTER basis: it is the room's behaviour being modelled, and the room is
    not solving a league-wide assignment. Everything is valued for YOU against
    one yardstick — today's replacement levels on our own points — so every
    candidate's two-pick lineup is measured in the same units.
    """
    mine_at = [i for i, (_, _, is_mine) in enumerate(upcoming) if is_mine]
    if not mine_at:
        return {}
    my_slot = upcoming[mine_at[0]][1]
    # Up to AND including your following pick: autodraft stops on reaching it.
    between = list(upcoming[mine_at[0] + 1 : mine_at[1] + 1]) if len(mine_at) > 1 else []

    available = list(available)
    board = [list(r) for r in rosters.values()]
    replacement = live_replacement_levels(available, board, settings, points, eligibility)
    bench = bench_replacement(available, replacement, points)

    out: dict[int, Rollout] = {}
    for candidate in candidates:
        cid = candidate.espn_player_id
        roster = [*mine, candidate]
        now = lineup_value(roster, settings, replacement, bench, points, eligibility)
        if not between:
            out[cid] = Rollout(value=now, next_player=None)
            continue

        others = {slot: list(r) for slot, r in rosters.items()}
        others.setdefault(my_slot, [])
        others[my_slot] = [*others[my_slot], candidate]
        pool = [p for p in available if p.espn_player_id != cid]

        taken = autodraft(
            upcoming=between,
            rosters=others,
            available=pool,
            settings=settings,
            points=field_points,
            preseason={},
            rng=random.Random(seed),
            reach=reach,
            stop_at_my_pick=True,
            basis=ReplacementBasis.STARTER,
        )
        gone = {pick.espn_player_id for pick in taken}
        left = [p for p in pool if p.espn_player_id not in gone]
        if not left:
            out[cid] = Rollout(value=now, next_player=None)
            continue

        responses = [
            LiveValue(_valuation_of(p, replacement, bench, points, eligibility), 0.0, 0.0)
            for p in left
        ]
        gain = lineup_deltas(responses, roster, settings, replacement, bench, points, eligibility)
        best = max(
            responses,
            key=lambda r: (
                gain[r.value.player.espn_player_id],
                r.value.value,
                -r.value.player.espn_player_id,
            ),
        )
        # A response that would not start adds nothing to the lineup: it is
        # depth, and a lineup's value cannot fall from adding depth.
        out[cid] = Rollout(
            value=now + max(0.0, gain[best.value.player.espn_player_id]),
            next_player=best.value.player,
        )
    return out
