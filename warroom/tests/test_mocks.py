"""Mock drafts, picks and best-available (SPEC 6).

The fixture league is 2 teams x roster_size 13, so a mock lays out 26 picks,
and the pool is the SPEC 5.3 worked example ranked [4, 1, 2, 5, 3, 6, 7].

Three tests here are worth more than the rest put together.

best-available excludes drafted players with a NOT IN over mock_picks, and the
pick rows are created EMPTY — so that subquery is full of NULLs, and
`x NOT IN (1, NULL)` is NULL, never true, for every row. Drop the IS NOT NULL
guard and the endpoint returns an empty page while still answering 200, which
reads as "everyone is already drafted" on pick one. Measured: 0 available
without the guard, 6 with it.

A manual user_rank has to beat a better computed value, because SPEC 5.4 says
the engine seeds the board and the human edits it. Asserted with the pool's
WORST player ranked first, so value ordering cannot produce the same answer by
accident.

And re-sending the same player to the pick they already occupy must not 409
against itself. The duplicate-player check excludes the pick being written; get
that wrong and correcting a neighbouring typo becomes impossible.
"""

import uuid

import pytest
from sqlalchemy import event, func, select

from warroom.api.routes.mocks import generate_picks
from warroom.models import MockDraft, MockPick, Player, Ranking, Valuation
from warroom.tests.conftest import SEASON, SPEC_53_EXPECTED_RANKING

PICKS_PER_MOCK = 26  # num_teams 2 x roster_size 13


def compute(client, league, headers):
    return client.post(f"/leagues/{league.id}/valuations/compute", headers=headers)


def make_mock(client, board, headers, name="Mock 1", my_draft_slot=1):
    return client.post(
        f"/boards/{board.id}/mocks",
        headers=headers,
        json={"name": name, "my_draft_slot": my_draft_slot},
    )


def draft(client, mock_id, headers, pick_number, player_id):
    return client.post(
        f"/mocks/{mock_id}/picks",
        headers=headers,
        json={"pick_number": pick_number, "player_id": player_id},
    )


def available(client, mock_id, headers, query=""):
    return client.get(f"/mocks/{mock_id}/best-available?limit=200{query}", headers=headers).json()


def available_ids(client, mock_id, headers, query=""):
    return [
        i["player"]["espn_player_id"] for i in available(client, mock_id, headers, query)["items"]
    ]


@pytest.fixture
def mock_id(client, auth_a, league, board, players):
    """A valued league with one mock laid out on A's board."""
    compute(client, league, auth_a)
    return make_mock(client, board, auth_a).json()["id"]


class TestGeneratePicks:
    """Pure function, no fixtures — checked against a hand-drawn board.

    pick   1  2  3  4 | 5  6  7  8 | 9 10 11 12
    round  1  1  1  1 | 2  2  2  2 | 3  3  3  3
    slot   1  2  3  4 | 4  3  2  1 | 1  2  3  4
    """

    @pytest.fixture
    def board_of_12(self):
        return generate_picks(uuid.uuid4(), num_teams=4, rounds=3, my_draft_slot=2)

    def test_every_team_picks_every_round(self, board_of_12):
        assert len(board_of_12) == 12

    def test_pick_numbers_are_contiguous_from_one(self, board_of_12):
        assert [p["pick_number"] for p in board_of_12] == list(range(1, 13))

    def test_odd_rounds_ascend_and_even_rounds_descend(self, board_of_12):
        by_round = {}
        for pick in board_of_12:
            by_round.setdefault(pick["round"], []).append(pick["team_slot"])

        assert by_round == {1: [1, 2, 3, 4], 2: [4, 3, 2, 1], 3: [1, 2, 3, 4]}

    def test_the_turn_gives_one_team_back_to_back_picks(self, board_of_12):
        """The whole reason draft position is worth modelling."""
        slots = {p["pick_number"]: p["team_slot"] for p in board_of_12}

        assert slots[4] == slots[5]
        assert slots[8] == slots[9]

    def test_is_mine_marks_only_my_slot(self, board_of_12):
        assert [p["pick_number"] for p in board_of_12 if p["is_mine"]] == [2, 7, 10]

    def test_every_team_picks_once_per_round(self, board_of_12):
        per_slot = {}
        for pick in board_of_12:
            per_slot.setdefault(pick["team_slot"], []).append(pick["round"])

        assert all(sorted(rounds) == [1, 2, 3] for rounds in per_slot.values())

    def test_picks_start_empty(self, board_of_12):
        assert all(pick["player_id"] is None for pick in board_of_12)

    def test_a_one_team_league_still_lays_out(self):
        picks = generate_picks(uuid.uuid4(), num_teams=1, rounds=3, my_draft_slot=1)

        assert [p["team_slot"] for p in picks] == [1, 1, 1]
        assert all(p["is_mine"] for p in picks)

    def test_a_slot_nobody_holds_marks_nothing(self):
        picks = generate_picks(uuid.uuid4(), num_teams=4, rounds=2, my_draft_slot=9)

        assert not any(p["is_mine"] for p in picks)


class TestCreateMock:
    def test_it_lays_out_the_whole_board(self, client, db, auth_a, board, players):
        r = make_mock(client, board, auth_a)

        assert r.status_code == 201
        assert r.json()["picks_total"] == PICKS_PER_MOCK
        assert r.json()["picks_made"] == 0
        assert db.scalar(select(func.count()).select_from(MockPick)) == PICKS_PER_MOCK

    def test_the_picks_belong_to_this_mock(self, client, db, auth_a, board, players):
        mock = make_mock(client, board, auth_a).json()

        picks = db.scalars(
            select(MockPick).where(MockPick.mock_draft_id == uuid.UUID(mock["id"]))
        ).all()
        assert len(picks) == PICKS_PER_MOCK

    def test_the_board_comes_from_the_path(self, client, auth_a, board, players):
        assert make_mock(client, board, auth_a).json()["board_id"] == str(board.id)

    def test_two_mocks_on_one_board_do_not_collide(self, client, db, auth_a, board, players):
        """unique(mock_draft_id, pick_number) is per draft, so pick 1 exists twice."""
        make_mock(client, board, auth_a, name="Early pick")
        r = make_mock(client, board, auth_a, name="Late pick", my_draft_slot=2)

        assert r.status_code == 201
        assert db.scalar(select(func.count()).select_from(MockPick)) == 2 * PICKS_PER_MOCK

    def test_a_slot_past_the_league_size_is_422(self, client, auth_a, board, players):
        """A 2-team league has no slot 99. The schema cannot know that."""
        r = make_mock(client, board, auth_a, my_draft_slot=99)

        assert r.status_code == 422
        assert "1 and 2" in r.json()["detail"]

    def test_slot_zero_is_422(self, client, auth_a, board, players):
        assert make_mock(client, board, auth_a, my_draft_slot=0).status_code == 422

    def test_an_empty_name_is_422(self, client, auth_a, board, players):
        assert make_mock(client, board, auth_a, name="").status_code == 422

    def test_a_league_with_no_roster_settings_is_422(self, client, db, auth_a, board, players):
        """sync writes roster_size=0 when ESPN returns no lineupSlotCounts, and
        zero rounds is a mock with no picks that still answers 201."""
        board.league.roster_size = 0
        db.commit()

        r = make_mock(client, board, auth_a)

        assert r.status_code == 422
        assert "ync" in r.json()["detail"]

    def test_a_refused_mock_writes_nothing(self, client, db, auth_a, board, players):
        board.league.roster_size = 0
        db.commit()

        make_mock(client, board, auth_a)

        assert db.scalar(select(func.count()).select_from(MockDraft)) == 0
        assert db.scalar(select(func.count()).select_from(MockPick)) == 0

    def test_a_board_that_does_not_exist_is_404(self, client, auth_a):
        r = client.post(
            f"/boards/{uuid.uuid4()}/mocks", headers=auth_a, json={"name": "M", "my_draft_slot": 1}
        )

        assert r.status_code == 404


