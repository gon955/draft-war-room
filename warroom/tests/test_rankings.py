"""Ranking CRUD — the heaviest write path (SPEC 6).

  create board -> add ranking -> reorder -> patch note/tier -> delete
  a duplicate ranking is rejected by unique(board_id, player_id)
  reorder is atomic: a bad player_id in the batch changes nothing

Access control for these routes is in test_authz.py; this file is about what the
writes actually do. Two of them are worth the space on their own.

PATCH has to distinguish "note omitted" from "note set to null" — one leaves the
text alone, the other clears it — and both look like `note is None` if the route
forgets exclude_unset. And reorder is all-or-nothing: a half-applied batch would
leave the board in an order the user never asked for and cannot undo, so the
failure case asserts that every rank is untouched, not merely that it 4xx'd.
"""

import uuid

from sqlalchemy import func, select

from warroom.models import Board, Player, Ranking, Tier


def add(client, board, player, headers, **fields):
    return client.post(
        f"/boards/{board.id}/rankings",
        headers=headers,
        json={"player_id": str(player.id), **fields},
    )


def ranks(client, board, headers):
    return [
        (r["player_id"], r["user_rank"])
        for r in client.get(f"/boards/{board.id}/rankings", headers=headers).json()
    ]


class TestCreate:
    def test_add_a_ranking(self, client, auth_a, board, players):
        r = add(client, board, players[0], auth_a, user_rank=1, note="my guy", is_target=True)

        assert r.status_code == 201
        body = r.json()
        assert body["player_id"] == str(players[0].id)
        assert body["user_rank"] == 1
        assert body["note"] == "my guy"
        assert body["is_target"] is True
        assert body["is_avoid"] is False

    def test_defaults_are_applied(self, client, auth_a, board, players):
        body = add(client, board, players[0], auth_a).json()

        assert body["user_rank"] is None
        assert body["tier_id"] is None
        assert body["note"] is None
        assert body["is_target"] is False
        assert body["is_avoid"] is False

    def test_the_same_player_twice_on_one_board_is_409(self, client, auth_a, board, players):
        """unique(board_id, player_id): one row per player per board."""
        assert add(client, board, players[0], auth_a).status_code == 201
        assert add(client, board, players[0], auth_a).status_code == 409

    def test_the_same_player_on_two_boards_is_fine(
        self, client, db, auth_a, board, players, league, user_a
    ):
        from warroom.models import Board

        other = Board(user_id=user_a.id, league_id=league.id, name="Strategy 2")
        db.add(other)
        db.commit()

        assert add(client, board, players[0], auth_a).status_code == 201
        assert add(client, other, players[0], auth_a).status_code == 201

    def test_an_unknown_player_is_422(self, client, auth_a, board):
        r = client.post(
            f"/boards/{board.id}/rankings", headers=auth_a, json={"player_id": str(uuid.uuid4())}
        )

        assert r.status_code == 422

    def test_a_zero_rank_is_rejected(self, client, auth_a, board, players):
        """user_rank is 1-based (Field(ge=1)); rank 0 is a client bug."""
        assert add(client, board, players[0], auth_a, user_rank=0).status_code == 422


class TestList:
    def test_ranked_players_come_first_in_rank_order(self, client, auth_a, board, players):
        add(client, board, players[0], auth_a, user_rank=3)
        add(client, board, players[1], auth_a, user_rank=1)
        add(client, board, players[2], auth_a, user_rank=2)

        assert [rank for _, rank in ranks(client, board, auth_a)] == [1, 2, 3]

    def test_unranked_players_sort_last_not_first(self, client, auth_a, board, players):
        """NULLS LAST. Postgres would otherwise put every unranked player ahead
        of the whole board on an ascending sort."""
        add(client, board, players[0], auth_a)
        add(client, board, players[1], auth_a, user_rank=1)

        assert [rank for _, rank in ranks(client, board, auth_a)] == [1, None]

    def test_an_empty_board_lists_nothing(self, client, auth_a, board):
        assert client.get(f"/boards/{board.id}/rankings", headers=auth_a).json() == []


