"""The authorization matrix (SPEC 6) — the centrepiece of the suite.

  B GETs A's board with no share             -> 404, NOT 403
  B PATCHes a ranking on A's board, no share -> 404
  B with a READ share: GET board             -> 200
  B with a READ share: POST a ranking        -> 403
  B with an EDIT share: POST a ranking       -> 200
  B with an EDIT share: DELETE the board     -> 403  (owner-only)
  B with a READ share: PATCH a tier          -> 403
  B with an EDIT share: compute valuations   -> 403  (league owner only)
  a non-owner POSTing to /boards/{id}/shares -> 404/403

404-not-403 is the assertion that matters: 403 tells an attacker the board is
real. Test the status code explicitly, never merely `>= 400`.

Two levels, deliberately. TestAuthzHelpers calls warroom.authz directly, so a
rule can be pinned down without a route in front of it; the HTTP classes then
prove the routes actually reach for those helpers. A rule that holds in the
first and not the second means a route forgot to ask.

SPEC 8: if the schedule slips, this file is the last thing to cut.
"""

import uuid

import pytest
from fastapi import HTTPException

from warroom.authz import (
    require_board_access,
    require_board_owner,
    require_edit_access,
    require_league_access,
    require_league_owner,
)
from warroom.models import SharePermission


def status_of(excinfo) -> int:
    return excinfo.value.status_code


class TestAuthzHelpers:
    """The rules themselves, with no HTTP in the way."""

    # -- owner ------------------------------------------------------------- #

    def test_owner_has_read_access(self, db, board, user_a):
        assert require_board_access(db, board.id, user_a).id == board.id

    def test_owner_has_edit_access(self, db, board, user_a):
        assert require_edit_access(db, board.id, user_a).id == board.id

    def test_owner_is_owner(self, db, board, user_a):
        assert require_board_owner(db, board.id, user_a).id == board.id

    # -- stranger: always 404 ---------------------------------------------- #

    @pytest.mark.parametrize(
        "helper", [require_board_access, require_edit_access, require_board_owner]
    )
    def test_stranger_gets_404_never_403(self, db, board, user_b, helper):
        # The whole point: B must not be able to tell A's board apart from an id
        # that was never issued.
        with pytest.raises(HTTPException) as excinfo:
            helper(db, board.id, user_b)
        assert status_of(excinfo) == 404

    @pytest.mark.parametrize(
        "helper", [require_board_access, require_edit_access, require_board_owner]
    )
    def test_a_board_that_never_existed_is_also_404(self, db, user_a, helper):
        # Same status, same detail as the case above — that equivalence is the
        # security property, so assert both halves of it.
        with pytest.raises(HTTPException) as excinfo:
            helper(db, uuid.uuid4(), user_a)
        assert status_of(excinfo) == 404

    def test_the_two_404s_are_indistinguishable(self, db, board, user_b):
        with pytest.raises(HTTPException) as denied:
            require_board_access(db, board.id, user_b)
        with pytest.raises(HTTPException) as absent:
            require_board_access(db, uuid.uuid4(), user_b)

        assert (denied.value.status_code, denied.value.detail) == (
            absent.value.status_code,
            absent.value.detail,
        )

    # -- read share --------------------------------------------------------- #

    def test_read_share_grants_read(self, db, board, user_b, read_share):
        assert require_board_access(db, board.id, user_b).id == board.id

    def test_read_share_is_403_for_edit(self, db, board, user_b, read_share):
        # 403 and not 404: B can already see this board, so there is nothing
        # left to conceal.
        with pytest.raises(HTTPException) as excinfo:
            require_edit_access(db, board.id, user_b)
        assert status_of(excinfo) == 403

    def test_read_share_is_403_for_owner_only(self, db, board, user_b, read_share):
        with pytest.raises(HTTPException) as excinfo:
            require_board_owner(db, board.id, user_b)
        assert status_of(excinfo) == 403

    # -- edit share --------------------------------------------------------- #

    def test_edit_share_grants_read(self, db, board, user_b, edit_share):
        assert require_board_access(db, board.id, user_b).id == board.id

    def test_edit_share_grants_edit(self, db, board, user_b, edit_share):
        assert require_edit_access(db, board.id, user_b).id == board.id

    def test_edit_share_is_still_not_ownership(self, db, board, user_b, edit_share):
        # The distinction that makes sharing safe to offer: an edit collaborator
        # can change what is on the board but cannot delete it or reshare it.
        with pytest.raises(HTTPException) as excinfo:
            require_board_owner(db, board.id, user_b)
        assert status_of(excinfo) == 403

    # -- leagues ------------------------------------------------------------ #

    def test_league_owner_has_access(self, db, league, user_a):
        assert require_league_access(db, league.id, user_a).id == league.id

    def test_stranger_cannot_see_a_league(self, db, league, user_b):
        with pytest.raises(HTTPException) as excinfo:
            require_league_access(db, league.id, user_b)
        assert status_of(excinfo) == 404

    def test_a_board_share_reaches_through_to_the_league(self, db, league, user_b, read_share):
        # Without this a read share would open a board whose every player is
        # invisible, which is not a share at all.
        assert require_league_access(db, league.id, user_b).id == league.id

    def test_share_holder_is_not_the_league_owner(self, db, league, user_b, edit_share):
        with pytest.raises(HTTPException) as excinfo:
            require_league_owner(db, league.id, user_b)
        assert status_of(excinfo) == 403

    def test_stranger_gets_404_from_league_owner_check(self, db, league, user_b):
        with pytest.raises(HTTPException) as excinfo:
            require_league_owner(db, league.id, user_b)
        assert status_of(excinfo) == 404

    # -- the share row itself ------------------------------------------------ #

    def test_revoking_a_share_revokes_access(self, db, board, user_b, read_share):
        assert require_board_access(db, board.id, user_b).id == board.id

        db.delete(read_share)
        db.commit()

        with pytest.raises(HTTPException) as excinfo:
            require_board_access(db, board.id, user_b)
        assert status_of(excinfo) == 404

    def test_permission_is_compared_as_an_enum_not_a_string(
        self, db, board, user_b, share_board_with
    ):
        # If models.py lost values_callable, the stored label would flip to
        # 'EDIT' and a string comparison here would quietly deny every edit.
        share = share_board_with(board, user_b, SharePermission.EDIT)
        assert share.permission is SharePermission.EDIT
        assert require_edit_access(db, board.id, user_b).id == board.id


