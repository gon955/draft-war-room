"""Auto-tiering: the gap split and the bulk write it drives (SPEC 5.4, 6).

Two levels, for the same reason test_authz.py has two. TestTierBoundaries calls
the pure function with no database at all, so the algorithm can be pinned down
against hand-computed splits; the rest prove the service and the route actually
use it and write what it says.

The fixture pool is the SPEC 5.3 worked example, so the split is known exactly:

    espn_id     4     1     2     5     3     6     7
    value      25    10     0     0   -10   -10   -20

Into three tiers, 1-D k-means cuts at [1, 4] — {25}, {10, 0, 0}, {-10, -10,
-20} — for a within-tier cost of 133.33. Every expectation below was confirmed
by brute-forcing all contiguous partitions, not read back off the
implementation; that partition is the unique optimum for this pool.

The test that matters most is the one asserting a pre-existing ranking keeps
its user_rank, note and flags. SPEC 5.4 is explicit that the engine seeds the
board and the human edits it, and the bulk upsert is one careless extra key in
its ON CONFLICT set_ away from wiping a draft board's notes — which no other
test in this suite would notice.
"""

import uuid

import pytest
from sqlalchemy import select

from warroom.models import Ranking, Tier
from warroom.services.tiering import tier_boundaries


def compute(client, league, headers):
    return client.post(f"/leagues/{league.id}/valuations/compute", headers=headers)


def autotier(client, board, headers, **payload):
    return client.post(f"/boards/{board.id}/tiers/auto", headers=headers, json=payload)


def grouping(db, board, players):
    """{tier sort_order: [espn_player_id, ...]} for everything on the board."""
    espn_id = {p.id: p.espn_player_id for p in players}
    order = {t.id: t.sort_order for t in db.scalars(select(Tier).where(Tier.board_id == board.id))}

    grouped: dict[int | None, list[int]] = {}
    for row in db.scalars(select(Ranking).where(Ranking.board_id == board.id)):
        key = order[row.tier_id] if row.tier_id is not None else None
        grouped.setdefault(key, []).append(espn_id[row.player_id])
    return {k: sorted(v) for k, v in grouped.items()}


class TestTierBoundaries:
    """Pure function, no fixtures — every expectation is hand-computed."""

    def test_the_single_largest_gap_splits_two_tiers(self):
        # gaps: 1 (index 1), 39 (index 2)
        assert tier_boundaries([50.0, 49.0, 10.0], n_tiers=2) == [2]

    def test_boundaries_come_back_ascending(self):
        # gaps: 40 (1), 1 (2), 30 (3) -> the two largest are indices 1 and 3
        assert tier_boundaries([100.0, 60.0, 59.0, 29.0], n_tiers=3) == [1, 3]

    def test_equal_gaps_split_evenly(self):
        """The case that killed largest-gap splitting.

        With every gap identical there is no "largest" one, and the old
        algorithm resolved that by cutting at the first index — peeling off one
        player and leaving the rest in a single bucket. Minimising within-tier
        variance splits down the middle instead, which is the only answer that
        means anything to someone reading the board.
        """
        assert tier_boundaries([30.0, 20.0, 10.0, 0.0], n_tiers=2) == [2]

    def test_it_is_deterministic(self):
        values = [30.0, 20.0, 20.0, 10.0, 10.0, 0.0]

        assert tier_boundaries(values, n_tiers=4) == tier_boundaries(values, n_tiers=4)

    def test_index_zero_is_never_a_boundary(self):
        """A split at 0 would open an empty first tier."""
        assert 0 not in tier_boundaries([9.0, 8.0, 7.0, 1.0], n_tiers=4)

    @pytest.mark.parametrize(
        ("values", "n_tiers"),
        [([], 4), ([42.0], 4), ([50.0, 40.0], 1), ([50.0, 40.0], 0)],
    )
    def test_nothing_to_split_returns_no_boundaries(self, values, n_tiers):
        assert tier_boundaries(values, n_tiers) == []

    def test_more_tiers_than_gaps_is_clamped_not_an_error(self):
        """Asking for 10 tiers over 3 players cannot invent boundaries."""
        assert tier_boundaries([50.0, 40.0, 30.0], n_tiers=10) == [1, 2]

    def test_the_spec_pool_splits_where_the_value_cliffs_are(self):
        # {25} | {10, 0, 0} | {-10, -10, -20}: cost 133.33, the unique optimum.
        assert tier_boundaries([25.0, 10.0, 0.0, 0.0, -10.0, -10.0, -20.0], n_tiers=3) == [1, 4]

    def test_a_smooth_curve_does_not_collapse_into_one_bucket(self):
        """The defect this algorithm replaced, pinned so it cannot come back.

        A projection curve is convex-decreasing, so its biggest gaps sit at the
        very top. Cutting at the largest gaps therefore peeled players off one
        at a time: 60 players into 6 tiers came out [1, 1, 1, 1, 1, 55], and a
        55-player tier tells a drafter nothing.
        """
        values = [round(1000 * (1 - (i / 60) ** 0.65), 1) for i in range(60)]

        boundaries = tier_boundaries(values, n_tiers=6)
        sizes = [b - a for a, b in zip([0, *boundaries], [*boundaries, len(values)])]

        assert sizes == [6, 8, 10, 11, 12, 13]
        # The shape of the old failure, stated directly: a chain of singletons
        # in front of one bucket holding 92% of the pool.
        assert sizes.count(1) <= 1
        assert max(sizes) < len(values) * 0.6

    @pytest.mark.parametrize(
        "values",
        [
            [round(1000 - 16 * i, 1) for i in range(60)],
            [round(1000 * 0.92**i, 1) for i in range(60)],
        ],
        ids=["linear", "exponential"],
    )
    def test_no_curve_shape_produces_a_runaway_tier(self, values):
        """Largest-gap returned [1, 1, 1, 1, 1, 55] for these too — three
        different shapes, the same useless answer.

        Deliberately not a balance requirement. A steep exponential really does
        put half the pool in a bottom tier, because past a point every player
        is replacement-level and saying so is correct. What is never correct is
        peeling singletons off the top and calling the remainder a tier.
        """
        boundaries = tier_boundaries(values, n_tiers=6)
        sizes = [b - a for a, b in zip([0, *boundaries], [*boundaries, len(values)])]

        assert len(sizes) == 6
        assert sizes.count(1) <= 1
        assert max(sizes) <= len(values) * 0.6

    def test_genuine_cliffs_are_still_found(self):
        """What the swap did NOT give up.

        Where a pool really does fall off a cliff, k-means puts its boundaries
        in exactly the same places largest-gap did — a cliff is where
        within-tier variance is already minimal. It simply declines to invent
        cliffs that are not there.
        """
        values = [90.0] * 5 + [60.0] * 10 + [58.0] * 12 + [20.0] * 33

        assert tier_boundaries(values, n_tiers=4) == [5, 15, 27]


