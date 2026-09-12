"""Board CRUD, and boards listed for owner and share-holder alike (SPEC 6).

The 404-not-403 matrix for these same routes lives in test_authz.py and is not
repeated here; what this file covers is the behaviour once access is settled —
what a create writes, what a PATCH leaves alone, and what a DELETE takes with it.

One rule gets its own class: creating a board asks for *access* to the league,
not ownership of it. A read-share holder who can open A's board can build their
own board on the same league — otherwise a share would let you see someone's
rankings but never keep your own opinion of the same player pool.
"""

import uuid

from sqlalchemy import func, select

from warroom.models import Board, Player, Ranking, Tier


class TestCreate:
    def test_create_returns_the_board(self, client, auth_a, user_a, league):
        r = client.post(
            "/boards", headers=auth_a, json={"league_id": str(league.id), "name": "Punt FT%"}
        )

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Punt FT%"
        assert body["league_id"] == str(league.id)
        assert body["user_id"] == str(user_a.id)

    def test_several_boards_per_league_are_allowed(self, client, auth_a, league):
        """A user keeps competing strategies side by side; there is no unique
        constraint on (user_id, league_id) and there should not be."""
        for name in ("Balanced", "Punt assists", "Stars and scrubs"):
            r = client.post(
                "/boards", headers=auth_a, json={"league_id": str(league.id), "name": name}
            )
            assert r.status_code == 201

        assert len(client.get("/boards", headers=auth_a).json()) == 3

    def test_a_league_the_caller_cannot_reach_is_404(self, client, auth_b, league):
        assert (
            client.post(
                "/boards", headers=auth_b, json={"league_id": str(league.id), "name": "Mine"}
            ).status_code
            == 404
        )

    def test_a_share_holder_can_build_their_own_board_on_that_league(
        self, client, auth_b, user_b, league, read_share
    ):
        """Access, not ownership: the share reaches through the board to the
        league, so B can keep their own opinion of the same player pool."""
        r = client.post(
            "/boards", headers=auth_b, json={"league_id": str(league.id), "name": "B's take"}
        )

        assert r.status_code == 201
        assert r.json()["user_id"] == str(user_b.id)

    def test_a_league_that_does_not_exist_is_404(self, client, auth_a):
        assert (
            client.post(
                "/boards", headers=auth_a, json={"league_id": str(uuid.uuid4()), "name": "X"}
            ).status_code
            == 404
        )

    def test_an_empty_name_is_422(self, client, auth_a, league):
        assert (
            client.post(
                "/boards", headers=auth_a, json={"league_id": str(league.id), "name": ""}
            ).status_code
            == 422
        )

    def test_unauthenticated_is_401(self, client, league):
        assert (
            client.post("/boards", json={"league_id": str(league.id), "name": "X"}).status_code
            == 401
        )


class TestList:
    def test_owned_and_shared_boards_both_appear(self, client, auth_b, board, read_share):
        listed = client.get("/boards", headers=auth_b).json()

        assert [b["id"] for b in listed] == [str(board.id)]

    def test_owned_and_shared_boards_arrive_in_one_list(
        self, client, db, auth_b, board, user_b, league, read_share
    ):
        """B's own board and A's shared board come back together, once each.

        Not a test of the query's .distinct(): unique(board_id,
        shared_with_user_id) caps the outer join at one share row per board per
        user, so no fixture reachable through the API can produce the duplicate
        that .distinct() would collapse. Removing .distinct() was checked and
        breaks nothing. This covers the union, not the de-duplication.
        """
        own = Board(user_id=user_b.id, league_id=league.id, name="B's own")
        db.add(own)
        db.commit()

        listed = client.get("/boards", headers=auth_b).json()
        ids = [b["id"] for b in listed]

        assert sorted(ids) == sorted({*ids})
        assert len(ids) == 2

    def test_boards_come_back_oldest_first(self, client, auth_a, league):
        names = ["First", "Second", "Third"]
        for name in names:
            client.post("/boards", headers=auth_a, json={"league_id": str(league.id), "name": name})

        assert [b["name"] for b in client.get("/boards", headers=auth_a).json()] == names

    def test_a_user_with_nothing_gets_an_empty_list(self, client, auth_b, board):
        assert client.get("/boards", headers=auth_b).json() == []


class TestPatch:
    def test_the_owner_can_rename(self, client, auth_a, board):
        r = client.patch(f"/boards/{board.id}", headers=auth_a, json={"name": "Renamed"})

        assert r.status_code == 200
        assert r.json()["name"] == "Renamed"

    def test_an_empty_body_changes_nothing(self, client, auth_a, board):
        """model_dump(exclude_unset=True): an omitted field is left alone rather
        than nulled. A PATCH that sends nothing is a no-op, not a wipe."""
        original = board.name

        r = client.patch(f"/boards/{board.id}", headers=auth_a, json={})

        assert r.status_code == 200
        assert r.json()["name"] == original

    def test_an_empty_name_is_rejected(self, client, auth_a, board):
        assert (
            client.patch(f"/boards/{board.id}", headers=auth_a, json={"name": ""}).status_code
            == 422
        )

    def test_the_id_and_owner_are_not_patchable(self, client, auth_a, auth_b, board, user_b):
        """BoardPatch carries only `name`; extra keys are ignored, so a caller
        cannot reassign a board to themselves through the edit endpoint."""
        r = client.patch(
            f"/boards/{board.id}", headers=auth_a, json={"user_id": str(user_b.id), "name": "New"}
        )

        assert r.status_code == 200
        assert r.json()["user_id"] != str(user_b.id)


class TestDelete:
    def test_the_owner_can_delete(self, client, db, auth_a, board):
        assert client.delete(f"/boards/{board.id}", headers=auth_a).status_code == 204
        assert db.get(Board, board.id) is None

    def test_deleting_takes_the_tiers_and_rankings_with_it(
        self, client, db, auth_a, board, players
    ):
        tier = Tier(board_id=board.id, label="Elite", sort_order=1)
        db.add(tier)
        db.commit()
        db.add(Ranking(board_id=board.id, player_id=players[0].id, tier_id=tier.id))
        db.commit()

        client.delete(f"/boards/{board.id}", headers=auth_a)

        assert db.scalar(select(func.count()).select_from(Tier)) == 0
        assert db.scalar(select(func.count()).select_from(Ranking)) == 0

    def test_deleting_a_board_leaves_the_players_alone(self, client, db, auth_a, board, players):
        """Reference data owned by no user (SPEC 0.2): the cascade stops at the
        rankings and must not reach through them into the shared pool."""
        db.add(Ranking(board_id=board.id, player_id=players[0].id))
        db.commit()

        client.delete(f"/boards/{board.id}", headers=auth_a)

        assert db.scalar(select(func.count()).select_from(Ranking)) == 0
        assert db.scalar(select(func.count()).select_from(Player)) == len(players)

    def test_deleting_a_board_revokes_its_shares(
        self, client, db, auth_a, auth_b, board, read_share
    ):
        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 200

        client.delete(f"/boards/{board.id}", headers=auth_a)

        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 404
        assert client.get("/boards", headers=auth_b).json() == []
