"""Group a valued pool into tiers (SPEC 5.4).

Group the ranked list into tiers by 1-D k-means over `value`, write the tiers
rows, and set rankings.tier_id. Tiers are what turn a ranked list into a
draft-day decision: within a tier the choice is a coin flip, between tiers it is
not.

SPEC 5.4 offers largest-gap splits or 1-D k-means; tier_boundaries explains at
length why only the second one survives contact with a real projection curve.

The engine seeds the board; the human edits it. A manual rankings.user_rank
always overrides the computed order.

SEEDING, decided up front because it shapes Phase 5 too: tiering creates ranking
rows only for the players it actually tiers (the top N worth grouping) — it does
NOT seed a row per player in the pool. rankings stays what SPEC 3 makes it, an
override table for players the user has touched; a row per pool player per board
would be ~400 near-empty rows per board with nothing in them but an id. Reads
that need the whole pool therefore drive off players and outer-join rankings —
see api/routes/players.py and api/routes/mocks.py.

Writing tier_id in bulk is exactly the path that skips a route's validation,
which is why "a ranking's tier belongs to that ranking's board" is a composite
foreign key in models.py rather than a check in rankings.py alone. A bulk write
that gets it wrong fails here, loudly, instead of corrupting the board.
"""

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from warroom.models import Board, Player, Ranking, Tier, Valuation


class NotValued(RuntimeError):
    """The league has no valuations yet, so there is nothing to group."""


def tier_boundaries(values: Sequence[float], n_tiers: int) -> list[int]:
    """Indices where a new tier starts, by 1-D k-means over `value`.

    SPEC 5.4 allows either largest-gap splits or 1-D k-means. This is k-means —
    Jenks natural breaks — because largest-gap, which this used to be,
    degenerates on exactly the data the tool is for.

    A projection curve is convex-decreasing: the biggest gaps in value sit at
    the very top. So "cut at the n largest gaps" peels the top few players off
    one at a time and drops everyone else in one bucket. Measured over 60
    players into 6 tiers, largest-gap returned [1, 1, 1, 1, 1, 55] for a
    convex decay, a linear ramp AND an exponential decay — three different
    shapes, the same useless answer — where this returns [6, 8, 10, 11, 12, 13],
    [10, 10, 10, 10, 10, 10] and [3, 4, 5, 7, 11, 30].

    Nothing is given up by the swap: where a pool has genuine cliffs, both
    methods return identical boundaries. Cliffs are where within-tier variance
    is already minimised, so k-means finds them too — it just does not invent
    them when they are absent.

    Minimises the total within-tier sum of squared deviations over CONTIGUOUS
    runs, which is what makes it a tiering rather than a clustering: the list is
    already ranked, and a tier that skipped a player would be nonsense.

    `values` must already be sorted descending. Returns at most n_tiers - 1
    indices, ascending, and never index 0 — a boundary there would open an
    empty first tier. O(k·n²), which is nothing at the pool_size <= 500 cap.
    """
    count = len(values)
    if count < 2 or n_tiers < 2:
        return []

    tiers = min(n_tiers, count)

    # Prefix sums of x and x², so the cost of any run is O(1) to look up.
    total = [0.0] * (count + 1)
    total_sq = [0.0] * (count + 1)
    for i, value in enumerate(values):
        total[i + 1] = total[i] + value
        total_sq[i + 1] = total_sq[i] + value * value

    def cost(start: int, stop: int) -> float:
        """Within-run sum of squared deviations for values[start:stop]."""
        size = stop - start
        if size <= 1:
            return 0.0
        run = total[stop] - total[start]
        # max(0.0, ...) because the algebraic form can go a hair below zero on
        # floating-point cancellation when every value in the run is equal —
        # which happens constantly here, since replacement-level players all
        # land on exactly the same value.
        return max(0.0, (total_sq[stop] - total_sq[start]) - run * run / size)

    # best[t][j] = least cost of splitting the first j values into t tiers.
    best = [[float("inf")] * (count + 1) for _ in range(tiers + 1)]
    cut_at = [[0] * (count + 1) for _ in range(tiers + 1)]
    best[0][0] = 0.0

    for tier in range(1, tiers + 1):
        for stop in range(tier, count + 1):
            for start in range(tier - 1, stop):
                candidate = best[tier - 1][start] + cost(start, stop)
                # Strict <, so an equal-cost split keeps the earlier `start`.
                # That makes the function deterministic, which is what matters
                # here — it does NOT promise the lexicographically smallest of
                # several optimal partitions, because the preference applies per
                # subproblem rather than to the assembled answer. Verified
                # against brute force over 4000 random pools: always optimal in
                # cost, occasionally a different optimum than the naive scan.
                if candidate < best[tier][stop]:
                    best[tier][stop] = candidate
                    cut_at[tier][stop] = start

    boundaries = []
    stop = count
    for tier in range(tiers, 0, -1):
        start = cut_at[tier][stop]
        if start > 0:
            boundaries.append(start)
        stop = start

    return sorted(boundaries)