class TestListMocks:
    def test_an_empty_board_lists_nothing(self, client, auth_a, board):
        assert client.get(f"/boards/{board.id}/mocks", headers=auth_a).json() == []

    def test_progress_counts_only_picks_actually_made(
        self, client, auth_a, board, players, mock_id
    ):
        """count(*) is every slot; count(player_id) skips the NULLs."""
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 2, str(players[1].id))

        listed = client.get(f"/boards/{board.id}/mocks", headers=auth_a).json()

        assert listed[0]["picks_total"] == PICKS_PER_MOCK
        assert listed[0]["picks_made"] == 2

    def test_clearing_a_pick_lowers_the_count(self, client, auth_a, board, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 1, None)

        listed = client.get(f"/boards/{board.id}/mocks", headers=auth_a).json()

        assert listed[0]["picks_made"] == 0

    def test_another_boards_mocks_do_not_appear(
        self, client, db, auth_a, board, players, user_a, mock_id
    ):
        from warroom.models import Board

        other = Board(user_id=user_a.id, league_id=board.league_id, name="Other")
        db.add(other)
        db.commit()
        make_mock(client, other, auth_a, name="Theirs")

        listed = client.get(f"/boards/{board.id}/mocks", headers=auth_a).json()

        assert [m["name"] for m in listed] == ["Mock 1"]


class TestListPicks:
    def test_the_board_comes_back_in_pick_order(self, client, auth_a, mock_id):
        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()

        assert len(picks) == PICKS_PER_MOCK
        assert [p["pick_number"] for p in picks] == list(range(1, PICKS_PER_MOCK + 1))

    def test_it_snakes(self, client, auth_a, mock_id):
        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        first_four = [(p["round"], p["team_slot"]) for p in picks[:4]]

        assert first_four == [(1, 1), (1, 2), (2, 2), (2, 1)]

    def test_picks_start_empty(self, client, auth_a, mock_id):
        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()

        assert all(p["player_id"] is None for p in picks)

    def test_a_mock_that_does_not_exist_is_404(self, client, auth_a):
        assert client.get(f"/mocks/{uuid.uuid4()}/picks", headers=auth_a).status_code == 404


class TestSetPick:
    def test_recording_a_pick(self, client, auth_a, players, mock_id):
        r = draft(client, mock_id, auth_a, 1, str(players[0].id))

        assert r.status_code == 200
        assert r.json()["player_id"] == str(players[0].id)
        assert r.json()["pick_number"] == 1

    def test_the_pick_keeps_its_place_in_the_order(self, client, auth_a, players, mock_id):
        body = draft(client, mock_id, auth_a, 3, str(players[0].id)).json()

        assert (body["round"], body["team_slot"]) == (2, 2)

    def test_clearing_a_pick(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))

        r = draft(client, mock_id, auth_a, 1, None)

        assert r.status_code == 200
        assert r.json()["player_id"] is None

    def test_overwriting_a_pick_with_a_different_player(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))

        r = draft(client, mock_id, auth_a, 1, str(players[1].id))

        assert r.status_code == 200
        assert r.json()["player_id"] == str(players[1].id)

    def test_the_same_player_twice_in_one_draft_is_409(self, client, auth_a, players, mock_id):
        """unique(mock_draft_id, pick_number) constrains the SLOT, not the
        player — nothing in the schema stops one player going twice."""
        draft(client, mock_id, auth_a, 1, str(players[0].id))

        r = draft(client, mock_id, auth_a, 2, str(players[0].id))

        assert r.status_code == 409
        assert "pick 1" in r.json()["detail"]

    def test_resending_the_same_player_to_their_own_pick_is_not_409(
        self, client, auth_a, players, mock_id
    ):
        """The duplicate check excludes the pick being written. Without that
        exclusion a pick 409s against itself and a typo cannot be corrected."""
        draft(client, mock_id, auth_a, 1, str(players[0].id))

        assert draft(client, mock_id, auth_a, 1, str(players[0].id)).status_code == 200

    def test_the_same_player_in_a_different_draft_is_fine(
        self, client, auth_a, board, players, mock_id
    ):
        """Two mocks are independent boards of play.

        Deliberately DIFFERENT pick numbers. With both at pick 1 the
        `pick_number != payload.pick_number` clause hides the row all by
        itself, and the duplicate check could forget which draft it is scoped
        to while this still passed.
        """
        other = make_mock(client, board, auth_a, name="Second mock").json()["id"]
        draft(client, mock_id, auth_a, 1, str(players[0].id))

        assert draft(client, other, auth_a, 5, str(players[0].id)).status_code == 200

    def test_the_duplicate_check_is_scoped_to_this_draft(
        self, client, auth_a, board, players, mock_id
    ):
        """The same assertion from the other side: a player taken in another
        mock must not block this one, at any pick number."""
        other = make_mock(client, board, auth_a, name="Second mock").json()["id"]
        draft(client, other, auth_a, 4, str(players[0].id))

        assert draft(client, mock_id, auth_a, 9, str(players[0].id)).status_code == 200

    def test_a_pick_number_past_the_end_is_422(self, client, auth_a, players, mock_id):
        r = draft(client, mock_id, auth_a, 999, str(players[0].id))

        assert r.status_code == 422
        assert "pick number" in r.json()["detail"]

    def test_a_player_that_does_not_exist_is_422(self, client, auth_a, mock_id):
        assert draft(client, mock_id, auth_a, 1, str(uuid.uuid4())).status_code == 422

    def test_a_player_from_another_season_is_422(self, client, db, auth_a, mock_id):
        stale = Player(
            espn_player_id=4321,
            season=SEASON - 1,
            name="Last Year",
            pro_team="LAL",
            positions=["PG"],
            projections={"pts": 1.0},
        )
        db.add(stale)
        db.commit()

        r = draft(client, mock_id, auth_a, 1, str(stale.id))

        assert r.status_code == 422
        assert "season" in r.json()["detail"]

    def test_a_mock_that_does_not_exist_is_404(self, client, auth_a, players):
        r = draft(client, str(uuid.uuid4()), auth_a, 1, str(players[0].id))

        assert r.status_code == 404