class TestPatch:
    def test_patch_the_note(self, client, auth_a, board, players):
        rid = add(client, board, players[0], auth_a).json()["id"]

        r = client.patch(f"/rankings/{rid}", headers=auth_a, json={"note": "elite passer"})

        assert r.status_code == 200
        assert r.json()["note"] == "elite passer"

    def test_patch_the_tier(self, client, db, auth_a, board, players):
        tier = Tier(board_id=board.id, label="Tier 1", sort_order=1)
        db.add(tier)
        db.commit()
        rid = add(client, board, players[0], auth_a).json()["id"]

        r = client.patch(f"/rankings/{rid}", headers=auth_a, json={"tier_id": str(tier.id)})

        assert r.status_code == 200
        assert r.json()["tier_id"] == str(tier.id)

    def test_an_omitted_field_is_left_alone(self, client, auth_a, board, players):
        """exclude_unset: patching the rank must not wipe the note."""
        rid = add(client, board, players[0], auth_a, note="keep me").json()["id"]

        body = client.patch(f"/rankings/{rid}", headers=auth_a, json={"user_rank": 5}).json()

        assert body["user_rank"] == 5
        assert body["note"] == "keep me"

    def test_an_explicit_null_does_clear_the_field(self, client, auth_a, board, players):
        """The other half of exclude_unset: sending null is a real instruction,
        and must be distinguishable from omitting the key."""
        rid = add(client, board, players[0], auth_a, note="remove me").json()["id"]

        body = client.patch(f"/rankings/{rid}", headers=auth_a, json={"note": None}).json()

        assert body["note"] is None

    def test_target_and_avoid_toggle_independently(self, client, auth_a, board, players):
        rid = add(client, board, players[0], auth_a, is_target=True).json()["id"]

        body = client.patch(f"/rankings/{rid}", headers=auth_a, json={"is_avoid": True}).json()

        assert body["is_target"] is True
        assert body["is_avoid"] is True

    def test_patching_a_ranking_that_does_not_exist_is_404(self, client, auth_a):
        assert (
            client.patch(
                f"/rankings/{uuid.uuid4()}", headers=auth_a, json={"note": "x"}
            ).status_code
            == 404
        )


class TestDelete:
    def test_delete_a_ranking(self, client, db, auth_a, board, players):
        rid = add(client, board, players[0], auth_a).json()["id"]

        assert client.delete(f"/rankings/{rid}", headers=auth_a).status_code == 204
        assert db.get(Ranking, uuid.UUID(rid)) is None

    def test_deleting_a_ranking_leaves_the_player_alone(self, client, db, auth_a, board, players):
        rid = add(client, board, players[0], auth_a).json()["id"]

        client.delete(f"/rankings/{rid}", headers=auth_a)

        assert db.get(type(players[0]), players[0].id) is not None

    def test_deleting_twice_is_404(self, client, auth_a, board, players):
        rid = add(client, board, players[0], auth_a).json()["id"]

        assert client.delete(f"/rankings/{rid}", headers=auth_a).status_code == 204
        assert client.delete(f"/rankings/{rid}", headers=auth_a).status_code == 404