class TestBoardAccessOverHttp:
    """SPEC 6's matrix, exactly as worded, through the real routes."""

    def test_b_gets_as_board_with_no_share_is_404_not_403(self, client, board, auth_b):
        response = client.get(f"/boards/{board.id}", headers=auth_b)
        assert response.status_code == 404, "403 here would confirm the board exists"

    def test_a_nonexistent_board_is_the_same_404(self, client, board, auth_b):
        denied = client.get(f"/boards/{board.id}", headers=auth_b)
        absent = client.get(f"/boards/{uuid.uuid4()}", headers=auth_b)
        assert denied.status_code == absent.status_code
        assert denied.json() == absent.json()

    def test_owner_gets_200(self, client, board, auth_a):
        response = client.get(f"/boards/{board.id}", headers=auth_a)
        assert response.status_code == 200
        assert response.json()["id"] == str(board.id)

    def test_read_share_gets_200(self, client, board, auth_b, read_share):
        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 200

    def test_unauthenticated_is_401(self, client, board):
        # 401 and not 404: the caller has not failed authorization yet, they
        # have failed authentication, and telling them so is not a leak.
        assert client.get(f"/boards/{board.id}").status_code == 401

    def test_listing_shows_owned_and_shared_but_not_others(
        self, client, board, auth_a, auth_b, read_share
    ):
        assert [b["id"] for b in client.get("/boards", headers=auth_a).json()] == [str(board.id)]
        assert [b["id"] for b in client.get("/boards", headers=auth_b).json()] == [str(board.id)]

    def test_listing_hides_a_board_with_no_share(self, client, board, auth_b):
        assert client.get("/boards", headers=auth_b).json() == []