class TestBestAvailable:
    def test_everyone_is_available_before_the_draft_starts(self, client, auth_a, players, mock_id):
        """THE test. The pick rows exist and are empty, so the NOT IN subquery
        is full of NULLs; without an IS NOT NULL guard this returns an empty
        page and still answers 200."""
        body = available(client, mock_id, auth_a)

        assert body["total"] == len(players)
        assert len(body["items"]) == len(players)

    def test_it_is_ordered_by_value_before_anyone_is_ranked(self, client, auth_a, players, mock_id):
        assert available_ids(client, mock_id, auth_a) == SPEC_53_EXPECTED_RANKING

    def test_a_drafted_player_disappears(self, client, auth_a, players, mock_id):
        top = next(p for p in players if p.espn_player_id == 4)

        draft(client, mock_id, auth_a, 1, str(top.id))

        assert available_ids(client, mock_id, auth_a) == [1, 2, 5, 3, 6, 7]

    def test_the_total_drops_with_each_pick(self, client, auth_a, players, mock_id):
        for i, player in enumerate(players[:3], start=1):
            draft(client, mock_id, auth_a, i, str(player.id))

            assert available(client, mock_id, auth_a)["total"] == len(players) - i

    def test_clearing_a_pick_returns_the_player_to_the_pool(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 1, None)

        assert available(client, mock_id, auth_a)["total"] == len(players)

    def test_another_drafts_picks_do_not_remove_players(
        self, client, auth_a, board, players, mock_id
    ):
        """Two mocks on one board are independent boards of play."""
        other = make_mock(client, board, auth_a, name="Second").json()["id"]
        draft(client, other, auth_a, 1, str(players[0].id))

        assert available(client, mock_id, auth_a)["total"] == len(players)

    def test_a_manual_rank_beats_a_better_value(self, client, db, auth_a, board, players, mock_id):
        """SPEC 5.4: the engine seeds the board, the human edits it.

        Player 7 is the WORST in the pool by value (-20.0), so putting them
        first is something value ordering cannot produce by accident.
        """
        worst = next(p for p in players if p.espn_player_id == 7)
        db.add(Ranking(board_id=board.id, player_id=worst.id, user_rank=1))
        db.commit()

        assert available_ids(client, mock_id, auth_a)[0] == 7

    def test_unranked_players_still_fall_back_to_value(
        self, client, db, auth_a, board, players, mock_id
    ):
        worst = next(p for p in players if p.espn_player_id == 7)
        db.add(Ranking(board_id=board.id, player_id=worst.id, user_rank=1))
        db.commit()

        assert available_ids(client, mock_id, auth_a) == [7, 4, 1, 2, 5, 3, 6]

    def test_the_ranking_is_attached(self, client, db, auth_a, board, players, mock_id):
        top = next(p for p in players if p.espn_player_id == 4)
        db.add(Ranking(board_id=board.id, player_id=top.id, note="my guy", is_target=True))
        db.commit()

        first = available(client, mock_id, auth_a)["items"][0]

        assert first["ranking"]["note"] == "my guy"
        assert first["ranking"]["is_target"] is True

    def test_untouched_players_carry_no_ranking(self, client, auth_a, players, mock_id):
        assert all(i["ranking"] is None for i in available(client, mock_id, auth_a)["items"])

    def test_another_boards_rankings_do_not_leak_in(
        self, client, db, auth_a, board, players, user_a, mock_id
    ):
        """The ranking join is scoped to THIS board in its ON clause.

        Without that scope another board's notes and ranks appear on this
        board's draft view, and — because user_rank is the first sort key —
        silently reorder it. A strategy board is exactly the thing a user keeps
        several of, so this is not a hypothetical.
        """
        from warroom.models import Board

        other_board = Board(user_id=user_a.id, league_id=board.league_id, name="Other strategy")
        db.add(other_board)
        db.commit()
        worst = next(p for p in players if p.espn_player_id == 7)
        db.add(
            Ranking(
                board_id=other_board.id,
                player_id=worst.id,
                user_rank=1,
                note="their guy",
                is_target=True,
            )
        )
        db.commit()

        body = available(client, mock_id, auth_a)

        # Untouched on THIS board: no ranking, and the order is still by value.
        assert all(item["ranking"] is None for item in body["items"])
        assert [i["player"]["espn_player_id"] for i in body["items"]] == (SPEC_53_EXPECTED_RANKING)

    def test_the_valuation_is_attached(self, client, auth_a, players, mock_id):
        first = available(client, mock_id, auth_a)["items"][0]

        assert first["valuation"]["value"] == 25.0
        assert first["valuation"]["assigned_slot"] == "C"

    def test_an_unvalued_league_still_lists_the_pool(self, client, auth_a, board, players):
        """Mocking before computing is allowed; the ordering just falls back."""
        mock = make_mock(client, board, auth_a).json()["id"]

        body = available(client, mock, auth_a)

        assert body["total"] == len(players)
        assert all(i["valuation"] is None for i in body["items"])

    @pytest.mark.parametrize(("position", "expected"), [("PG", 3), ("C", 3), ("SF", 1)])
    def test_filter_by_position(self, client, auth_a, mock_id, position, expected):
        body = available(client, mock_id, auth_a, f"&position={position}")

        assert body["total"] == expected
        assert all(position in i["player"]["positions"] for i in body["items"])

    def test_the_position_filter_also_excludes_drafted_players(
        self, client, auth_a, players, mock_id
    ):
        top_centre = next(p for p in players if p.espn_player_id == 4)
        draft(client, mock_id, auth_a, 1, str(top_centre.id))

        body = available(client, mock_id, auth_a, "&position=C")

        assert body["total"] == 2

    def test_pagination_does_not_change_the_total(self, client, auth_a, players, mock_id):
        body = client.get(
            f"/mocks/{mock_id}/best-available?limit=2&offset=2", headers=auth_a
        ).json()

        assert body["total"] == len(players)
        assert [i["player"]["espn_player_id"] for i in body["items"]] == [2, 5]

    @pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1", "position=G"])
    def test_bad_query_is_422(self, client, auth_a, mock_id, query):
        r = client.get(f"/mocks/{mock_id}/best-available?{query}", headers=auth_a)

        assert r.status_code == 422

    def test_a_mock_that_does_not_exist_is_404(self, client, auth_a):
        r = client.get(f"/mocks/{uuid.uuid4()}/best-available", headers=auth_a)

        assert r.status_code == 404