class TestReorder:
    def test_bulk_reorder_rewrites_the_ranks(self, client, auth_a, board, players):
        for i, p in enumerate(players[:3], start=1):
            add(client, board, p, auth_a, user_rank=i)

        r = client.patch(
            f"/boards/{board.id}/rankings/reorder",
            headers=auth_a,
            json={
                "items": [
                    {"player_id": str(players[2].id), "user_rank": 1},
                    {"player_id": str(players[1].id), "user_rank": 2},
                    {"player_id": str(players[0].id), "user_rank": 3},
                ]
            },
        )

        assert r.status_code == 200
        assert [row["player_id"] for row in r.json()] == [
            str(players[2].id),
            str(players[1].id),
            str(players[0].id),
        ]

    def test_the_response_is_already_in_the_new_order(self, client, auth_a, board, players):
        """Saves the client a follow-up GET, and proves the reorder was applied
        rather than merely accepted."""
        add(client, board, players[0], auth_a, user_rank=1)
        add(client, board, players[1], auth_a, user_rank=2)

        body = client.patch(
            f"/boards/{board.id}/rankings/reorder",
            headers=auth_a,
            json={"items": [{"player_id": str(players[1].id), "user_rank": 1}]},
        ).json()

        assert [row["user_rank"] for row in body] == [1, 1]

    def test_a_partial_batch_is_allowed(self, client, auth_a, board, players):
        """Reordering the top of a board should not require sending the tail."""
        add(client, board, players[0], auth_a, user_rank=1)
        add(client, board, players[1], auth_a, user_rank=2)

        r = client.patch(
            f"/boards/{board.id}/rankings/reorder",
            headers=auth_a,
            json={"items": [{"player_id": str(players[0].id), "user_rank": 9}]},
        )

        assert r.status_code == 200

    def test_an_unranked_player_in_the_batch_changes_nothing(self, client, auth_a, board, players):
        """Atomicity is the whole point: a half-applied reorder leaves the board
        in an order the user never asked for and cannot easily undo."""
        add(client, board, players[0], auth_a, user_rank=1)
        add(client, board, players[1], auth_a, user_rank=2)
        before = ranks(client, board, auth_a)

        r = client.patch(
            f"/boards/{board.id}/rankings/reorder",
            headers=auth_a,
            json={
                "items": [
                    {"player_id": str(players[0].id), "user_rank": 7},
                    {"player_id": str(players[5].id), "user_rank": 8},
                ]
            },
        )

        assert r.status_code == 422
        assert ranks(client, board, auth_a) == before

    def test_an_empty_batch_is_422(self, client, auth_a, board, players):
        add(client, board, players[0], auth_a, user_rank=1)

        r = client.patch(f"/boards/{board.id}/rankings/reorder", headers=auth_a, json={"items": []})

        assert r.status_code == 422


class TestTheWholeFlow:
    def test_create_add_reorder_patch_delete(self, client, db, auth_a, league, players):
        """The SPEC 6 walkthrough end to end, as one user would do it."""
        board_id = client.post(
            "/boards", headers=auth_a, json={"league_id": str(league.id), "name": "Draft day"}
        ).json()["id"]

        ids = [
            client.post(
                f"/boards/{board_id}/rankings",
                headers=auth_a,
                json={"player_id": str(p.id), "user_rank": i},
            ).json()["id"]
            for i, p in enumerate(players[:3], start=1)
        ]

        client.patch(
            f"/boards/{board_id}/rankings/reorder",
            headers=auth_a,
            json={"items": [{"player_id": str(players[2].id), "user_rank": 1}]},
        )

        tier = Tier(board_id=uuid.UUID(board_id), label="Elite", sort_order=1)
        db.add(tier)
        db.commit()
        patched = client.patch(
            f"/rankings/{ids[0]}",
            headers=auth_a,
            json={"note": "reach", "tier_id": str(tier.id), "is_avoid": True},
        ).json()
        assert patched["note"] == "reach"
        assert patched["tier_id"] == str(tier.id)
        assert patched["is_avoid"] is True

        assert client.delete(f"/rankings/{ids[1]}", headers=auth_a).status_code == 204
        assert len(client.get(f"/boards/{board_id}/rankings", headers=auth_a).json()) == 2

        assert db.scalar(select(Ranking).where(Ranking.id == uuid.UUID(ids[1]))) is None


