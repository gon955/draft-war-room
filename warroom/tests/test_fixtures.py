"""Proof that the fixtures themselves work (SPEC 6).

Every other integration test trusts conftest to give it a clean database, a
faked ESPN and two identities. That trust is worth one file of assertions,
because the ways it breaks are all silent: a suite that leaks rows between
tests still passes test-by-test, and a suite that quietly reaches the network
still passes until ESPN has an outage.

The isolation pair below is the important one. It is written as two tests on
purpose — a single test cannot prove that teardown rolled anything back.
"""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from warroom.models import Board, League, Player, SharePermission, User
from warroom.tests.conftest import SEASON, TEST_PASSWORD

LEAKED_EMAIL = "leak-detector@example.com"


class TestTransactionalIsolation:
    """Blocker: routes call db.commit(), which must not escape the test.

    auth.py commits on register. What keeps that commit inside the test is the
    db fixture binding its Session to a Connection with an already-open
    transaction; bind it to the engine instead and the commit is real and
    permanent. Confirmed by making that exact change and watching the two
    test_b_* cases below fail.
    """

    def test_a_write_and_commit_a_row(self, db):
        db.add(User(email=LEAKED_EMAIL, password_hash="x"))
        db.commit()
        assert db.scalar(select(func.count()).select_from(User).where(User.email == LEAKED_EMAIL))

    def test_b_the_committed_row_is_gone(self, db):
        # Depends on running after the test above (alphabetical within the
        # class). If the savepoint mode regresses, this is the test that fails.
        leaked = db.scalar(select(func.count()).select_from(User).where(User.email == LEAKED_EMAIL))
        assert leaked == 0, (
            "a committed row survived teardown — the db fixture is no longer "
            "rolling back. Check join_transaction_mode='create_savepoint'."
        )

    def test_a_register_through_the_api_commits(self, client):
        response = client.post(
            "/auth/register", json={"email": "api-leak@example.com", "password": TEST_PASSWORD}
        )
        assert response.status_code == 201

    def test_b_the_registered_user_is_gone(self, client):
        # The same proof one level up: through the real route, real commit.
        response = client.post(
            "/auth/register", json={"email": "api-leak@example.com", "password": TEST_PASSWORD}
        )
        assert response.status_code == 201, (
            "the email was still taken, so the previous test's commit leaked out of its transaction"
        )