class TestBoardMutationOverHttp:
    def test_read_share_cannot_patch(self, client, board, auth_b, read_share):
        response = client.patch(f"/boards/{board.id}", json={"name": "x"}, headers=auth_b)
        assert response.status_code == 403

    def test_edit_share_can_patch(self, client, board, auth_b, edit_share):
        response = client.patch(f"/boards/{board.id}", json={"name": "Renamed"}, headers=auth_b)
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    def test_stranger_patching_is_404(self, client, board, auth_b):
        assert (
            client.patch(f"/boards/{board.id}", json={"name": "x"}, headers=auth_b).status_code
            == 404
        )

    def test_edit_share_cannot_delete_the_board(self, client, board, auth_b, edit_share):
        # The line that makes sharing safe to offer at all.
        assert client.delete(f"/boards/{board.id}", headers=auth_b).status_code == 403

    def test_owner_can_delete(self, client, board, auth_a):
        assert client.delete(f"/boards/{board.id}", headers=auth_a).status_code == 204
        assert client.get(f"/boards/{board.id}", headers=auth_a).status_code == 404

    def test_stranger_deleting_is_404(self, client, board, auth_b):
        assert client.delete(f"/boards/{board.id}", headers=auth_b).status_code == 404


class TestRankingAccessOverHttp:
    def test_b_patches_as_ranking_with_no_share_is_404(
        self, client, db, board, players, auth_a, auth_b
    ):
        created = client.post(
            f"/boards/{board.id}/rankings",
            json={"player_id": str(players[0].id)},
            headers=auth_a,
        )
        assert created.status_code == 201

        response = client.patch(
            f"/rankings/{created.json()['id']}", json={"note": "mine now"}, headers=auth_b
        )
        assert response.status_code == 404

    def test_read_share_can_list_rankings(self, client, board, auth_b, read_share):
        assert client.get(f"/boards/{board.id}/rankings", headers=auth_b).status_code == 200

    def test_read_share_cannot_post_a_ranking(self, client, board, players, auth_b, read_share):
        response = client.post(
            f"/boards/{board.id}/rankings",
            json={"player_id": str(players[0].id)},
            headers=auth_b,
        )
        assert response.status_code == 403

    def test_edit_share_can_post_a_ranking(self, client, board, players, auth_b, edit_share):
        response = client.post(
            f"/boards/{board.id}/rankings",
            json={"player_id": str(players[0].id)},
            headers=auth_b,
        )
        assert response.status_code == 201

    def test_stranger_cannot_post_a_ranking(self, client, board, players, auth_b):
        response = client.post(
            f"/boards/{board.id}/rankings",
            json={"player_id": str(players[0].id)},
            headers=auth_b,
        )
        assert response.status_code == 404

    def test_a_ranking_id_that_does_not_exist_is_404(self, client, auth_a):
        response = client.patch(f"/rankings/{uuid.uuid4()}", json={"note": "x"}, headers=auth_a)
        assert response.status_code == 404