class TestTierBelongsToItsBoard:
    """A ranking's tier must come from that ranking's own board.

    Enforced twice on purpose. The composite FK on (tier_id, board_id) is the
    invariant — auto-tiering writes tier_id in bulk and never passes through
    these routes — and the route checks exist so the API answers "no such tier
    on this board" instead of letting Postgres raise an IntegrityError that
    create_ranking would have reported as "already ranked on this board".
    """

    @staticmethod
    def _other_board_tier(db, league, user):
        """A tier on a DIFFERENT board, built on the same league."""
        other = Board(user_id=user.id, league_id=league.id, name="Other strategy")
        db.add(other)
        db.commit()
        tier = Tier(board_id=other.id, label="Elite", sort_order=1)
        db.add(tier)
        db.commit()
        return tier

    def test_create_with_a_tier_from_another_board_is_422(
        self, client, db, auth_a, board, players, league, user_a
    ):
        tier = self._other_board_tier(db, league, user_a)

        r = add(client, board, players[0], auth_a, tier_id=str(tier.id))

        assert r.status_code == 422
        assert r.json()["detail"] == "No such tier on this board."
        assert db.scalar(select(func.count()).select_from(Ranking)) == 0

    def test_create_with_a_tier_that_does_not_exist_is_422(self, client, auth_a, board, players):
        # Same message as the cross-board case: telling them apart would confirm
        # that another board's tier id exists.
        r = add(client, board, players[0], auth_a, tier_id=str(uuid.uuid4()))

        assert r.status_code == 422
        assert r.json()["detail"] == "No such tier on this board."

    def test_patch_with_a_tier_from_another_board_is_422(
        self, client, db, auth_a, board, players, league, user_a
    ):
        rid = add(client, board, players[0], auth_a).json()["id"]
        tier = self._other_board_tier(db, league, user_a)

        r = client.patch(f"/rankings/{rid}", headers=auth_a, json={"tier_id": str(tier.id)})

        assert r.status_code == 422
        assert db.get(Ranking, uuid.UUID(rid)).tier_id is None

    def test_this_board_s_own_tier_is_accepted(self, client, db, auth_a, board, players):
        """The check must not be so eager it blocks the ordinary case."""
        tier = Tier(board_id=board.id, label="Elite", sort_order=1)
        db.add(tier)
        db.commit()

        r = add(client, board, players[0], auth_a, tier_id=str(tier.id))

        assert r.status_code == 201
        assert r.json()["tier_id"] == str(tier.id)

    def test_clearing_a_tier_to_null_still_works(self, client, db, auth_a, board, players):
        tier = Tier(board_id=board.id, label="Elite", sort_order=1)
        db.add(tier)
        db.commit()
        rid = add(client, board, players[0], auth_a, tier_id=str(tier.id)).json()["id"]

        r = client.patch(f"/rankings/{rid}", headers=auth_a, json={"tier_id": None})

        assert r.status_code == 200
        assert r.json()["tier_id"] is None

    def test_deleting_a_tier_clears_it_and_keeps_the_ranking(
        self, client, db, auth_a, board, players
    ):
        """ON DELETE SET NULL (tier_id) against a live database.

        The column-scoped form is load-bearing: a bare SET NULL would try to
        null board_id too, which is NOT NULL, and this delete would raise.
        """
        tier = Tier(board_id=board.id, label="Elite", sort_order=1)
        db.add(tier)
        db.commit()
        rid = uuid.UUID(add(client, board, players[0], auth_a, tier_id=str(tier.id)).json()["id"])

        db.delete(tier)
        db.commit()
        db.expire_all()

        ranking = db.get(Ranking, rid)
        assert ranking is not None
        assert ranking.tier_id is None
        assert ranking.board_id == board.id


class TestPlayerMustBeInTheLeagueSeason:
    """players is season-scoped shared reference data and the FK only says the
    row exists, so nothing but this check ties a ranking to its league's season.
    A player from another season joins to no valuation and would show up on the
    board with no value at all."""

    def test_a_player_from_another_season_is_422(self, client, db, auth_a, board, league):
        stale = Player(
            espn_player_id=99999,
            season=league.season - 1,
            name="Last Year",
            pro_team="LAL",
            positions=["PG"],
            projections={"pts": 1.0},
        )
        db.add(stale)
        db.commit()

        r = client.post(
            f"/boards/{board.id}/rankings", headers=auth_a, json={"player_id": str(stale.id)}
        )

        assert r.status_code == 422
        assert str(league.season) in r.json()["detail"]
        assert db.scalar(select(func.count()).select_from(Ranking)) == 0

    def test_a_player_in_the_league_season_is_accepted(self, client, auth_a, board, players):
        assert add(client, board, players[0], auth_a).status_code == 201
