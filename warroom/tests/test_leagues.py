"""League CRUD and owner-only access, over a faked data source (SPEC 6).

The authorization helpers themselves are pinned in test_authz.py; what this file
proves is that the five routes actually reach for them, and that creating a
league is one atomic act — settings, point weights and the whole player pool
land together or not at all.

Two things get their own tests because they are invisible when they break:
the ESPN cookie must reach the database encrypted and never come back out, and
a refused sync must leave no half-written league behind.
"""

import uuid

import pytest
from sqlalchemy import func, select

from warroom.crypto import decrypt
from warroom.models import Board, League, Player, Ranking
from warroom.tests.conftest import ESPN_LEAGUE_ID, SEASON
from warroom.valuation.data_source import FakePlayerDataSource

OTHER_ESPN_ID = 27311
COOKIE = "AEBxK%2FvN0pQ3rLm8" + "Xy7" * 40


def create_payload(espn_league_id=OTHER_ESPN_ID, season=SEASON, name="Another League", **extra):
    return {"espn_league_id": espn_league_id, "season": season, "name": name, **extra}


def use_empty_pool(client, league_settings):
    """Point the app at a source that returns no players."""
    from warroom.deps import get_data_source

    client.app.dependency_overrides[get_data_source] = lambda: FakePlayerDataSource(
        settings=league_settings, players=[]
    )


class TestCreate:
    def test_create_returns_the_league_with_espn_settings_filled_in(
        self, client, auth_a, league_settings
    ):
        r = client.post("/leagues", headers=auth_a, json=create_payload())

        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "Another League"
        assert body["espn_league_id"] == OTHER_ESPN_ID
        # None of these were in the request: they came from the data source.
        assert body["scoring_format"] == "points"
        assert body["num_teams"] == league_settings.num_teams
        assert body["roster_slots"] == league_settings.roster_slots
        assert body["point_weights"] == league_settings.point_weights
        assert body["roster_size"] == league_settings.roster_size

    def test_the_caller_is_the_owner(self, client, auth_a, user_a):
        body = client.post("/leagues", headers=auth_a, json=create_payload()).json()

        assert body["user_id"] == str(user_a.id)

    def test_create_also_writes_the_player_pool(self, client, db, auth_a, player_pool):
        assert db.scalar(select(func.count()).select_from(Player)) == 0

        client.post("/leagues", headers=auth_a, json=create_payload())

        assert db.scalar(select(func.count()).select_from(Player)) == len(player_pool)

    def test_unauthenticated_is_401(self, client):
        assert client.post("/leagues", json=create_payload()).status_code == 401

    def test_the_same_league_twice_is_409(self, client, auth_a):
        assert client.post("/leagues", headers=auth_a, json=create_payload()).status_code == 201
        assert client.post("/leagues", headers=auth_a, json=create_payload()).status_code == 409

    def test_the_same_league_in_another_season_is_allowed(self, client, auth_a):
        """unique(user_id, espn_league_id, season) is season-scoped on purpose —
        a league's roster shape really does change year to year."""
        assert client.post("/leagues", headers=auth_a, json=create_payload()).status_code == 201
        assert (
            client.post(
                "/leagues", headers=auth_a, json=create_payload(season=SEASON + 1)
            ).status_code
            == 201
        )

    def test_two_users_can_connect_the_same_espn_league(self, client, auth_a, auth_b):
        """The constraint is per user, so B joining the same real-world league
        is not a conflict with A's connection to it."""
        assert client.post("/leagues", headers=auth_a, json=create_payload()).status_code == 201
        assert client.post("/leagues", headers=auth_b, json=create_payload()).status_code == 201

    @pytest.mark.parametrize(
        ("payload", "why"),
        [
            (create_payload(espn_league_id=0), "espn_league_id must be positive"),
            (create_payload(season=1999), "season below the floor"),
            (create_payload(name=""), "empty name"),
        ],
    )
    def test_bad_input_is_422(self, client, auth_a, payload, why):
        assert client.post("/leagues", headers=auth_a, json=payload).status_code == 422, why