class TestClientWiring:
    def test_health_needs_no_auth(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_the_route_session_is_the_fixture_session(self, client, db, user_a, auth_a):
        # The override must hand the route the SAME session, or rows created by
        # fixtures would be invisible to the app (they are never committed to
        # anything another connection could see).
        response = client.get("/auth/me", headers=auth_a)
        assert response.status_code == 200
        assert response.json()["email"] == user_a.email

    def test_the_session_survives_a_request(self, client, db, user_a, auth_a):
        # A get_db override that closed the session would detach user_a here.
        client.get("/auth/me", headers=auth_a)
        db.refresh(user_a)
        assert user_a.email == "a@example.com"

    def test_espn_is_faked(self, fake_data_source):
        settings = fake_data_source.get_league_settings(1, SEASON)
        assert settings.scoring_format == "points"
        assert len(fake_data_source.get_player_pool(1, SEASON)) == 7


class TestIdentities:
    def test_a_and_b_are_distinct(self, user_a, user_b):
        assert user_a.id != user_b.id

    def test_tokens_resolve_to_their_own_user(self, client, user_a, user_b, auth_a, auth_b):
        assert client.get("/auth/me", headers=auth_a).json()["email"] == user_a.email
        assert client.get("/auth/me", headers=auth_b).json()["email"] == user_b.email

    def test_the_shared_hash_verifies(self, client, user_a):
        # The pre-hashed password is a performance shortcut; it still has to be
        # a real argon2 hash of TEST_PASSWORD or every login test is a lie.
        response = client.post(
            "/auth/login", json={"email": user_a.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_no_token_is_401(self, client):
        assert client.get("/auth/me").status_code == 401


class TestOwnedData:
    def test_league_belongs_to_a(self, league, user_a):
        assert league.user_id == user_a.id

    def test_board_belongs_to_a_and_its_league(self, board, user_a, league):
        assert (board.user_id, board.league_id) == (user_a.id, league.id)

    def test_the_pool_is_the_spec_5_3_example(self, players):
        assert len(players) == 7
        assert {p.espn_player_id for p in players} == set(range(1, 8))
        assert all(p.season == SEASON for p in players)

    def test_players_are_visible_to_the_session(self, db, players):
        assert db.scalar(select(func.count()).select_from(Player)) == 7


class TestShareFactory:
    """Blocker: unique(board_id, shared_with_user_id) forbids read AND edit."""

    def test_read_share_is_created(self, read_share, board, user_b):
        assert read_share.permission is SharePermission.READ
        assert (read_share.board_id, read_share.shared_with_user_id) == (board.id, user_b.id)

    def test_edit_share_is_created(self, edit_share):
        assert edit_share.permission is SharePermission.EDIT

    def test_one_user_cannot_hold_two_shares_on_one_board(
        self, db, share_board_with, board, user_b
    ):
        # The reason share_board_with is a factory instead of two fixtures.
        share_board_with(board, user_b, SharePermission.READ)
        with pytest.raises(IntegrityError):
            share_board_with(board, user_b, SharePermission.EDIT)
        db.rollback()

    def test_permission_is_stored_as_its_value_not_its_name(self, db, read_share):
        # SPEC 4 authorizes on permission='edit'. values_callable in models.py
        # is what keeps this from being 'READ' in the database.
        #
        # The ::text cast is required: selecting the column through SQLAlchemy
        # runs the Enum type's result processor, which would hand back
        # SharePermission.READ and prove nothing about the stored label.
        stored = db.execute(
            text("SELECT permission::text FROM board_shares WHERE id = :id"),
            {"id": read_share.id},
        ).scalar_one()
        assert stored == "read"


class TestSchemaIsReal:
    """The fixtures build from Base.metadata; make sure that is the real thing."""

    def test_cascade_delete_reaches_the_database(self, db, board, user_a):
        # ON DELETE CASCADE, exercised rather than merely asserted in metadata.
        board_id = board.id
        db.delete(user_a)
        db.commit()

        # expire_all is required, not decoration: the db fixture sets
        # expire_on_commit=False (so fixture-held objects survive a route's
        # commit), which means db.get would answer from the identity map and
        # hand back the deleted Board. Ask the database instead.
        db.expire_all()
        remaining = db.scalar(select(func.count()).select_from(Board).where(Board.id == board_id))
        assert remaining == 0

    def test_restrict_blocks_deleting_a_referenced_player(self, db, board, players):
        from warroom.models import Ranking

        db.add(Ranking(board_id=board.id, player_id=players[0].id))
        db.commit()

        db.delete(players[0])
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_uuid_pks_are_uuids(self, league):
        assert isinstance(league.id, uuid.UUID)

    def test_jsonb_round_trips(self, db, league):
        db.refresh(league)
        assert league.roster_slots == {"PG": 1, "C": 1, "UTIL": 1}
        assert league.point_weights == {"pts": 1.0}

    def test_league_is_season_scoped(self, db, league, user_a):
        # unique(user_id, espn_league_id, season): the same league next season
        # is a different row, not a conflict.
        db.add(
            League(
                user_id=user_a.id,
                espn_league_id=league.espn_league_id,
                season=league.season + 1,
                name="Next Season",
                scoring_format=league.scoring_format,
                num_teams=2,
                roster_size=13,
                roster_slots={},
                point_weights={},
            )
        )
        db.commit()
        assert db.scalar(select(func.count()).select_from(League)) == 2


class TestAuditTimestamps:
    """Blocker: now() is transaction-start time, so audit columns froze.

    Every test runs in one transaction. With server_default=now() (Postgres
    transaction_timestamp) every row in a test shared a created_at to the
    microsecond and updated_at could never advance — so no test could assert
    that a PATCH actually touched a row. models.py uses clock_timestamp().
    """

    def test_updated_at_advances_on_update(self, db, user_a):
        created = user_a.created_at

        user_a.email = "a-renamed@example.com"
        db.commit()
        db.refresh(user_a)

        assert user_a.updated_at > created, (
            "updated_at did not advance inside a single transaction — the audit "
            "columns are back on now() instead of clock_timestamp()"
        )

    def test_rows_written_in_one_transaction_differ(self, db, user_a, user_b):
        # Under now() these were identical, which made "which was created first"
        # unanswerable for anything a single request writes in a batch.
        assert user_a.created_at != user_b.created_at
