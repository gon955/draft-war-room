"""Share creation, permission levels and owner-only management (SPEC 6).

The owner-only matrix for these routes is in test_authz.py. What this file adds
is the sharing mechanics: that a permission level actually means something once
granted, that the unique constraint holds one level per user per board, and that
revoking is immediate.

Sharing is by email, which makes POST /boards/{id}/shares the one endpoint in
the app that can be pointed at the users table. An unknown recipient therefore
returns the module's generic 404 rather than "no such user" — the same answer a
hidden board gives — so it cannot be used to test whether an address has an
account here.
"""

import uuid

from sqlalchemy import select

from warroom.models import BoardShare, SharePermission


def share_with(client, board, headers, email, permission="read"):
    return client.post(
        f"/boards/{board.id}/shares",
        headers=headers,
        json={"email": email, "permission": permission},
    )


class TestCreate:
    def test_the_owner_can_share_by_email(self, client, auth_a, board, user_b):
        r = share_with(client, board, auth_a, user_b.email, "read")

        assert r.status_code == 201
        body = r.json()
        assert body["board_id"] == str(board.id)
        assert body["shared_with_user_id"] == str(user_b.id)
        assert body["permission"] == "read"

    def test_an_edit_share_is_stored_as_edit(self, client, db, auth_a, board, user_b):
        rid = share_with(client, board, auth_a, user_b.email, "edit").json()["id"]

        stored = db.get(BoardShare, uuid.UUID(rid))
        assert stored.permission is SharePermission.EDIT

    def test_the_email_is_matched_case_insensitively(self, client, auth_a, board, user_b):
        """Addresses are stored lowercased and ShareCreate normalises on the way
        in, so sharing with B@Example.com finds b@example.com."""
        r = share_with(client, board, auth_a, user_b.email.upper())

        assert r.status_code == 201
        assert r.json()["shared_with_user_id"] == str(user_b.id)

    def test_an_unknown_email_is_404_not_a_user_oracle(self, client, auth_a, board):
        r = share_with(client, board, auth_a, "nobody@example.com")

        assert r.status_code == 404
        assert r.json()["detail"] == "Not found"

    def test_the_unknown_user_404_matches_the_hidden_board_404(
        self, client, auth_a, auth_b, board, user_b
    ):
        """Byte for byte, so neither answer identifies which half was wrong."""
        unknown_user = share_with(client, board, auth_a, "nobody@example.com")
        hidden_board = share_with(client, board, auth_b, user_b.email)

        assert unknown_user.status_code == hidden_board.status_code == 404
        assert unknown_user.json() == hidden_board.json()

    def test_sharing_with_yourself_is_422(self, client, auth_a, board, user_a):
        r = share_with(client, board, auth_a, user_a.email)

        assert r.status_code == 422

    def test_sharing_twice_with_the_same_user_is_409(self, client, auth_a, board, user_b):
        assert share_with(client, board, auth_a, user_b.email, "read").status_code == 201
        assert share_with(client, board, auth_a, user_b.email, "edit").status_code == 409

    def test_an_invalid_permission_is_422(self, client, auth_a, board, user_b):
        assert share_with(client, board, auth_a, user_b.email, "admin").status_code == 422

    def test_unauthenticated_is_401(self, client, board, user_b):
        r = client.post(
            f"/boards/{board.id}/shares", json={"email": user_b.email, "permission": "read"}
        )

        assert r.status_code == 401


class TestWhatAPermissionBuys:
    def test_a_read_share_can_read_but_not_write(self, client, auth_b, board, players, read_share):
        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 200
        assert client.get(f"/boards/{board.id}/rankings", headers=auth_b).status_code == 200

        r = client.post(
            f"/boards/{board.id}/rankings", headers=auth_b, json={"player_id": str(players[0].id)}
        )
        assert r.status_code == 403

    def test_an_edit_share_can_write(self, client, auth_b, board, players, edit_share):
        r = client.post(
            f"/boards/{board.id}/rankings", headers=auth_b, json={"player_id": str(players[0].id)}
        )

        assert r.status_code == 201

    def test_neither_level_is_ownership(self, client, auth_b, board, edit_share):
        """Even an edit share cannot delete the board or reshare it."""
        assert client.delete(f"/boards/{board.id}", headers=auth_b).status_code == 403
        assert client.get(f"/boards/{board.id}/shares", headers=auth_b).status_code == 403

    def test_upgrading_is_a_delete_then_a_post(
        self, client, auth_a, auth_b, board, players, user_b, read_share
    ):
        """unique(board_id, shared_with_user_id) means the two rows can never
        coexist, so 'does B have edit access?' stays answerable."""
        assert (
            client.post(
                f"/boards/{board.id}/rankings",
                headers=auth_b,
                json={"player_id": str(players[0].id)},
            ).status_code
            == 403
        )

        client.delete(f"/shares/{read_share.id}", headers=auth_a)
        share_with(client, board, auth_a, user_b.email, "edit")

        assert (
            client.post(
                f"/boards/{board.id}/rankings",
                headers=auth_b,
                json={"player_id": str(players[0].id)},
            ).status_code
            == 201
        )


class TestList:
    def test_the_owner_sees_the_share_list(self, client, auth_a, board, read_share, user_b):
        listed = client.get(f"/boards/{board.id}/shares", headers=auth_a).json()

        assert len(listed) == 1
        assert listed[0]["shared_with_user_id"] == str(user_b.id)

    def test_an_unshared_board_lists_nothing(self, client, auth_a, board):
        assert client.get(f"/boards/{board.id}/shares", headers=auth_a).json() == []

    def test_the_recipients_email_is_not_in_the_payload(
        self, client, auth_a, board, read_share, user_b
    ):
        """ShareOut carries the user id, not the address. Sharing is by email but
        reading back a board's shares should not hand out contact details."""
        listed = client.get(f"/boards/{board.id}/shares", headers=auth_a).json()

        assert user_b.email not in str(listed)


class TestRevoke:
    def test_revoking_removes_access_immediately(
        self, client, db, auth_a, auth_b, board, read_share
    ):
        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 200

        assert client.delete(f"/shares/{read_share.id}", headers=auth_a).status_code == 204

        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 404
        assert db.get(BoardShare, read_share.id) is None

    def test_the_share_holder_cannot_revoke_anything(self, client, auth_b, board, read_share):
        assert client.delete(f"/shares/{read_share.id}", headers=auth_b).status_code == 403

    def test_revoking_a_share_that_does_not_exist_is_404(self, client, auth_a):
        assert client.delete(f"/shares/{uuid.uuid4()}", headers=auth_a).status_code == 404

    def test_revoking_leaves_the_board_and_its_rankings_alone(
        self, client, db, auth_a, auth_b, board, players, edit_share
    ):
        """B's contributions belong to the board, not to B's access to it."""
        client.post(
            f"/boards/{board.id}/rankings", headers=auth_b, json={"player_id": str(players[0].id)}
        )

        client.delete(f"/shares/{edit_share.id}", headers=auth_a)

        from warroom.models import Ranking

        assert len(db.scalars(select(Ranking).where(Ranking.board_id == board.id)).all()) == 1
        assert client.get(f"/boards/{board.id}", headers=auth_a).status_code == 200