class TestTheEspnCookie:
    def test_it_reaches_the_database_encrypted(self, client, db, auth_a, fernet_key):
        client.post("/leagues", headers=auth_a, json=create_payload(espn_s2=COOKIE))

        stored = db.scalar(
            select(League.espn_s2_encrypted).where(League.espn_league_id == OTHER_ESPN_ID)
        )
        assert stored is not None
        assert COOKIE not in stored
        assert decrypt(stored) == COOKIE

    def test_it_never_comes_back_out(self, client, auth_a, fernet_key):
        """SPEC 2.3: no endpoint returns the cookie. LeagueOut omits the column,
        and this fails the day someone 'completes' that schema."""
        created = client.post(
            "/leagues", headers=auth_a, json=create_payload(espn_s2=COOKIE)
        ).json()
        fetched = client.get(f"/leagues/{created['id']}", headers=auth_a).json()
        listed = client.get("/leagues", headers=auth_a).json()

        for body in (created, fetched, *listed):
            assert "espn_s2" not in str(body)
            assert COOKIE not in str(body)

    def test_a_public_league_stores_no_cookie(self, client, db, auth_a):
        """No espn_s2 in the payload means NULL, not an encrypted empty string."""
        client.post("/leagues", headers=auth_a, json=create_payload())

        assert (
            db.scalar(
                select(League.espn_s2_encrypted).where(League.espn_league_id == OTHER_ESPN_ID)
            )
            is None
        )


class TestList:
    def test_listing_shows_only_the_callers_leagues(self, client, auth_a, auth_b, league):
        assert len(client.get("/leagues", headers=auth_a).json()) == 1
        assert client.get("/leagues", headers=auth_b).json() == []

    def test_a_share_does_not_put_the_league_in_bs_list(self, client, auth_b, league, read_share):
        """Deliberately narrower than require_league_access: B can reach this
        league through the shared board, but it is not B's league to list."""
        assert client.get("/leagues", headers=auth_b).json() == []

    def test_unauthenticated_is_401(self, client):
        assert client.get("/leagues").status_code == 401


class TestGet:
    def test_the_owner_gets_it(self, client, auth_a, league):
        r = client.get(f"/leagues/{league.id}", headers=auth_a)

        assert r.status_code == 200
        assert r.json()["espn_league_id"] == ESPN_LEAGUE_ID

    def test_a_stranger_gets_404_not_403(self, client, auth_b, league):
        assert client.get(f"/leagues/{league.id}", headers=auth_b).status_code == 404

    def test_a_share_holder_is_still_not_the_owner(self, client, auth_b, league, read_share):
        """GET /leagues/{id} is owner-only per SPEC 4; the share reaches the
        board, not the league's own settings."""
        assert client.get(f"/leagues/{league.id}", headers=auth_b).status_code == 403

    def test_a_league_that_never_existed_is_the_same_404(self, client, auth_a):
        assert client.get(f"/leagues/{uuid.uuid4()}", headers=auth_a).status_code == 404

    def test_unauthenticated_is_401(self, client, league):
        assert client.get(f"/leagues/{league.id}").status_code == 401


class TestDelete:
    def test_the_owner_can_delete(self, client, db, auth_a, league):
        assert client.delete(f"/leagues/{league.id}", headers=auth_a).status_code == 204
        assert db.get(League, league.id) is None

    def test_a_stranger_cannot(self, client, auth_b, league):
        assert client.delete(f"/leagues/{league.id}", headers=auth_b).status_code == 404

    def test_an_edit_share_holder_cannot(self, client, auth_b, league, edit_share):
        assert client.delete(f"/leagues/{league.id}", headers=auth_b).status_code == 403

    def test_deleting_takes_the_boards_and_rankings_with_it(
        self, client, db, auth_a, league, board, players
    ):
        """The cascade is documented at the call site because it is invisible
        there: this is the endpoint that can delete another user's shared board."""
        db.add(Ranking(board_id=board.id, player_id=players[0].id))
        db.commit()

        client.delete(f"/leagues/{league.id}", headers=auth_a)

        assert db.scalar(select(func.count()).select_from(Board)) == 0
        assert db.scalar(select(func.count()).select_from(Ranking)) == 0

    def test_the_shared_player_pool_survives(self, client, db, auth_a, league, players):
        """players is reference data owned by no user (SPEC 0.2). Deleting the
        league that happened to fetch it must not delete it."""
        client.delete(f"/leagues/{league.id}", headers=auth_a)

        assert db.scalar(select(func.count()).select_from(Player)) == len(players)


class TestUpstreamFailure:
    def test_an_empty_pool_is_a_clean_4xx(self, client, auth_a, league_settings):
        """Not a 500 from a bare exception: ESPN publishes nothing for a season
        until close to opening night, and that is a normal thing to be told."""
        use_empty_pool(client, league_settings)

        assert client.post("/leagues", headers=auth_a, json=create_payload()).status_code == 422

    def test_a_refused_create_leaves_no_league_behind(self, client, db, auth_a, league_settings):
        """sync_league_settings runs before the pool fetch, so the row exists in
        the session by the time this fails. Without the rollback it would commit
        a league with no players."""
        use_empty_pool(client, league_settings)

        client.post("/leagues", headers=auth_a, json=create_payload())

        assert db.scalar(select(func.count()).select_from(League)) == 0
        assert db.scalar(select(func.count()).select_from(Player)) == 0