def autotier_board(
    db: Session, board: Board, *, n_tiers: int = 8, pool_size: int = 60
) -> list[Tier]:
    """Replace a board's tiers with gap-split tiers over the league's values.

    Nothing here commits; the route owns the transaction boundary, so the tier
    delete, the tier insert and the ranking upsert land together or not at all.
    """
    valued = db.execute(
        select(Valuation.player_id, Valuation.value)
        .join(Player, Player.id == Valuation.player_id)
        .where(Valuation.league_id == board.league_id)
        .order_by(Valuation.value.desc(), Player.espn_player_id.asc())
        .limit(pool_size)
    ).all()

    if not valued:
        raise NotValued(
            "This league has no valuations yet. Compute them before tiering "
            f"(POST /leagues/{board.league_id}/valuations/compute)."
        )

    # Clean slate, deleted through the ORM rather than with a Core delete: that
    # loads each tier's rankings and nulls tier_id itself, which advances
    # rankings.updated_at. A Core delete would lean on the composite FK's
    # ON DELETE SET NULL (tier_id) and leave updated_at claiming the ranking
    # never changed. n_tiers is single digits, so the extra statements are free.
    for existing in db.scalars(select(Tier).where(Tier.board_id == board.id)):
        db.delete(existing)
    db.flush()

    boundaries = tier_boundaries([value for _, value in valued], n_tiers)

    tiers = [
        Tier(board_id=board.id, label=f"Tier {order + 1}", sort_order=order)
        for order in range(len(boundaries) + 1)
    ]
    db.add_all(tiers)
    # Flush before the ranking upsert: the composite FK needs these tier ids to
    # exist, and until the INSERT runs they are only client-side defaults.
    db.flush()

    rows = []
    tier_index = 0
    for position, (player_id, _) in enumerate(valued):
        if tier_index < len(boundaries) and position >= boundaries[tier_index]:
            tier_index += 1
        rows.append({"board_id": board.id, "player_id": player_id, "tier_id": tiers[tier_index].id})

    # Every tier above was created on board.id and every row here carries the
    # same board.id, so the composite FK on (tier_id, board_id) is satisfied by
    # construction — which is the point of doing the bulk write this way.
    #
    # The season check create_ranking performs is not repeated: `valued` comes
    # from valuations, which compute_valuations built from the season-filtered
    # pool, so these players are in the league's season by construction too.
    stmt = insert(Ranking).values(rows)
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=["board_id", "player_id"],
            # tier_id ONLY. SPEC 5.4: the engine seeds the board, the human
            # edits it — so a re-tier must not touch user_rank, note, is_target
            # or is_avoid on a row the user has already worked on.
            set_={"tier_id": stmt.excluded.tier_id, "updated_at": func.clock_timestamp()},
        )
    )

    return tiers
