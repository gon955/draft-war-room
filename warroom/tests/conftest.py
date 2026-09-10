"""Fixtures for the integration suite (SPEC 6).

The whole suite runs against a real Postgres, never SQLite: the schema is
JSONB-heavy and a passing SQLite suite would prove nothing about what CI and
production actually run.

Isolation model — one transaction per test, rolled back:

    connection.begin()                  outer transaction, never committed
      Session(join_transaction_mode="create_savepoint")
        route calls db.commit()         releases a SAVEPOINT, not the outer tx
      trans.rollback()                  undoes the whole test

`bind=connection` is the load-bearing part: bind the Session to the engine
instead and every `db.commit()` in a route goes straight to the database, the
teardown rollback has nothing to undo, and rows leak between tests — a suite
that passes test-by-test and fails in combination. Verified by temporarily
breaking it: tests/test_fixtures.py::TestTransactionalIsolation catches it.

`join_transaction_mode="create_savepoint"` is stated explicitly for the reader,
not because the default is wrong — SQLAlchemy 2.0 defaults to
"conditional_savepoint", which already creates a savepoint when the Session
joins a Connection with an open transaction. Naming it keeps the behaviour from
depending on a default, and says out loud why a route's commit is survivable.

ESPN is ALWAYS faked: `get_data_source` is overridden with FakePlayerDataSource,
so no test touches the network.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session

from warroom.config import get_settings
from warroom.db import Base, get_db
from warroom.deps import get_data_source
from warroom.main import create_app
from warroom.models import Board, BoardShare, League, Player, ScoringFormat, SharePermission, User
from warroom.security import create_access_token, hash_password
from warroom.valuation.data_source import FakePlayerDataSource
from warroom.valuation.domain import LeagueSettings, PlayerProjection

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SEASON = 2027
ESPN_LEAGUE_ID = 19048

# Argon2 costs ~50 ms per call by design. Hashing once at import keeps that off
# every single test that needs a user; two users per test would otherwise add
# ~100 ms to each one.
TEST_PASSWORD = "correct-horse-battery-staple"
_PASSWORD_HASH = hash_password(TEST_PASSWORD)

# The SPEC 5.3 worked example, so every valuation assertion has a hand-computed
# expected answer already written down in the spec.
#
#   num_teams=2, slots PG:1 / C:1 / UTIL:1, weight pts:1
#   PG pool 50/40/30 -> replacement 40      C pool 45/20/10 -> replacement 20
#   UTIL pool 50,45,40,30,25,20,10          -> replacement 45
#   expected ranking by value: [4, 1, 2, 5, 3, 6, 7]
SPEC_53_POOL = (
    # espn_player_id, name, positions, projected points
    (1, "Paul Guard", ("PG",), 50.0),
    (2, "Peter Guard", ("PG",), 40.0),
    (3, "Piotr Guard", ("PG",), 30.0),
    (4, "Cal Center", ("C",), 45.0),
    (5, "Colin Center", ("C",), 20.0),
    (6, "Curtis Center", ("C",), 10.0),
    (7, "Sam Forward", ("SF",), 25.0),
)

SPEC_53_EXPECTED_RANKING = [4, 1, 2, 5, 3, 6, 7]


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


def _test_database_url() -> str:
    """The test database URL, which must never be the development one.

    warroom/db.py builds its engine at import time from Settings, and
    get_settings is lru_cached — so the app's engine cannot be repointed after
    import. The suite therefore builds its own engine here and overrides
    get_db, leaving the app's (lazy, never-connected) engine unused.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit

    url = make_url(get_settings().database_url)
    if url.database and url.database.endswith("_test"):
        # CI already points DATABASE_URL at warroom_test.
        return url.render_as_string(hide_password=False)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A session-wide engine over a freshly built test schema."""
    test_url = make_url(_test_database_url())

    # Refuse anything not visibly a test database. The check is the _test suffix
    # rather than "differs from DATABASE_URL": in CI they are the same string,
    # because DATABASE_URL there already points at warroom_test. Comparing the
    # two instead makes the suite refuse to run on CI — which it did, once.
    if not (test_url.database or "").endswith("_test"):
        pytest.fail(
            f"refusing to run the suite against {test_url.database!r} — the "
            f"fixtures drop and recreate every table, and that name is not a "
            f"test database. Point TEST_DATABASE_URL at one ending in '_test'."
        )

    test_engine = create_engine(test_url, pool_pre_ping=True)

    # Build from metadata, not migrations. CI runs `alembic upgrade head` (and a
    # downgrade/upgrade round trip) as its own step before pytest, so drift
    # between models.py and the migrations is caught there rather than here.
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)

    yield test_engine

    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    """A session inside a transaction that is always rolled back."""
    connection = engine.connect()
    trans = connection.begin()

    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    )

    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


# --------------------------------------------------------------------------- #
# The faked ESPN data source (SPEC 2.2)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def league_settings() -> LeagueSettings:
    return LeagueSettings(
        scoring_format="points",
        num_teams=2,
        roster_slots={"PG": 1, "C": 1, "UTIL": 1},
        point_weights={"pts": 1.0},
    )


@pytest.fixture(scope="session")
def player_pool() -> list[PlayerProjection]:
    return [
        PlayerProjection(
            espn_player_id=espn_id,
            name=name,
            positions=positions,
            stats={"pts": points},
            pro_team="BOS",
        )
        for espn_id, name, positions, points in SPEC_53_POOL
    ]


@pytest.fixture
def fake_data_source(
    league_settings: LeagueSettings, player_pool: list[PlayerProjection]
) -> FakePlayerDataSource:
    return FakePlayerDataSource(settings=league_settings, players=player_pool)


# --------------------------------------------------------------------------- #
# The application under test
# --------------------------------------------------------------------------- #


@pytest.fixture
def client(db: Session, fake_data_source: FakePlayerDataSource) -> Iterator[TestClient]:
    """A TestClient whose app shares this test's session and fakes ESPN.

    The get_db override deliberately returns the session without closing it:
    closing would detach every object the fixtures still hold.
    """
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_data_source] = lambda: fake_data_source

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Identities (SPEC 6: the two the authorization matrix is written against)
# --------------------------------------------------------------------------- #


def _make_user(db: Session, email: str) -> User:
    user = User(email=email, password_hash=_PASSWORD_HASH)
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def user_a(db: Session) -> User:
    """The owner. Everything in the matrix belongs to A."""
    return _make_user(db, "a@example.com")


@pytest.fixture
def user_b(db: Session) -> User:
    """The other party — shared with, or denied."""
    return _make_user(db, "b@example.com")


@pytest.fixture
def token_a(user_a: User) -> str:
    return create_access_token(subject=user_a.id)


@pytest.fixture
def token_b(user_b: User) -> str:
    return create_access_token(subject=user_b.id)


@pytest.fixture
def auth_a(token_a: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_a}"}


@pytest.fixture
def auth_b(token_b: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_b}"}


# --------------------------------------------------------------------------- #
# Owned data
# --------------------------------------------------------------------------- #


@pytest.fixture
def players(db: Session) -> list[Player]:
    """The cached ESPN pool — reference data owned by no user (SPEC 0.2).

    Seeded inside the test's transaction like everything else. Committing these
    session-wide instead would strand them: rankings.player_id and
    mock_picks.player_id are ON DELETE RESTRICT, so cleanup would be blocked by
    the very constraint that protects them.
    """
    rows = [
        Player(
            espn_player_id=espn_id,
            season=SEASON,
            name=name,
            pro_team="BOS",
            positions=list(positions),
            projections={"pts": points},
        )
        for espn_id, name, positions, points in SPEC_53_POOL
    ]
    db.add_all(rows)
    db.commit()
    return rows


@pytest.fixture
def league(db: Session, user_a: User, league_settings: LeagueSettings) -> League:
    """A points league owned by A, matching the faked data source's settings."""
    row = League(
        user_id=user_a.id,
        espn_league_id=ESPN_LEAGUE_ID,
        season=SEASON,
        name="Test League",
        scoring_format=ScoringFormat.POINTS,
        num_teams=league_settings.num_teams,
        roster_size=13,
        roster_slots=league_settings.roster_slots,
        point_weights=league_settings.point_weights,
        espn_s2_encrypted=None,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def board(db: Session, user_a: User, league: League) -> Board:
    """A board owned by A — the resource the whole authz matrix points at."""
    row = Board(user_id=user_a.id, league_id=league.id, name="Test Board")
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# Shares (SPEC 4) — a factory, because read and edit are mutually exclusive
# --------------------------------------------------------------------------- #


@pytest.fixture
def share_board_with(db: Session) -> Callable[[Board, User, SharePermission], BoardShare]:
    """Grant one user one permission on one board.

    A factory rather than the `read_share` + `edit_share` pair SPEC 6 words it
    as: unique(board_id, shared_with_user_id) means a single user cannot hold
    both a read and an edit share on the same board, so fixtures creating both
    would raise IntegrityError. That constraint is deliberate — two rows would
    make "does B have edit access?" depend on row order — so the fixture bends,
    not the schema.
    """

    def _share(board: Board, user: User, permission: SharePermission) -> BoardShare:
        row = BoardShare(
            board_id=board.id,
            shared_with_user_id=user.id,
            permission=permission,
        )
        db.add(row)
        db.commit()
        return row

    return _share


@pytest.fixture
def read_share(
    share_board_with: Callable[..., BoardShare], board: Board, user_b: User
) -> BoardShare:
    """B holds a READ share on A's board."""
    return share_board_with(board, user_b, SharePermission.READ)


@pytest.fixture
def edit_share(
    share_board_with: Callable[..., BoardShare], board: Board, user_b: User
) -> BoardShare:
    """B holds an EDIT share on A's board. Mutually exclusive with read_share."""
    return share_board_with(board, user_b, SharePermission.EDIT)