class TestTierAccessOverHttp:
    """Tiers over HTTP (SPEC 4: board access to read, edit access to write).

    The read-share-cannot-PATCH case is the one that carries this class. Every
    other case here passes even if the id-scoped routes call
    require_board_access instead of require_edit_access, so without it a tier
    route that hands write access to every read-share holder looks green.
    """

    @staticmethod
    def _tier_on_as_board(client, board, auth_a):
        created = client.post(f"/boards/{board.id}/tiers", json={"label": "Elite"}, headers=auth_a)
        assert created.status_code == 201
        return created.json()["id"]

    def test_b_lists_as_tiers_with_no_share_is_404(self, client, board, auth_b):
        assert client.get(f"/boards/{board.id}/tiers", headers=auth_b).status_code == 404

    def test_b_patches_as_tier_with_no_share_is_404(self, client, board, auth_a, auth_b):
        tier_id = self._tier_on_as_board(client, board, auth_a)

        response = client.patch(f"/tiers/{tier_id}", json={"label": "mine now"}, headers=auth_b)

        assert response.status_code == 404

    def test_b_deletes_as_tier_with_no_share_is_404(self, client, board, auth_a, auth_b):
        tier_id = self._tier_on_as_board(client, board, auth_a)

        assert client.delete(f"/tiers/{tier_id}", headers=auth_b).status_code == 404

    def test_read_share_can_list_tiers(self, client, board, auth_b, read_share):
        # Otherwise a shared board renders with no grouping at all.
        assert client.get(f"/boards/{board.id}/tiers", headers=auth_b).status_code == 200

    def test_read_share_cannot_post_a_tier(self, client, board, auth_b, read_share):
        response = client.post(f"/boards/{board.id}/tiers", json={"label": "Elite"}, headers=auth_b)
        assert response.status_code == 403

    def test_read_share_cannot_patch_a_tier(self, client, board, auth_a, auth_b, read_share):
        # 403, not 404: B can already see this tier, so there is nothing left to
        # conceal — only the operation is too privileged.
        tier_id = self._tier_on_as_board(client, board, auth_a)

        response = client.patch(f"/tiers/{tier_id}", json={"label": "mine now"}, headers=auth_b)

        assert response.status_code == 403

    def test_read_share_cannot_delete_a_tier(self, client, board, auth_a, auth_b, read_share):
        tier_id = self._tier_on_as_board(client, board, auth_a)

        assert client.delete(f"/tiers/{tier_id}", headers=auth_b).status_code == 403

    def test_edit_share_can_post_a_tier(self, client, board, auth_b, edit_share):
        response = client.post(f"/boards/{board.id}/tiers", json={"label": "Elite"}, headers=auth_b)
        assert response.status_code == 201

    def test_edit_share_can_patch_and_delete_a_tier(
        self, client, board, auth_a, auth_b, edit_share
    ):
        tier_id = self._tier_on_as_board(client, board, auth_a)

        patched = client.patch(f"/tiers/{tier_id}", json={"label": "Studs"}, headers=auth_b)

        assert patched.status_code == 200
        assert client.delete(f"/tiers/{tier_id}", headers=auth_b).status_code == 204

    def test_stranger_cannot_post_a_tier(self, client, board, auth_b):
        response = client.post(f"/boards/{board.id}/tiers", json={"label": "Elite"}, headers=auth_b)
        assert response.status_code == 404

    def test_a_tier_id_that_does_not_exist_is_404(self, client, auth_a):
        response = client.patch(f"/tiers/{uuid.uuid4()}", json={"label": "x"}, headers=auth_a)
        assert response.status_code == 404


class TestValuationAccessOverHttp:
    """Players and valuations (SPEC 4: league access to read, owner to compute).

    The split that matters is read versus compute. Both GETs take
    require_league_access, which a board share grants — a read share that shows
    the board but not one player on it is worthless. Computing is
    require_league_owner, because it overwrites reference data every
    share-holder reads and costs the owner's own ESPN-derived pool.

    B reaches this league only through the share on A's board, which is exactly
    the path require_league_access walks.
    """

    def test_read_share_can_list_players(self, client, league, auth_b, players, read_share):
        assert client.get(f"/leagues/{league.id}/players", headers=auth_b).status_code == 200

    def test_read_share_can_list_valuations(self, client, league, auth_b, players, read_share):
        assert client.get(f"/leagues/{league.id}/valuations", headers=auth_b).status_code == 200

    def test_read_share_cannot_compute(self, client, league, auth_b, players, read_share):
        # 403, not 404: B can already see this league's pool, so there is
        # nothing left to conceal — only the operation is too privileged.
        response = client.post(f"/leagues/{league.id}/valuations/compute", headers=auth_b)
        assert response.status_code == 403

    def test_edit_share_cannot_compute_either(self, client, league, auth_b, players, edit_share):
        # An edit share is permission on a BOARD, never on the league behind it.
        response = client.post(f"/leagues/{league.id}/valuations/compute", headers=auth_b)
        assert response.status_code == 403

    def test_stranger_cannot_list_players(self, client, league, auth_b, players):
        assert client.get(f"/leagues/{league.id}/players", headers=auth_b).status_code == 404

    def test_stranger_cannot_list_valuations(self, client, league, auth_b, players):
        assert client.get(f"/leagues/{league.id}/valuations", headers=auth_b).status_code == 404

    def test_stranger_cannot_compute(self, client, league, auth_b, players):
        response = client.post(f"/leagues/{league.id}/valuations/compute", headers=auth_b)
        assert response.status_code == 404

    def test_the_owner_can_do_all_three(self, client, league, auth_a, players):
        assert (
            client.post(f"/leagues/{league.id}/valuations/compute", headers=auth_a).status_code
            == 200
        )
        assert client.get(f"/leagues/{league.id}/players", headers=auth_a).status_code == 200
        assert client.get(f"/leagues/{league.id}/valuations", headers=auth_a).status_code == 200

    def test_a_league_id_that_does_not_exist_is_404(self, client, auth_a):
        assert client.get(f"/leagues/{uuid.uuid4()}/players", headers=auth_a).status_code == 404