class TestAutotier:
    def test_it_creates_the_requested_tiers_in_order(self, client, auth_a, league, board, players):
        compute(client, league, auth_a)

        r = autotier(client, board, auth_a, n_tiers=3)

        assert r.status_code == 201
        assert [t["sort_order"] for t in r.json()] == [0, 1, 2]
        assert [t["label"] for t in r.json()] == ["Tier 1", "Tier 2", "Tier 3"]

    def test_players_are_grouped_by_value(self, client, db, auth_a, league, board, players):
        compute(client, league, auth_a)

        autotier(client, board, auth_a, n_tiers=3)

        # Boundaries [1, 4] over the value order 4, 1, 2, 5, 3, 6, 7.
        assert grouping(db, board, players) == {0: [4], 1: [1, 2, 5], 2: [3, 6, 7]}

    def test_the_best_player_lands_in_the_top_tier(
        self, client, db, auth_a, league, board, players
    ):
        compute(client, league, auth_a)

        autotier(client, board, auth_a, n_tiers=3)

        assert grouping(db, board, players)[0] == [4]

    def test_every_tier_belongs_to_this_board(self, client, db, auth_a, league, board, players):
        """What the composite FK enforces, asserted from the other side."""
        compute(client, league, auth_a)

        autotier(client, board, auth_a, n_tiers=3)

        rows = db.scalars(select(Ranking).where(Ranking.board_id == board.id)).all()
        assert rows
        assert all(db.get(Tier, row.tier_id).board_id == board.id for row in rows)

    def test_it_seeds_a_ranking_for_every_tiered_player(
        self, client, db, auth_a, league, board, players
    ):
        compute(client, league, auth_a)

        autotier(client, board, auth_a, n_tiers=3)

        rows = db.scalars(select(Ranking).where(Ranking.board_id == board.id)).all()
        assert len(rows) == len(players)

    def test_it_never_touches_the_humans_edits(self, client, db, auth_a, league, board, players):
        """SPEC 5.4: the engine seeds the board, the human edits it.

        The upsert's ON CONFLICT set_ must name tier_id and nothing else. One
        extra key here silently wipes the notes, ranks and flags that are the
        whole reason someone kept the board.
        """
        compute(client, league, auth_a)
        db.add(
            Ranking(
                board_id=board.id,
                player_id=players[0].id,
                user_rank=1,
                note="sleeper, do not reach",
                is_target=True,
                is_avoid=False,
            )
        )
        db.commit()

        autotier(client, board, auth_a, n_tiers=3)

        db.expire_all()
        kept = db.scalar(
            select(Ranking).where(Ranking.board_id == board.id, Ranking.player_id == players[0].id)
        )
        assert kept.user_rank == 1
        assert kept.note == "sleeper, do not reach"
        assert kept.is_target is True
        assert kept.tier_id is not None

    def test_it_does_not_duplicate_an_existing_ranking(
        self, client, db, auth_a, league, board, players
    ):
        """unique(board_id, player_id) — the upsert must update, not insert."""
        compute(client, league, auth_a)
        db.add(Ranking(board_id=board.id, player_id=players[0].id, note="mine"))
        db.commit()

        autotier(client, board, auth_a, n_tiers=3)

        rows = db.scalars(
            select(Ranking).where(Ranking.board_id == board.id, Ranking.player_id == players[0].id)
        ).all()
        assert len(rows) == 1

    def test_rerunning_replaces_the_tiers(self, client, db, auth_a, league, board, players):
        compute(client, league, auth_a)
        autotier(client, board, auth_a, n_tiers=3)

        autotier(client, board, auth_a, n_tiers=5)

        tiers = db.scalars(select(Tier).where(Tier.board_id == board.id)).all()
        assert len(tiers) == 5
        assert sorted(t.sort_order for t in tiers) == [0, 1, 2, 3, 4]

    def test_rerunning_regroups_the_same_rankings(self, client, db, auth_a, league, board, players):
        compute(client, league, auth_a)
        autotier(client, board, auth_a, n_tiers=3)

        autotier(client, board, auth_a, n_tiers=2)

        db.expire_all()
        rows = db.scalars(select(Ranking).where(Ranking.board_id == board.id)).all()
        assert len(rows) == len(players)
        assert all(row.tier_id is not None for row in rows)

    def test_pool_size_limits_how_deep_it_tiers(self, client, db, auth_a, league, board, players):
        compute(client, league, auth_a)

        autotier(client, board, auth_a, n_tiers=2, pool_size=3)

        grouped = grouping(db, board, players)
        assert sum(len(ids) for ids in grouped.values()) == 3
        # The three best by value: 4 (25), 1 (10), then 2 on the espn_id tiebreak.
        assert sorted(i for ids in grouped.values() for i in ids) == [1, 2, 4]

    def test_a_player_dropped_from_the_pool_loses_its_tier(
        self, client, db, auth_a, league, board, players
    ):
        """Shrinking pool_size must not leave a ranking pointing at a deleted
        tier — the old tiers go, so tier_id falls back to NULL."""
        compute(client, league, auth_a)
        autotier(client, board, auth_a, n_tiers=3)

        autotier(client, board, auth_a, n_tiers=2, pool_size=2)

        db.expire_all()
        grouped = grouping(db, board, players)
        assert sorted(grouped[None]) == [2, 3, 5, 6, 7]

    def test_tiers_from_a_previous_run_are_gone_from_the_listing(
        self, client, auth_a, league, board, players
    ):
        compute(client, league, auth_a)
        autotier(client, board, auth_a, n_tiers=4)

        autotier(client, board, auth_a, n_tiers=2)

        listed = client.get(f"/boards/{board.id}/tiers", headers=auth_a).json()
        assert len(listed) == 2

    def test_manual_tiers_are_replaced_too(self, client, db, auth_a, league, board, players):
        """Documented as destructive: auto-tiering owns the board's tier list."""
        compute(client, league, auth_a)
        client.post(f"/boards/{board.id}/tiers", headers=auth_a, json={"label": "By hand"})

        autotier(client, board, auth_a, n_tiers=2)

        labels = [
            t["label"] for t in client.get(f"/boards/{board.id}/tiers", headers=auth_a).json()
        ]
        assert "By hand" not in labels


