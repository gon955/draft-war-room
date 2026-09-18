"""Tier CRUD and ordering (SPEC 6).

Access control for these routes is in test_authz.py::TestTierAccessOverHttp;
this file is about what the writes actually do. Three cases carry most of the
weight, and each of them is a bug that passes a casual smoke test.

Deleting a tier must UNGROUP its rankings, never delete them — a board of notes
and target flags is the most expensive thing in the app to lose, and the
difference between the two is invisible until it has already happened.

PATCH has to tell "color omitted" from "color set to null" — one leaves the
colour alone, the other clears it — and both look like `color is None` if the
route forgets exclude_unset. Recolouring a tier without resending its label is
the ordinary case, so `label` being required would break it.

List ordering needs a deterministic tiebreak. sort_order is not unique and
tiers, alone among the user-owned tables, has no created_at to fall back on, so
without a second sort key two tiers sharing an order swap places between reads.
"""

import uuid

from sqlalchemy import select

from warroom.models import Board, Ranking, Tier


def add_tier(client, board, headers, label="Tier 1", **fields):
    return client.post(
        f"/boards/{board.id}/tiers", headers=headers, json={"label": label, **fields}
    )


def listed(client, board, headers):
    return client.get(f"/boards/{board.id}/tiers", headers=headers).json()


class TestCreate:
    def test_create_a_tier(self, client, auth_a, board):
        r = add_tier(client, board, auth_a, label="Elite", color="#ff0000", sort_order=3)

        assert r.status_code == 201
        body = r.json()
        assert body["label"] == "Elite"
        assert body["color"] == "#ff0000"
        assert body["sort_order"] == 3
        assert body["board_id"] == str(board.id)

    def test_the_board_comes_from_the_path_not_the_body(self, client, auth_a, board, db, user_a):
        """TierCreate has no board_id, so a body cannot redirect the write."""
        other = Board(user_id=user_a.id, league_id=board.league_id, name="Other")
        db.add(other)
        db.commit()

        body = client.post(
            f"/boards/{board.id}/tiers",
            headers=auth_a,
            json={"label": "Elite", "board_id": str(other.id)},
        ).json()

        assert body["board_id"] == str(board.id)

    def test_color_defaults_to_null(self, client, auth_a, board):
        assert add_tier(client, board, auth_a).json()["color"] is None

    def test_the_first_tier_on_a_board_is_sort_order_zero(self, client, auth_a, board):
        assert add_tier(client, board, auth_a).json()["sort_order"] == 0

    def test_an_omitted_sort_order_appends(self, client, auth_a, board):
        orders = [
            add_tier(client, board, auth_a, label=f"T{i}").json()["sort_order"] for i in range(3)
        ]

        assert orders == [0, 1, 2]

    def test_appending_counts_from_the_highest_not_the_count(self, client, auth_a, board):
        """max() + 1, so a gap in the existing orders does not collide."""
        add_tier(client, board, auth_a, label="Far", sort_order=10)

        assert add_tier(client, board, auth_a, label="Next").json()["sort_order"] == 11

    def test_appending_ignores_other_boards(self, client, db, auth_a, board, user_a):
        """max(sort_order) must be scoped to this board."""
        other = Board(user_id=user_a.id, league_id=board.league_id, name="Other")
        db.add(other)
        db.commit()
        db.add(Tier(board_id=other.id, label="Theirs", sort_order=99))
        db.commit()

        assert add_tier(client, board, auth_a).json()["sort_order"] == 0

    def test_an_explicit_sort_order_of_zero_is_honoured(self, client, auth_a, board):
        """0 is falsy — a truthiness check here would silently append instead."""
        add_tier(client, board, auth_a, label="First", sort_order=5)

        assert add_tier(client, board, auth_a, label="Zero", sort_order=0).json()["sort_order"] == 0

    def test_a_missing_label_is_422(self, client, auth_a, board):
        assert client.post(f"/boards/{board.id}/tiers", headers=auth_a, json={}).status_code == 422

    def test_an_empty_label_is_422(self, client, auth_a, board):
        assert add_tier(client, board, auth_a, label="").status_code == 422

    def test_a_negative_sort_order_is_422(self, client, auth_a, board):
        assert add_tier(client, board, auth_a, sort_order=-1).status_code == 422

    def test_duplicate_labels_are_allowed(self, client, auth_a, board):
        """No unique(board_id, label): two 'Round 1' tiers are the user's call."""
        assert add_tier(client, board, auth_a, label="Same").status_code == 201
        assert add_tier(client, board, auth_a, label="Same").status_code == 201