class TestAutotierAccessOverHttp:
    """POST /boards/{id}/tiers/auto — edit access, like every other tier write.

    Not in SPEC 4's endpoint list (SPEC 5.4 and SPEC 8 require the feature), so
    its place in the matrix is worth stating explicitly rather than inferring:
    it rewrites a board's tiers and its rankings' tier_id, which is a board
    write, so an edit share is enough and a read share is not.
    """

    @staticmethod
    def _valued(client, league, auth_a):
        assert (
            client.post(f"/leagues/{league.id}/valuations/compute", headers=auth_a).status_code
            == 200
        )

    def test_read_share_cannot_autotier(
        self, client, league, board, auth_a, auth_b, players, read_share
    ):
        self._valued(client, league, auth_a)

        response = client.post(f"/boards/{board.id}/tiers/auto", headers=auth_b, json={})

        assert response.status_code == 403

    def test_edit_share_can_autotier(
        self, client, league, board, auth_a, auth_b, players, edit_share
    ):
        self._valued(client, league, auth_a)

        response = client.post(f"/boards/{board.id}/tiers/auto", headers=auth_b, json={})

        assert response.status_code == 201

    def test_stranger_cannot_autotier(self, client, league, board, auth_a, auth_b, players):
        self._valued(client, league, auth_a)

        response = client.post(f"/boards/{board.id}/tiers/auto", headers=auth_b, json={})

        assert response.status_code == 404

    def test_a_board_id_that_does_not_exist_is_404(self, client, auth_a):
        response = client.post(f"/boards/{uuid.uuid4()}/tiers/auto", headers=auth_a, json={})
        assert response.status_code == 404


class TestShareManagementOverHttp:
    def test_non_owner_with_no_share_posting_a_share_is_404(self, client, board, user_a, auth_b):
        response = client.post(
            f"/boards/{board.id}/shares",
            json={"email": user_a.email, "permission": "read"},
            headers=auth_b,
        )
        assert response.status_code == 404

    def test_edit_share_holder_cannot_reshare(self, client, board, user_b, auth_b, edit_share):
        # An edit collaborator must not be able to widen access on your board.
        response = client.post(
            f"/boards/{board.id}/shares",
            json={"email": "c@example.com", "permission": "read"},
            headers=auth_b,
        )
        assert response.status_code == 403

    def test_non_owner_cannot_list_shares(self, client, board, auth_b, read_share):
        assert client.get(f"/boards/{board.id}/shares", headers=auth_b).status_code == 403

    def test_owner_can_share_by_email(self, client, board, user_b, auth_a):
        response = client.post(
            f"/boards/{board.id}/shares",
            json={"email": user_b.email, "permission": "edit"},
            headers=auth_a,
        )
        assert response.status_code == 201
        assert response.json()["permission"] == "edit"
        assert response.json()["shared_with_user_id"] == str(user_b.id)

    def test_sharing_with_an_unknown_email_is_404_not_a_user_oracle(self, client, board, auth_a):
        response = client.post(
            f"/boards/{board.id}/shares",
            json={"email": "nobody@example.com", "permission": "read"},
            headers=auth_a,
        )
        assert response.status_code == 404

    def test_double_sharing_is_409(self, client, board, user_b, auth_a, read_share):
        response = client.post(
            f"/boards/{board.id}/shares",
            json={"email": user_b.email, "permission": "edit"},
            headers=auth_a,
        )
        assert response.status_code == 409

    def test_owner_can_revoke(self, client, board, auth_a, auth_b, read_share):
        assert client.delete(f"/shares/{read_share.id}", headers=auth_a).status_code == 204
        assert client.get(f"/boards/{board.id}", headers=auth_b).status_code == 404

    def test_share_holder_cannot_revoke_their_own_share(self, client, auth_b, read_share):
        assert client.delete(f"/shares/{read_share.id}", headers=auth_b).status_code == 403
