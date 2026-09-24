"""Simulated teams drafting on ESPN's valuation instead of this app's.

The bots normally price players with the engine's own projections. `espn` swaps
that one input for ESPN's published projected total — the number ESPN itself
computes for each player under this league's scoring, which arrives in the same
payload as the raw stats and is stored on players.espn_projected_points.

THE POINT IS THE ROOM, NOT THE MATHS. Nine bots using your own valuations can
never show you the player your league will let slide, because they agree with
you by construction. Bots on ESPN's numbers reach for who ESPN likes, which is
what your leaguemates are actually reading.

WHAT MUST NOT CHANGE is everything downstream. Live replacement levels, the
marginal basis and roster fit are all derived FROM the strength mapping, so
swapping it changes whose opinion the scarcity maths is applied to and nothing
about the maths. TestScarcityStillApplies is the executable version of that
sentence, and it is the test to keep if any of these go.
"""

import uuid

import pytest
from sqlalchemy import select

from warroom.models import Board, League, MockDraft, MockPick, Player, ScoringFormat, User
from warroom.services.simulation import BotValuation, NoEspnProjections, simulate
from warroom.tests.conftest import SEASON

# Engine order and ESPN order deliberately disagree end to end: the engine's
# best (by projected points) is ESPN's worst. Nothing can score the same in
# both, so any pick attributes itself to exactly one source.
#
# Both favourites win their own basis by a wide margin, on purpose. Values here
# are value OVER REPLACEMENT, not raw points, so a pool with evenly spaced
# points produces ties — three players tied on 10 in the first draft of this
# fixture, and the pick fell to the espn_player_id tie-break rather than to
# either opinion. Widening the top of each column makes every assertion below
# attributable.
#
#   id  pos   engine pts   espn pts     top by engine VOR   top by ESPN VOR
#    1  PG      200            10             110                   0
#    2  PG       90            20               0                  10
#    3  C        80            30              10                   0
#    4  C        70            40               0                  10
#    5  SF       60           200             -30                 160
POOL = [
    (1, "Engine Favourite", ("PG",), 200.0, 10.0),
    (2, "Second Guard", ("PG",), 90.0, 20.0),
    (3, "Third Centre", ("C",), 80.0, 30.0),
    (4, "Fourth Centre", ("C",), 70.0, 40.0),
    (5, "Espn Favourite", ("SF",), 60.0, 200.0),
]


@pytest.fixture
def espn_players(db) -> list[Player]:
    rows = [
        Player(
            espn_player_id=pid,
            season=SEASON,
            name=name,
            pro_team="BOS",
            positions=list(positions),
            projections={"pts": engine_pts},
            espn_projected_points=espn_pts,
        )
        for pid, name, positions, engine_pts, espn_pts in POOL
    ]
    db.add_all(rows)
    db.commit()
    return rows


@pytest.fixture
def espn_league(db, user_a: User) -> League:
    """One starting seat per slot, so scarcity bites within a couple of picks."""
    row = League(
        user_id=user_a.id,
        espn_league_id=987654,
        season=SEASON,
        name="ESPN mode league",
        scoring_format=ScoringFormat.POINTS,
        num_teams=2,
        roster_size=3,
        roster_slots={"PG": 1, "C": 1, "UTIL": 1},
        point_weights={"pts": 1.0},
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def espn_mock(db, user_a: User, espn_league: League, espn_players) -> MockDraft:
    board = Board(user_id=user_a.id, league_id=espn_league.id, name="b")
    db.add(board)
    db.commit()

    mock = MockDraft(board_id=board.id, name="m", my_draft_slot=1)
    db.add(mock)
    db.commit()

    # A 2-team snake over 3 rounds: 1,2 | 2,1 | 1,2. my_draft_slot is 1, and
    # every test below passes stop_at_my_pick=False so the bots run it all.
    picks = []
    for rnd in range(1, 4):
        for position in range(1, 3):
            slot = position if rnd % 2 else 3 - position
            picks.append(
                MockPick(
                    mock_draft_id=mock.id,
                    pick_number=(rnd - 1) * 2 + position,
                    round=rnd,
                    team_slot=slot,
                    is_mine=slot == 1,
                )
            )
    db.add_all(picks)
    db.commit()
    return mock


def drafted_ids(db, mock: MockDraft) -> list[int]:
    rows = db.scalars(
        select(MockPick).where(MockPick.mock_draft_id == mock.id).order_by(MockPick.pick_number)
    )
    by_id = {p.id: p for p in db.scalars(select(Player).where(Player.season == SEASON))}
    return [by_id[p.player_id].espn_player_id for p in rows if p.player_id]


class TestTheSourceActuallySwaps:
    def test_espn_mode_takes_espns_favourite_first(self, db, espn_mock):
        """ESPN rates player 5 highest and the engine rates them worst, so the
        first pick alone identifies which opinion the room is using."""
        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN)

        assert drafted_ids(db, espn_mock)[0] == 5

    def test_engine_mode_is_unchanged(self, db, espn_mock, client, auth_a, espn_league):
        """The default must still be this app's numbers. Engine mode needs the
        valuations cache, so compute it the way a caller would."""
        client.post(f"/leagues/{espn_league.id}/valuations/compute", headers=auth_a)

        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ENGINE)

        assert drafted_ids(db, espn_mock)[0] == 1

    def test_the_two_modes_produce_different_boards(
        self, db, espn_mock, client, auth_a, espn_league
    ):
        client.post(f"/leagues/{espn_league.id}/valuations/compute", headers=auth_a)
        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN)
        espn_board = drafted_ids(db, espn_mock)

        for pick in db.scalars(select(MockPick).where(MockPick.mock_draft_id == espn_mock.id)):
            pick.player_id = None
        db.commit()

        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ENGINE)

        assert espn_board != drafted_ids(db, espn_mock)