class TestTheWholeFlow:
    def test_sync_value_tier_mock_and_draft(self, client, db, auth_a, league, board, players):
        """What the tool is actually for, end to end."""
        assert client.post(f"/leagues/{league.id}/sync", headers=auth_a).status_code == 200
        assert (
            client.post(f"/leagues/{league.id}/valuations/compute", headers=auth_a).status_code
            == 200
        )
        assert (
            client.post(
                f"/boards/{board.id}/tiers/auto", headers=auth_a, json={"n_tiers": 3}
            ).status_code
            == 201
        )

        mock = make_mock(client, board, auth_a, my_draft_slot=2).json()
        assert mock["picks_total"] == PICKS_PER_MOCK

        # Draft the top of the board at my first pick.
        best = available(client, mock["id"], auth_a)["items"][0]
        assert best["player"]["espn_player_id"] == 4
        assert best["ranking"]["tier_id"] is not None  # auto-tiering seeded it

        my_first = next(
            p
            for p in client.get(f"/mocks/{mock['id']}/picks", headers=auth_a).json()
            if p["is_mine"]
        )
        drafted = draft(client, mock["id"], auth_a, my_first["pick_number"], best["player"]["id"])
        assert drafted.status_code == 200
        assert drafted.json()["is_mine"] is True

        after = available(client, mock["id"], auth_a)
        assert after["total"] == len(players) - 1
        assert best["player"]["espn_player_id"] not in [
            i["player"]["espn_player_id"] for i in after["items"]
        ]
        listed = client.get(f"/boards/{board.id}/mocks", headers=auth_a).json()
        assert listed[0]["picks_made"] == 1


def recommended(client, mock_id, headers, query=""):
    return client.get(f"/mocks/{mock_id}/recommendation?limit=200{query}", headers=headers).json()


def recommended_ids(client, mock_id, headers, query=""):
    return [
        i["player"]["espn_player_id"] for i in recommended(client, mock_id, headers, query)["items"]
    ]


def live_of(payload, espn_id):
    return next(i["live"] for i in payload["items"] if i["player"]["espn_player_id"] == espn_id)


class TestRecommendation:
    """Draft-aware re-valuation (step 1: the live replacement level).

    The fixture league is 2 teams x {PG:1, C:1, UTIL:1}, so league-wide demand
    is 2 seats per slot and two picks at one position saturate it. Preseason
    levels over the SPEC 5.3 pool are PG 40, C 20, UTIL 45.
    """

    def test_an_untouched_board_matches_the_preseason_ranking(self, client, auth_a, mock_id):
        """The anchor. Before anyone picks, nothing has drained, so the live
        answer must be exactly the static one — otherwise every mid-draft
        number this endpoint produces is suspect.

        Pinned on sort=value deliberately: the default sort now folds in your
        roster and the field's likely picks, which is a different question.
        This test is about the live re-valuation alone."""
        assert recommended_ids(client, mock_id, auth_a, "&sort=value") == SPEC_53_EXPECTED_RANKING

    def test_an_untouched_board_reports_no_shift(self, client, auth_a, mock_id):
        payload = recommended(client, mock_id, auth_a)
        assert all(i["live"]["replacement_shift"] == 0 for i in payload["items"])
        assert all(i["live"]["value_change"] == 0 for i in payload["items"])

    def test_a_drafted_player_leaves_the_board(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        assert 1 not in recommended_ids(client, mock_id, auth_a)
        assert recommended(client, mock_id, auth_a)["total"] == 6

    def test_draining_a_slot_lifts_the_players_left_in_it(self, client, auth_a, players, mock_id):
        """The whole point of the feature, end to end.

        Picks 1 and 2 take both guards — one to each team, so PG's two seats
        are gone and the slot saturates. Piotr (30) can now only be credited at
        UTIL, whose level has fallen 45 -> 30 as the pool shrank. He was -10
        preseason and is 0 now: a 10-point gain with no change to the player.

        best-available cannot express this. It would still rank him 5th on a
        number computed over a pool that no longer exists.
        """
        draft(client, mock_id, auth_a, 1, str(players[0].id))  # Paul Guard, team 1
        draft(client, mock_id, auth_a, 2, str(players[1].id))  # Peter Guard, team 2

        piotr = live_of(recommended(client, mock_id, auth_a), 3)
        assert piotr["assigned_slot"] == "UTIL"
        assert piotr["value"] == 0
        assert piotr["replacement_shift"] == -10  # credited against PG 40, now UTIL 30
        assert piotr["value_change"] == 10

    def test_a_drained_slot_reorders_the_board(self, client, auth_a, players, mock_id):
        # Both guards gone: C stays at 20 (centres 45/20/10, 2 seats), UTIL
        # falls to 30. Piotr 30-30=0 and Colin 20-20=0 tie on espn_player_id;
        # Sam 25-30=-5 climbs past Curtis 10-20=-10.
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 2, str(players[1].id))
        assert recommended_ids(client, mock_id, auth_a, "&sort=value") == [4, 3, 5, 7, 6]

    def test_a_players_own_slot_saturating_does_not_inflate_them(
        self, client, auth_a, players, mock_id
    ):
        """The 0.0-replacement trap.

        It takes five picks to strand a guard, and the snake is why: picks 1-2
        take PG on each team, pick 3 comes back to team 2 (round 2 descends)
        and pick 5 returns to team 1. Both PG seats and both UTIL seats are
        then gone, so Piotr fits nowhere in the league.

        Valued against a 0.0 replacement he would take his whole 30 points and
        lead the board precisely because nobody can start him. He must instead
        be measured against C, the shallowest level still in play, and sit
        below the centre who actually fits it.
        """
        draft(client, mock_id, auth_a, 1, str(players[0].id))  # Paul   -> team 1 PG
        draft(client, mock_id, auth_a, 2, str(players[1].id))  # Peter  -> team 2 PG
        draft(client, mock_id, auth_a, 3, str(players[6].id))  # Sam    -> team 2 UTIL
        draft(client, mock_id, auth_a, 4, str(players[4].id))  # Colin  -> team 1 C
        draft(client, mock_id, auth_a, 5, str(players[5].id))  # Curtis -> team 1 UTIL

        payload = recommended(client, mock_id, auth_a)
        piotr = live_of(payload, 3)
        assert piotr["assigned_slot"] == "BENCH"
        # C is the only slot with demand left, and Cal (45) is the last one
        # needed, so Piotr is 30 - 45 = -15, not the 30 a 0.0 baseline gives.
        assert piotr["value"] == -15
        # sort=value: this is a claim about the live re-valuation, not about
        # which of the two is the better pick given the field.
        assert recommended_ids(client, mock_id, auth_a, "&sort=value") == [4, 3]

    def test_the_position_filter_does_not_change_the_numbers(
        self, client, auth_a, players, mock_id
    ):
        """Filtering is a view of the answer, never an input to it.

        Sam is the pool's only SF and is credited at UTIL, level 45. Narrow the
        pool to SF BEFORE computing and UTIL's level would be read off a
        one-player pool — clamped to Sam himself, making his value 0 and the
        board a lie. He must keep the -20 he has on the full board.
        """
        unfiltered = live_of(recommended(client, mock_id, auth_a), 7)
        filtered = live_of(recommended(client, mock_id, auth_a, "&position=SF"), 7)
        assert unfiltered["value"] == -20
        assert filtered == unfiltered

    def test_the_total_respects_the_position_filter(self, client, auth_a, mock_id):
        payload = recommended(client, mock_id, auth_a, "&position=C")
        assert payload["total"] == 3
        assert [i["player"]["espn_player_id"] for i in payload["items"]] == [4, 5, 6]

    def test_paging_slices_without_changing_the_total(self, client, auth_a, mock_id):
        page = client.get(f"/mocks/{mock_id}/recommendation?limit=2&offset=1", headers=auth_a)
        payload = page.json()
        assert payload["total"] == 7
        assert [i["player"]["espn_player_id"] for i in payload["items"]] == [1, 2]

    def test_the_preseason_valuation_travels_with_the_live_one(self, client, auth_a, mock_id):
        """Both numbers, so a UI can show the movement rather than just the
        result. The cache must also be untouched by this read."""
        top = recommended(client, mock_id, auth_a)["items"][0]
        assert top["valuation"]["value"] == 25.0
        assert top["live"]["value"] == 25.0

    def test_an_unvalued_league_is_422_not_an_empty_page(self, client, auth_a, board, players):
        """No compute() here. An empty page would read as "no good players
        left" on pick one; the caller needs to be told to value the league."""
        unvalued = make_mock(client, board, auth_a, name="Unvalued").json()["id"]
        response = client.get(f"/mocks/{unvalued}/recommendation", headers=auth_a)
        assert response.status_code == 422
        assert "valuations" in response.json()["detail"]

    def test_a_mock_that_does_not_exist_is_404(self, client, auth_a):
        response = client.get(f"/mocks/{uuid.uuid4()}/recommendation", headers=auth_a)
        assert response.status_code == 404

    def test_another_users_mock_is_404(self, client, auth_a, auth_b, mock_id):
        """Denied reads are 404, never 403 — the authz spine's rule, and this
        endpoint goes through the same _mock_for as every other."""
        assert client.get(f"/mocks/{mock_id}/recommendation", headers=auth_b).status_code == 404

    def test_it_never_writes_to_the_valuations_cache(
        self, client, db, auth_a, players, mock_id, league
    ):
        """Read-only, asserted. valuations is a per-LEAGUE cache and a board
        can hold several mocks; a write here would let one mock's draft state
        overwrite another's idea of what a player is worth."""
        before = db.scalar(
            select(func.count()).select_from(Valuation).where(Valuation.league_id == league.id)
        )
        stamps = sorted(
            db.scalars(select(Valuation.computed_at).where(Valuation.league_id == league.id))
        )

        draft(client, mock_id, auth_a, 1, str(players[0].id))
        recommended(client, mock_id, auth_a)

        db.expire_all()
        assert (
            db.scalar(
                select(func.count()).select_from(Valuation).where(Valuation.league_id == league.id)
            )
            == before
        )
        assert (
            sorted(
                db.scalars(select(Valuation.computed_at).where(Valuation.league_id == league.id))
            )
            == stamps
        )