class TestAutotierRefusals:
    def test_an_unvalued_league_is_422(self, client, auth_a, board, players):
        """Valuations first — the engine seeds tiers, so there is nothing to
        split before it has run."""
        r = autotier(client, board, auth_a)

        assert r.status_code == 422
        assert "valuations" in r.json()["detail"]

    def test_a_refused_run_leaves_no_tiers_behind(self, client, db, auth_a, board, players):
        autotier(client, board, auth_a)

        assert db.scalars(select(Tier).where(Tier.board_id == board.id)).all() == []

    def test_a_refused_run_does_not_drop_existing_tiers(self, client, auth_a, board, players):
        """The rollback has to undo the clean-slate delete as well."""
        client.post(f"/boards/{board.id}/tiers", headers=auth_a, json={"label": "By hand"})

        autotier(client, board, auth_a)

        listed = client.get(f"/boards/{board.id}/tiers", headers=auth_a).json()
        assert [t["label"] for t in listed] == ["By hand"]

    def test_a_board_that_does_not_exist_is_404(self, client, auth_a):
        r = client.post(f"/boards/{uuid.uuid4()}/tiers/auto", headers=auth_a, json={})

        assert r.status_code == 404

    @pytest.mark.parametrize("payload", [{"n_tiers": 1}, {"n_tiers": 21}, {"pool_size": 1}])
    def test_out_of_range_knobs_are_422(self, client, auth_a, board, players, payload):
        assert autotier(client, board, auth_a, **payload).status_code == 422

    def test_an_empty_body_uses_the_defaults(self, client, auth_a, league, board, players):
        compute(client, league, auth_a)

        r = client.post(f"/boards/{board.id}/tiers/auto", headers=auth_a, json={})

        assert r.status_code == 201