class TestList:
    def test_an_empty_board_lists_nothing(self, client, auth_a, board):
        assert listed(client, board, auth_a) == []

    def test_tiers_come_back_in_sort_order(self, client, auth_a, board):
        add_tier(client, board, auth_a, label="Third", sort_order=2)
        add_tier(client, board, auth_a, label="First", sort_order=0)
        add_tier(client, board, auth_a, label="Second", sort_order=1)

        assert [t["label"] for t in listed(client, board, auth_a)] == [
            "First",
            "Second",
            "Third",
        ]

    def test_a_shared_sort_order_breaks_ties_by_label(self, client, auth_a, board):
        """Deterministic, so a board does not reshuffle itself between reads."""
        add_tier(client, board, auth_a, label="Beta", sort_order=0)
        add_tier(client, board, auth_a, label="Alpha", sort_order=0)

        assert [t["label"] for t in listed(client, board, auth_a)] == ["Alpha", "Beta"]

    def test_another_boards_tiers_do_not_appear(self, client, db, auth_a, board, user_a):
        """The test that catches a missing WHERE board_id."""
        other = Board(user_id=user_a.id, league_id=board.league_id, name="Other")
        db.add(other)
        db.commit()
        db.add(Tier(board_id=other.id, label="Theirs", sort_order=0))
        db.commit()
        add_tier(client, board, auth_a, label="Mine")

        assert [t["label"] for t in listed(client, board, auth_a)] == ["Mine"]


class TestPatch:
    def test_rename_a_tier(self, client, auth_a, board):
        tid = add_tier(client, board, auth_a, label="Elite").json()["id"]

        r = client.patch(f"/tiers/{tid}", headers=auth_a, json={"label": "Studs"})

        assert r.status_code == 200
        assert r.json()["label"] == "Studs"

    def test_recolour_without_resending_the_label(self, client, auth_a, board):
        """The ordinary case, and the one a required `label` would break."""
        tid = add_tier(client, board, auth_a, label="Elite").json()["id"]

        r = client.patch(f"/tiers/{tid}", headers=auth_a, json={"color": "#00ff00"})

        assert r.status_code == 200
        assert r.json()["color"] == "#00ff00"
        assert r.json()["label"] == "Elite"

    def test_an_omitted_field_is_left_alone(self, client, auth_a, board):
        """exclude_unset: patching the label must not blank the colour."""
        tid = add_tier(client, board, auth_a, label="Elite", color="#ff0000").json()["id"]

        body = client.patch(f"/tiers/{tid}", headers=auth_a, json={"label": "Studs"}).json()

        assert body["label"] == "Studs"
        assert body["color"] == "#ff0000"

    def test_an_explicit_null_clears_the_colour(self, client, auth_a, board):
        """The other half of the exclude_unset contract."""
        tid = add_tier(client, board, auth_a, color="#ff0000").json()["id"]

        body = client.patch(f"/tiers/{tid}", headers=auth_a, json={"color": None}).json()

        assert body["color"] is None

    def test_reorder_a_tier(self, client, auth_a, board):
        tid = add_tier(client, board, auth_a, sort_order=0).json()["id"]

        assert (
            client.patch(f"/tiers/{tid}", headers=auth_a, json={"sort_order": 7}).json()[
                "sort_order"
            ]
            == 7
        )

    def test_an_empty_patch_changes_nothing(self, client, auth_a, board):
        tid = add_tier(client, board, auth_a, label="Elite", color="#ff0000").json()["id"]

        body = client.patch(f"/tiers/{tid}", headers=auth_a, json={}).json()

        assert body["label"] == "Elite"
        assert body["color"] == "#ff0000"

    def test_an_empty_label_is_422(self, client, auth_a, board):
        tid = add_tier(client, board, auth_a).json()["id"]

        assert client.patch(f"/tiers/{tid}", headers=auth_a, json={"label": ""}).status_code == 422

    def test_a_tier_that_does_not_exist_is_404(self, client, auth_a):
        r = client.patch(f"/tiers/{uuid.uuid4()}", headers=auth_a, json={"label": "x"})

        assert r.status_code == 404