class TestPicksCarryThePlayer:
    """GET /mocks/{id}/picks nests the player, not just their id.

    The bug this pins: a drafted player is by definition absent from
    best-available, so a UI joining the two lists has no source for the name
    and renders a raw UUID on every filled slot. The board endpoint is the
    only place the name can come from.
    """

    def test_an_empty_slot_has_no_player(self, client, auth_a, mock_id):
        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        assert all(p["player"] is None for p in picks)
        assert all(p["player_id"] is None for p in picks)

    def test_a_filled_slot_carries_the_whole_player(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        first = next(p for p in picks if p["pick_number"] == 1)

        assert first["player"] is not None
        assert first["player"]["name"] == players[0].name
        assert first["player"]["id"] == str(players[0].id)
        # espn_player_id rides along, which is what the headshot URL is built
        # from — without it the board could show a name but never a face.
        assert first["player"]["espn_player_id"] == players[0].espn_player_id

    def test_clearing_a_pick_drops_the_player_again(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 1, None)
        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        assert next(p for p in picks if p["pick_number"] == 1)["player"] is None

    def test_the_board_is_one_query_per_request_not_one_per_pick(
        self, client, db, auth_a, players, mock_id
    ):
        """selectinload, asserted.

        26 picks here, 130 in a real 10-team league. Lazy loading `player` on
        each row is one query per FILLED slot, so the cost of opening the
        board would grow with how far the draft has got — worst exactly when
        the screen matters most.
        """
        for number, player in enumerate(players[:5], start=1):
            draft(client, mock_id, auth_a, number, str(player.id))

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        finally:
            event.remove(engine, "before_cursor_execute", record)

        selects_on_players = [s for s in statements if "FROM players" in s]
        assert len([p for p in picks if p["player"] is not None]) == 5
        # One IN-load for all five, not five separate lookups.
        assert len(selects_on_players) <= 1, selects_on_players


def lineup(client, mock_id, headers):
    return client.get(f"/mocks/{mock_id}/lineup", headers=headers).json()


class TestLineup:
    """GET /mocks/{id}/lineup — your own roster over the league's slots.

    The fixture league is {PG:1, C:1, UTIL:1} with roster_size 13, so three
    starting seats and a ten-man bench. Your slot is 1, which in a 2-team
    snake owns picks 1, 4, 5, 8, 9 ...
    """

    def test_an_undrafted_mock_is_all_empty_seats(self, client, auth_a, mock_id):
        body = lineup(client, mock_id, auth_a)
        assert [(s["slot"], s["index"]) for s in body["starters"]] == [
            ("PG", 1),
            ("C", 1),
            ("UTIL", 1),
        ]
        assert all(s["player"] is None for s in body["starters"])
        assert body["bench"] == []
        assert body["picks_made"] == 0

    def test_bench_size_is_roster_size_minus_the_starting_slots(self, client, auth_a, mock_id):
        """Derived, not stored. roster_slots counts starters only (3) and
        roster_size is the whole roster (13), so the bench is 10 and neither
        column alone can say so."""
        body = lineup(client, mock_id, auth_a)
        assert body["roster_size"] == 13
        assert body["bench_size"] == 10

    def test_only_my_picks_appear(self, client, auth_a, players, mock_id):
        """Pick 1 is mine, pick 2 is the other team's. A lineup that showed
        both would be the draft board, not a roster."""
        draft(client, mock_id, auth_a, 1, str(players[0].id))  # mine
        draft(client, mock_id, auth_a, 2, str(players[3].id))  # theirs

        body = lineup(client, mock_id, auth_a)
        filled = [s["player"]["espn_player_id"] for s in body["starters"] if s["player"]]
        assert filled == [1]
        assert body["picks_made"] == 1

    def test_a_player_lands_in_their_scarcest_slot(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))  # Paul Guard, PG
        draft(client, mock_id, auth_a, 4, str(players[3].id))  # Cal Center, C

        seats = {s["slot"]: s["player"] for s in lineup(client, mock_id, auth_a)["starters"]}
        assert seats["PG"]["name"] == "Paul Guard"
        assert seats["C"]["name"] == "Cal Center"
        assert seats["UTIL"] is None

    def test_the_overflow_guard_falls_to_util_then_the_bench(
        self, client, auth_a, players, mock_id
    ):
        # My picks in a 2-team snake are 1, 4, 5. Three guards: PG, then UTIL,
        # then nowhere to start.
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 4, str(players[1].id))
        draft(client, mock_id, auth_a, 5, str(players[2].id))

        body = lineup(client, mock_id, auth_a)
        seats = {s["slot"]: s["player"] for s in body["starters"]}
        assert seats["PG"]["espn_player_id"] == 1
        assert seats["UTIL"]["espn_player_id"] == 2
        assert [p["espn_player_id"] for p in body["bench"]] == [3]
        assert body["picks_made"] == 3

    def test_clearing_a_pick_empties_its_seat(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        draft(client, mock_id, auth_a, 1, None)

        body = lineup(client, mock_id, auth_a)
        assert all(s["player"] is None for s in body["starters"])
        assert body["picks_made"] == 0

    def test_picks_remaining_counts_my_unfilled_slots(self, client, auth_a, players, mock_id):
        """13 rounds, 2 teams, so I own 13 picks."""
        body = lineup(client, mock_id, auth_a)
        assert body["picks_remaining"] == 13

        draft(client, mock_id, auth_a, 1, str(players[0].id))
        assert lineup(client, mock_id, auth_a)["picks_remaining"] == 12

    def test_the_seats_agree_with_what_the_recommender_thinks_is_taken(
        self, client, auth_a, players, mock_id
    ):
        """One placement, two views. If the lineup says a player is starting
        at C while the recommender still counts that seat open, every
        replacement level downstream is computed against the wrong league."""
        draft(client, mock_id, auth_a, 1, str(players[3].id))  # Cal Center -> C

        seats = {s["slot"]: s["player"] for s in lineup(client, mock_id, auth_a)["starters"]}
        assert seats["C"]["espn_player_id"] == 4

        # C demand is now 1 of 2 league-wide, so the C level is read off the
        # best remaining centre (Colin, 20) rather than the second (10).
        colin = live_of(recommended(client, mock_id, auth_a), 5)
        assert colin["assigned_slot"] == "C"
        assert colin["replacement_points"] == 20.0

    def test_another_users_mock_is_404(self, client, auth_b, mock_id):
        assert client.get(f"/mocks/{mock_id}/lineup", headers=auth_b).status_code == 404

    def test_a_mock_that_does_not_exist_is_404(self, client, auth_a):
        assert client.get(f"/mocks/{uuid.uuid4()}/lineup", headers=auth_a).status_code == 404


def simulate(client, mock_id, headers, **body):
    return client.post(f"/mocks/{mock_id}/simulate", headers=headers, json=body)


class TestSimulate:
    """Auto-drafting the other teams (2-team fixture: slots alternate 1, 2)."""

    def test_it_stops_on_my_pick(self, client, auth_a, mock_id):
        """My slot is 1, so pick 1 is mine and nothing should happen yet."""
        body = simulate(client, mock_id, auth_a).json()
        assert body["picks"] == []
        assert body["next_pick_number"] == 1
        assert body["next_is_mine"] is True

    def test_it_drafts_the_opponent_after_my_pick(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        body = simulate(client, mock_id, auth_a).json()

        # Picks 2 and 3 belong to team 2 (round 2 of a snake descends), then
        # pick 4 comes back to me.
        assert [p["pick_number"] for p in body["picks"]] == [2, 3]
        assert {p["team_slot"] for p in body["picks"]} == {2}
        assert body["next_pick_number"] == 4
        assert body["next_is_mine"] is True

    def test_it_never_overwrites_a_pick_i_made(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        simulate(client, mock_id, auth_a)
        simulate(client, mock_id, auth_a)  # twice, deliberately

        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        assert picks[0]["player"]["espn_player_id"] == 1

    def test_a_seed_makes_the_board_reproducible(self, client, auth_a, board, players, league):
        """Same seed, same draft. Without this you cannot replay one board
        against a different strategy and attribute the difference."""
        compute(client, league, auth_a)
        runs = []
        for _ in range(2):
            mid = make_mock(client, board, auth_a, name=f"seeded-{_}").json()["id"]
            draft(client, mid, auth_a, 1, str(players[0].id))
            body = simulate(client, mid, auth_a, seed=7, reach=5).json()
            runs.append([p["player"]["espn_player_id"] for p in body["picks"]])
        assert runs[0] == runs[1]

    def test_reach_one_is_a_pure_value_maximizer(self, client, auth_a, players, mock_id):
        """reach=1 takes the best fit every time, so the pick is predictable.

        I take Paul Guard (PG, 50) at pick 1. Team 2's best available that
        fits an open seat is Cal Center (C, 45) — the top of the board by
        value, since centre is the scarcer slot.
        """
        draft(client, mock_id, auth_a, 1, str(players[0].id))
        body = simulate(client, mock_id, auth_a, reach=1).json()
        assert body["picks"][0]["player"]["espn_player_id"] == 4

    def test_bots_fill_their_lineup_before_their_bench(self, client, auth_a, players, mock_id):
        """Roster need, not just value.

        The fixture has one PG, one C and one UTIL seat per team. Running the
        whole board out, team 2's first three picks must occupy three
        different starting slots rather than stacking one position, which is
        what a pure value-maximizer with no need model would do.
        """
        body = simulate(client, mock_id, auth_a, reach=1, stop_at_my_pick=False).json()
        theirs = [p for p in body["picks"] if p["team_slot"] == 2][:3]
        positions = [p["player"]["positions"][0] for p in theirs]
        assert len(set(positions)) > 1, positions

    def test_running_the_whole_board_out_fills_every_slot(self, client, auth_a, mock_id):
        """stop_at_my_pick=False drafts my picks too. The pool is 7 players
        and the board is 26 slots, so it stops when the pool runs dry rather
        than inventing anybody."""
        body = simulate(client, mock_id, auth_a, stop_at_my_pick=False).json()
        assert len(body["picks"]) == 7
        assert body["board_complete"] is False

        picks = client.get(f"/mocks/{mock_id}/picks", headers=auth_a).json()
        assert sum(1 for p in picks if p["player"]) == 7

    def test_no_player_is_drafted_twice(self, client, auth_a, mock_id):
        body = simulate(client, mock_id, auth_a, stop_at_my_pick=False).json()
        ids = [p["player"]["espn_player_id"] for p in body["picks"]]
        assert len(ids) == len(set(ids))

    def test_an_unvalued_league_is_422(self, client, auth_a, board, players):
        unvalued = make_mock(client, board, auth_a, name="Unvalued").json()["id"]
        r = simulate(client, unvalued, auth_a)
        assert r.status_code == 422
        assert "valuations" in r.json()["detail"]

    def test_a_read_only_share_cannot_simulate(self, client, db, auth_b, user_b, board, mock_id):
        """It writes picks, so board access is not enough.

        403 rather than 404, matching POST /mocks/{id}/picks: B can already
        read this board, so concealing it would only be confusing. The
        404-not-403 rule is for boards the caller cannot see at all.
        """
        from warroom.models import BoardShare, SharePermission

        db.add(
            BoardShare(
                board_id=board.id, shared_with_user_id=user_b.id, permission=SharePermission.READ
            )
        )
        db.commit()

        assert client.get(f"/mocks/{mock_id}/picks", headers=auth_b).status_code == 200
        assert simulate(client, mock_id, auth_b).status_code == 403

    def test_a_mock_that_does_not_exist_is_404(self, client, auth_a):
        assert simulate(client, str(uuid.uuid4()), auth_a).status_code == 404


class TestMarginalValue:
    """Step 2: the board re-read against YOUR roster.

    Fixture league: 2 teams, slots PG:1 / C:1 / UTIL:1, my slot is 1 so I own
    picks 1, 4, 5. Pool is the SPEC 5.3 seven.
    """

    def test_an_empty_roster_leaves_the_order_unchanged(self, client, auth_a, mock_id):
        """The safety property. Turning step 2 on must not touch round one:
        with no seats filled, marginal value IS live value."""
        payload = recommended(client, mock_id, auth_a, "&sort=marginal")
        assert [i["player"]["espn_player_id"] for i in payload["items"]] == (
            SPEC_53_EXPECTED_RANKING
        )
        for item in payload["items"]:
            if item["live"]["value"] > 0:
                assert item["marginal_value"] == item["live"]["value"]

    def test_everyone_improves_an_empty_lineup(self, client, auth_a, mock_id):
        payload = recommended(client, mock_id, auth_a)
        top = payload["items"][0]
        assert top["improves_lineup"] is True
        assert top["marginal_value"] == 25.0  # Cal Center's VOR

    def test_a_filled_seat_demotes_the_players_who_only_fit_it(
        self, client, auth_a, players, mock_id
    ):
        """The point of the whole step.

        I take Cal Center (C, value 25) at pick 1. My C seat is now held by the
        best centre in the pool, so the remaining centres can only reach UTIL
        or my bench — while guards still walk into an empty PG seat. Colin
        Center is worth 0 to the league-wide board and less than that to me.
        """
        draft(client, mock_id, auth_a, 1, str(players[3].id))  # Cal Center -> my C

        payload = recommended(client, mock_id, auth_a)
        colin = next(i for i in payload["items"] if i["player"]["espn_player_id"] == 5)
        assert colin["improves_lineup"] is False
        assert colin["marginal_value"] == 0.0

        paul = next(i for i in payload["items"] if i["player"]["espn_player_id"] == 1)
        assert paul["improves_lineup"] is True
        assert paul["marginal_value"] > 0

    def test_the_default_sort_is_by_marginal_value(self, client, auth_a, players, mock_id):
        """The contract, asserted on the sort key rather than on the ids.

        This pool cannot demonstrate a REORDER: every player below the top has
        a live value of 0 or less, so their marginal clamps to 0 and the
        live-value tie-break reproduces the same sequence. The reordering
        itself is pinned in test_draft.py, where the pool can be built to show
        it. What belongs here is that the endpoint sorts on the right number
        and that the escape hatch works.
        """
        draft(client, mock_id, auth_a, 1, str(players[3].id))  # Cal Center

        marginals = [i["marginal_value"] for i in recommended(client, mock_id, auth_a)["items"]]
        assert marginals == sorted(marginals, reverse=True)

    def test_sort_value_gives_back_the_league_wide_order(self, client, auth_a, players, mock_id):
        draft(client, mock_id, auth_a, 1, str(players[3].id))
        payload = recommended(client, mock_id, auth_a, "&sort=value")
        values = [i["live"]["value"] for i in payload["items"]]
        assert values == sorted(values, reverse=True)

    def test_only_my_picks_shape_it(self, client, auth_a, players, mock_id):
        """Pick 2 is the opponent's. Their roster must not change what a
        player is worth to me — only the league-wide replacement levels."""
        draft(client, mock_id, auth_a, 2, str(players[3].id))  # theirs, not mine

        payload = recommended(client, mock_id, auth_a)
        for item in payload["items"]:
            if item["live"]["value"] > 0:
                assert item["marginal_value"] == item["live"]["value"]

    def test_a_bad_sort_is_422(self, client, auth_a, mock_id):
        r = client.get(f"/mocks/{mock_id}/recommendation?sort=vibes", headers=auth_a)
        assert r.status_code == 422

    def test_filling_a_hole_outranks_a_better_player_you_cannot_start(
        self, client, auth_a, players, mock_id
    ):
        """The late-draft tie-break, which is when step 2 earns its keep.

        I fill C and UTIL, leaving only PG. Everyone left grades at or under
        replacement, so marginal_value is 0 across the board — and on that tie
        a guard who fills my last seat must come before a centre who cannot,
        even though the centre scores higher on the league-wide board.
        """
        draft(client, mock_id, auth_a, 1, str(players[3].id))  # Cal Center -> C
        draft(client, mock_id, auth_a, 4, str(players[6].id))  # Sam Forward -> UTIL

        items = recommended(client, mock_id, auth_a)["items"]
        guards = [i for i in items if "PG" in i["player"]["positions"]]
        centres = [i for i in items if i["player"]["positions"] == ["C"]]

        assert all(i["fills_open_seat"] for i in guards)
        assert not any(i["fills_open_seat"] for i in centres)

        order = [i["player"]["espn_player_id"] for i in items]
        best_guard = min(order.index(g["player"]["espn_player_id"]) for g in guards)
        best_centre = min(order.index(c["player"]["espn_player_id"]) for c in centres)
        assert best_guard < best_centre

    def test_fills_open_seat_is_false_once_the_lineup_is_full(
        self, client, auth_a, players, mock_id
    ):
        draft(client, mock_id, auth_a, 1, str(players[0].id))  # PG
        draft(client, mock_id, auth_a, 4, str(players[3].id))  # C
        draft(client, mock_id, auth_a, 5, str(players[6].id))  # UTIL

        items = recommended(client, mock_id, auth_a)["items"]
        assert not any(i["fills_open_seat"] for i in items)


class TestVonaEndpoint:
    """Step 3 through the API. 2-team snake, my slot 1, so I own 1, 4, 5, 8…"""

    def test_it_reports_how_long_until_my_next_pick(self, client, auth_a, mock_id):
        """Every survival number is conditioned on this, so it travels with
        them. Picks 2 and 3 are the opponent's before I pick again at 4."""
        assert recommended(client, mock_id, auth_a)["picks_until_next"] == 2

    def test_at_the_turn_nobody_picks_in_between(self, client, db, auth_a, board, players, league):
        """Slot 2 in a 2-team snake picks at 2 and 3 back to back, so from
        pick 2 nothing can be taken from them — survival is 1.0 for everyone.
        """
        compute(client, league, auth_a)
        mine = make_mock(client, board, auth_a, name="turn", my_draft_slot=2).json()["id"]
        draft(client, mine, auth_a, 1, str(players[0].id))  # clear the opponent's pick

        payload = recommended(client, mine, auth_a)
        assert payload["picks_until_next"] == 0
        assert all(i["survival"] == 1.0 for i in payload["items"])

    def test_the_best_players_are_least_likely_to_survive(self, client, auth_a, mock_id):
        payload = recommended(client, mock_id, auth_a, "&sort=value")
        survivals = [i["survival"] for i in payload["items"]]
        assert survivals == sorted(survivals)
        assert survivals[0] < 1.0

    def test_score_is_marginal_plus_what_you_expect_next(self, client, auth_a, mock_id):
        for item in recommended(client, mock_id, auth_a)["items"]:
            assert item["score"] == pytest.approx(item["marginal_value"] + item["expected_next"])

    def test_the_default_sort_is_by_score(self, client, auth_a, mock_id):
        scores = [i["score"] for i in recommended(client, mock_id, auth_a)["items"]]
        assert scores == sorted(scores, reverse=True)

    def test_waiting_longer_lowers_what_you_can_expect_next(
        self, client, auth_a, board, players, league
    ):
        """The mechanism, asserted where it is actually visible.

        Score rarely REORDERS this board, because E[best survivor] barely
        moves when you remove one player from a pool of hundreds — so the
        constant cancels out of the comparison. What it does do is price the
        wait, and that shows up in the number rather than the sequence: from
        the turn, where nothing can be taken, your fallback is the whole board
        at full strength; from mid-round it is whatever survives six picks.
        """
        compute(client, league, auth_a)
        # Two empty boards over the identical pool, differing ONLY in where I
        # sit in the order. Slot 2 picks 2 and 3 back to back at the turn;
        # slot 1 picks 1 and then waits two. Drafting into either would change
        # the pool and make the comparison meaningless.
        at_turn = make_mock(client, board, auth_a, name="turn", my_draft_slot=2).json()["id"]
        mid = make_mock(client, board, auth_a, name="mid", my_draft_slot=1).json()["id"]

        turn_page = recommended(client, at_turn, auth_a)
        mid_page = recommended(client, mid, auth_a)
        assert turn_page["picks_until_next"] == 0
        assert mid_page["picks_until_next"] > 0

        # Nothing can be taken at the turn, so the fallback is worth strictly
        # more than it is when opponents pick in between.
        assert turn_page["items"][0]["expected_next"] > mid_page["items"][0]["expected_next"]

    def test_all_three_sorts_are_accepted(self, client, auth_a, mock_id):
        for sort in ("score", "marginal", "value"):
            r = client.get(f"/mocks/{mock_id}/recommendation?sort={sort}", headers=auth_a)
            assert r.status_code == 200, sort

    def test_expected_next_never_counts_the_player_you_took(self, client, auth_a, mock_id):
        """Taking a player removes them from your own fallback — otherwise
        every candidate would look like a free option."""
        items = recommended(client, mock_id, auth_a, "&sort=value")["items"]
        top = items[0]
        # The best player cannot be his own fallback, so the expectation has
        # to come from someone strictly worse.
        assert top["expected_next"] < top["marginal_value"]

    def test_a_board_of_tied_scores_falls_through_to_your_roster(
        self, client, auth_a, players, mock_id
    ):
        """Bench rounds tie every score at 0.0, so the fallback decides the
        board. It used to be league-wide live value, which knows nothing about
        your team — in a rebound-heavy league that recommended another centre
        to a manager who already had five. It is now lineup_delta: how far
        each player sits below the starter they would displace.

        The original point of this test still stands underneath: whatever the
        fallback is, it must not be espn_player_id, or the board sorts itself
        by seniority."""
        # The three BEST players, so nobody left can improve the lineup —
        # otherwise the board is not tied at all. Taking Sam Forward (25) here
        # instead used to tie because the seating maths dropped a negative
        # starter and reported their seat as empty; with the seat correctly
        # occupied, Peter Guard (40) would genuinely displace him.
        draft(client, mock_id, auth_a, 1, str(players[0].id))  # Paul Guard, 50
        draft(client, mock_id, auth_a, 4, str(players[3].id))  # Cal Center, 45
        draft(client, mock_id, auth_a, 5, str(players[1].id))  # Peter Guard, 40

        items = recommended(client, mock_id, auth_a)["items"]
        assert all(i["score"] == 0.0 for i in items)

        deltas = [i["lineup_delta"] for i in items]
        assert deltas == sorted(deltas, reverse=True)

        ids = [i["player"]["espn_player_id"] for i in items]
        assert ids != sorted(ids), "fell through to seniority"


class TestConfidentSort:
    """Ranking by the score you would get in a poor outcome for the parts of
    the projection this app inferred rather than measured."""

    def test_it_is_accepted_and_orders_by_the_discounted_score(self, client, auth_a, mock_id):
        items = recommended(client, mock_id, auth_a, "&sort=confident")["items"]
        discounted = [i["score"] - i["valuation"]["model_sd"] for i in items]
        assert discounted == sorted(discounted, reverse=True)

    def test_the_band_rides_on_the_valuation(self, client, auth_a, mock_id):
        """No new field on the recommendation: it already carries the cached
        valuation, and the band belongs to that row."""
        top = recommended(client, mock_id, auth_a)["items"][0]
        assert top["valuation"]["value_sd"] > 0
        assert "model_sd" in top["valuation"]

    def test_a_pool_with_nothing_estimated_orders_the_same_as_score(self, client, auth_a, mock_id):
        """The fixture league scores only `pts`, which ESPN projects directly,
        so there is no model component to discount and the two agree."""
        assert recommended_ids(client, mock_id, auth_a, "&sort=confident") == (
            recommended_ids(client, mock_id, auth_a, "&sort=score")
        )


class TestSimulationBasis:
    def test_the_bots_do_not_use_the_marginal_basis(self, client, auth_a, league, board, players):
        """Deliberate, and measured: the marginal basis re-solves a league-wide
        assignment per pick, which takes a full draft from under a second to
        forty. The bots also stand in for managers drafting off ESPN's ranks,
        so the cheaper basis is the more realistic one for them.

        The assertion that matters is that simulating stays fast enough to be
        usable while the league sits on `marginal`.
        """
        client.post(
            f"/leagues/{league.id}/valuations/compute",
            headers=auth_a,
            json={"replacement_basis": "marginal"},
        )
        # Slot 2, so pick 1 is the opponent's and the simulator has work to
        # do. Not stop_at_my_pick=False: this fixture pool is seven players
        # and running the board out would leave nothing to recommend.
        mock_id = make_mock(client, board, auth_a, name="sim basis", my_draft_slot=2).json()["id"]

        response = simulate(client, mock_id, auth_a)
        assert response.status_code == 200
        assert response.json()["picks"]

        # And the board the human reads DOES honour the league's basis.
        items = client.get(f"/mocks/{mock_id}/recommendation", headers=auth_a).json()["items"]
        assert items