class TestScarcityStillApplies:
    """The requirement: swapping the numbers must not switch off the engine.

    With one PG seat, one C seat and one UTIL seat across two teams, every slot
    saturates after two players. A bot that had stopped re-pricing would simply
    walk ESPN's list; one that is still computing live replacement levels off
    ESPN's numbers will leave a player ESPN rates higher on the board once the
    slot they fill is full.
    """

    def test_the_draft_is_not_espns_order(self, db, espn_mock):
        """Straight down ESPN's list would be [5, 4, 3, 2, 1]. Anything else is
        the replacement levels and roster fit still doing their work."""
        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN)

        assert drafted_ids(db, espn_mock) != [5, 4, 3, 2, 1]

    def test_every_team_fills_its_lineup_before_taking_depth(self, db, espn_mock):
        """Roster fit is the other half of it. Each team has a PG seat, a C seat
        and a UTIL seat, so neither should end with two of one position and a
        hole — which is exactly what ESPN's ordering would hand them."""
        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN)

        rows = list(
            db.scalars(
                select(MockPick)
                .where(MockPick.mock_draft_id == espn_mock.id)
                .order_by(MockPick.pick_number)
            )
        )
        by_id = {p.id: p for p in db.scalars(select(Player).where(Player.season == SEASON))}
        for slot in (1, 2):
            positions = [
                tuple(by_id[p.player_id].positions)
                for p in rows
                if p.team_slot == slot and p.player_id
            ]
            assert len(positions) == len(set(positions)), f"team {slot} doubled up: {positions}"


class TestEspnModeStandsAlone:
    def test_it_does_not_need_this_apps_valuations(self, db, espn_mock):
        """Drafting on ESPN's opinion should not require ours to have been
        computed. Nothing in this test runs /valuations/compute."""
        assert db.scalar(select(Player).where(Player.season == SEASON)) is not None

        result = simulate(
            db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN
        )

        assert len(result.picks) == 5

    def test_a_pool_with_no_espn_numbers_is_a_clear_error(self, db, espn_mock):
        """The common case is a pool synced before the column existed. The
        message has to name /sync, not /valuations/compute."""
        for player in db.scalars(select(Player).where(Player.season == SEASON)):
            player.espn_projected_points = None
        db.commit()

        with pytest.raises(NoEspnProjections, match="sync"):
            simulate(db, espn_mock, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN)

    def test_a_player_espn_did_not_project_is_kept_at_zero(self, db, espn_mock):
        """Not dropped: they are draftable, they belong at the bottom, and
        removing them would shrink the pool that sets every replacement level."""
        unprojected = db.scalar(select(Player).where(Player.espn_player_id == 5))
        unprojected.espn_projected_points = None
        db.commit()

        simulate(db, espn_mock, reach=1, stop_at_my_pick=False, bot_valuation=BotValuation.ESPN)
        board = drafted_ids(db, espn_mock)

        assert 5 in board
        assert board[0] != 5


class TestTheApiSurface:
    def test_the_default_is_the_engine(self, client, auth_a, espn_league, espn_mock):
        client.post(f"/leagues/{espn_league.id}/valuations/compute", headers=auth_a)

        # reach=1 deliberately: the default of 3 samples from a shortlist, so
        # without it this asserts on the dice rather than on the valuation.
        r = client.post(
            f"/mocks/{espn_mock.id}/simulate",
            headers=auth_a,
            json={"stop_at_my_pick": False, "reach": 1},
        )

        assert r.status_code == 200
        assert r.json()["picks"][0]["player"]["espn_player_id"] == 1

    def test_espn_can_be_asked_for_over_http(self, client, auth_a, espn_mock):
        r = client.post(
            f"/mocks/{espn_mock.id}/simulate",
            headers=auth_a,
            json={"stop_at_my_pick": False, "reach": 1, "bot_valuation": "espn"},
        )

        assert r.status_code == 200
        assert r.json()["picks"][0]["player"]["espn_player_id"] == 5

    def test_an_unknown_source_is_rejected(self, client, auth_a, espn_mock):
        r = client.post(
            f"/mocks/{espn_mock.id}/simulate",
            headers=auth_a,
            json={"bot_valuation": "yahoo"},
        )

        assert r.status_code == 422

    def test_a_missing_espn_pool_is_422_not_500(self, client, db, auth_a, espn_mock):
        for player in db.scalars(select(Player).where(Player.season == SEASON)):
            player.espn_projected_points = None
        db.commit()

        r = client.post(
            f"/mocks/{espn_mock.id}/simulate",
            headers=auth_a,
            json={"stop_at_my_pick": False, "bot_valuation": "espn"},
        )

        assert r.status_code == 422
        assert "sync" in r.json()["detail"]

    def test_a_read_only_share_still_cannot_simulate(self, client, auth_b, espn_mock):
        """bot_valuation is a knob on a WRITE. It must not become a way in."""
        r = client.post(
            f"/mocks/{espn_mock.id}/simulate", headers=auth_b, json={"bot_valuation": "espn"}
        )

        assert r.status_code in (403, 404)
        assert uuid.UUID(str(espn_mock.id))