class TestDelete:
    def test_delete_a_tier(self, client, db, auth_a, board):
        tid = add_tier(client, board, auth_a).json()["id"]

        assert client.delete(f"/tiers/{tid}", headers=auth_a).status_code == 204
        assert db.get(Tier, uuid.UUID(tid)) is None

    def test_deleting_a_tier_ungroups_its_rankings(self, client, db, auth_a, board, players):
        """The invariant the composite FK was built around.

        SQLAlchemy loads Tier.rankings and nulls tier_id itself before issuing
        the DELETE; ON DELETE SET NULL (tier_id) is the backstop for a
        non-ORM delete. Either way the ranking — and the note and flags on it —
        must survive.
        """
        tid = add_tier(client, board, auth_a).json()["id"]
        db.add(
            Ranking(
                board_id=board.id,
                player_id=players[0].id,
                tier_id=uuid.UUID(tid),
                note="my guy",
                is_target=True,
            )
        )
        db.commit()

        assert client.delete(f"/tiers/{tid}", headers=auth_a).status_code == 204

        # expire_all is not optional: the fixture session runs with
        # expire_on_commit=False, so without it this asserts against the stale
        # in-memory tier_id and passes even when nothing worked.
        db.expire_all()
        ranking = db.scalar(select(Ranking).where(Ranking.board_id == board.id))
        assert ranking is not None
        assert ranking.tier_id is None
        assert ranking.board_id == board.id
        assert ranking.note == "my guy"
        assert ranking.is_target is True

    def test_deleting_a_tier_leaves_the_boards_other_tiers(self, client, db, auth_a, board):
        doomed = add_tier(client, board, auth_a, label="Doomed").json()["id"]
        add_tier(client, board, auth_a, label="Keeper")

        client.delete(f"/tiers/{doomed}", headers=auth_a)

        assert [t["label"] for t in listed(client, board, auth_a)] == ["Keeper"]

    def test_a_tier_that_does_not_exist_is_404(self, client, auth_a):
        assert client.delete(f"/tiers/{uuid.uuid4()}", headers=auth_a).status_code == 404


class TestTheWholeFlow:
    def test_create_list_reorder_recolour_delete(self, client, db, auth_a, board, players):
        """One user's tiering session, end to end."""
        elite = add_tier(client, board, auth_a, label="Elite").json()
        solid = add_tier(client, board, auth_a, label="Solid").json()
        assert [elite["sort_order"], solid["sort_order"]] == [0, 1]

        # Group a player under the top tier, the way auto-tiering will.
        rid = client.post(
            f"/boards/{board.id}/rankings",
            headers=auth_a,
            json={"player_id": str(players[0].id), "tier_id": elite["id"]},
        ).json()["id"]

        # Swap the two tiers and give the top one a colour.
        client.patch(f"/tiers/{elite['id']}", headers=auth_a, json={"sort_order": 1})
        client.patch(f"/tiers/{solid['id']}", headers=auth_a, json={"sort_order": 0})
        client.patch(f"/tiers/{elite['id']}", headers=auth_a, json={"color": "#ff0000"})

        tiers = listed(client, board, auth_a)
        assert [t["label"] for t in tiers] == ["Solid", "Elite"]
        assert tiers[1]["color"] == "#ff0000"

        # Dropping the tier keeps the ranking, unassigned.
        assert client.delete(f"/tiers/{elite['id']}", headers=auth_a).status_code == 204
        db.expire_all()
        assert db.get(Ranking, uuid.UUID(rid)).tier_id is None
        assert [t["label"] for t in listed(client, board, auth_a)] == ["Solid"]
