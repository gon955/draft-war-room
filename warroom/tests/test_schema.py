"""Schema assertions against SPEC 3 — pure metadata, no database.

These exist because of a class of bug that passes every other check. A missing
dunder on `__table_args__`, a wrong `ondelete`, a NOT NULL where the spec says
null: the module imports, `configure_mappers()` succeeds, ruff is clean, and the
migration generates happily against a schema that is quietly wrong. Two unique
constraints were silently absent this way — including
unique(board_id, shared_with_user_id), whose loss lets one user hold both a read
and an edit share on the same board and makes "does B have edit access?"
depend on row order.

Nothing here connects to Postgres; it reads Base.metadata after import. That
does mean importing warroom.models needs DATABASE_URL and JWT_SECRET set, which
CI supplies in its env block and local dev gets from .env.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

import warroom.models as m
from warroom.db import Base

ALL_TABLES = (
    "users",
    "leagues",
    "players",
    "valuations",
    "boards",
    "tiers",
    "rankings",
    "mock_drafts",
    "mock_picks",
    "board_shares",
)


def table(name):
    return Base.metadata.tables[name]


def unique_sets(name):
    """The column tuples covered by UniqueConstraints on a table."""
    return {
        tuple(c.name for c in con.columns)
        for con in table(name).constraints
        if type(con).__name__ == "UniqueConstraint"
    }


def ondelete(table_name, column):
    """The ON DELETE rule on a column's foreign key."""
    fk = next(fk for fk in table(table_name).foreign_keys if fk.parent.name == column)
    return fk.ondelete


def test_every_spec_table_exists():
    assert set(ALL_TABLES) <= set(Base.metadata.tables)


class TestUniqueConstraints:
    """SPEC 3. Each of these carries meaning, not decoration."""

    @pytest.mark.parametrize(
        ("table_name", "columns"),
        [
            # A league is season-scoped: the same ESPN league in a new season is
            # a different row, because the roster shape genuinely changes.
            ("leagues", ("user_id", "espn_league_id", "season")),
            ("players", ("espn_player_id", "season")),
            ("valuations", ("league_id", "player_id")),
            ("rankings", ("board_id", "player_id")),
            ("mock_picks", ("mock_draft_id", "pick_number")),
            ("board_shares", ("board_id", "shared_with_user_id")),
        ],
    )
    def test_constraint_is_present(self, table_name, columns):
        assert columns in unique_sets(table_name), (
            f"unique{columns} missing from {table_name} — check for a typo'd "
            f"`table_args` instead of `__table_args__`, which SQLAlchemy ignores"
        )

    def test_one_share_row_per_board_and_user(self):
        # Called out on its own because it is an authorization constraint, not a
        # data-hygiene one: two rows would mean read and edit at once.
        assert ("board_id", "shared_with_user_id") in unique_sets("board_shares")


class TestIndexes:
    def test_email_is_unique_case_insensitively(self):
        # SPEC 3 says email is unique and stored lowercased. A plain unique
        # constraint is case-sensitive, so Alice@x.com and alice@x.com would
        # both insert; the functional index is what actually enforces it.
        ix = next(i for i in table("users").indexes if "email" in i.name)
        assert ix.unique
        assert "lower" in str(ix.expressions[0]).lower()

    @pytest.mark.parametrize(
        ("table_name", "columns"),
        [("players", ("season",)), ("rankings", ("board_id", "user_rank"))],
    )
    def test_index_is_present(self, table_name, columns):
        found = {tuple(c.name for c in i.columns) for i in table(table_name).indexes}
        assert columns in found


class TestForeignKeyBehaviour:
    """Who takes what down. SPEC 0.2 and 3."""

    @pytest.mark.parametrize(
        ("table_name", "column"),
        [
            ("leagues", "user_id"),
            ("boards", "user_id"),
            ("boards", "league_id"),
            ("tiers", "board_id"),
            ("rankings", "board_id"),
            ("mock_drafts", "board_id"),
            ("mock_picks", "mock_draft_id"),
            ("board_shares", "board_id"),
            ("board_shares", "shared_with_user_id"),
            ("valuations", "league_id"),
        ],
    )
    def test_owned_rows_cascade(self, table_name, column):
        # DELETE /boards/{id} must not fail on a foreign key the first time a
        # board has any content.
        assert ondelete(table_name, column) == "CASCADE"

    @pytest.mark.parametrize("table_name", ["rankings", "mock_picks"])
    def test_user_data_blocks_player_deletion(self, table_name):
        # players is shared reference data owned by no user (SPEC 0.2), refreshed
        # from ESPN. CASCADE here would let a routine resync delete every user's
        # rankings, notes and target flags for a dropped player — across every
        # board, including other people's. It must fail loudly instead.
        assert ondelete(table_name, "player_id") == "RESTRICT"

    def test_deleting_a_tier_keeps_its_rankings(self):
        assert ondelete("rankings", "tier_id") == "SET NULL"

    def test_cascading_relationships_let_postgres_do_the_work(self):
        # cascade="all, delete-orphan" without passive_deletes makes SQLAlchemy
        # load every child and delete it row by row, so the database-level
        # ON DELETE CASCADE never fires.
        for mapper in Base.registry.mappers:
            for rel in mapper.relationships:
                if "delete-orphan" in rel.cascade:
                    assert rel.passive_deletes, f"{mapper.class_.__name__}.{rel.key}"


class TestNullability:
    @pytest.mark.parametrize(
        ("table_name", "column"),
        [
            # The engine seeds the order; user_rank is set only on manual override.
            ("rankings", "user_rank"),
            ("rankings", "note"),
            ("rankings", "tier_id"),
            ("tiers", "color"),
            # An unmade pick has no player yet.
            ("mock_picks", "player_id"),
            # A public league needs no cookie at all (SPEC 2.1).
            ("leagues", "espn_s2_encrypted"),
        ],
    )
    def test_column_is_nullable(self, table_name, column):
        assert table(table_name).c[column].nullable

    @pytest.mark.parametrize(
        ("table_name", "column"),
        [
            ("users", "email"),
            ("users", "password_hash"),
            ("leagues", "point_weights"),
            ("leagues", "roster_slots"),
            ("leagues", "roster_size"),
            ("players", "projections"),
            ("valuations", "value"),
            ("board_shares", "permission"),
        ],
    )
    def test_column_is_required(self, table_name, column):
        assert not table(table_name).c[column].nullable


class TestEnums:
    @pytest.mark.parametrize(
        ("enum_cls", "labels"),
        [(m.SharePermission, ["read", "edit"]), (m.ScoringFormat, ["points", "categories"])],
    )
    def test_postgres_stores_values_not_member_names(self, enum_cls, labels):
        # SQLAlchemy defaults to the member NAMES as labels, so this would be
        # ['READ', 'EDIT'] without values_callable — and the str/Enum mixin does
        # not change it. SPEC 4 authorizes on permission='edit'.
        column = {
            m.SharePermission: table("board_shares").c.permission,
            m.ScoringFormat: table("leagues").c.scoring_format,
        }[enum_cls]
        assert column.type.enums == labels


class TestPrimaryKeys:
    @pytest.mark.parametrize("table_name", ALL_TABLES)
    def test_pk_is_a_uuid_with_a_default(self, table_name):
        # SPEC 3: UUID throughout. Sequential integers would make resource
        # enumeration free, which undercuts returning 404-not-403 (SPEC 0.3).
        pk = list(table(table_name).primary_key.columns)
        assert len(pk) == 1
        assert pk[0].type.python_type is uuid.UUID
        # Without a default the first INSERT fails on NOT NULL.
        assert pk[0].default is not None
        assert pk[0].server_default is not None


class TestJsonbColumns:
    @pytest.mark.parametrize(
        ("table_name", "column"),
        [
            ("leagues", "roster_slots"),
            ("leagues", "point_weights"),
            ("players", "positions"),
            ("players", "projections"),
        ],
    )
    def test_column_is_jsonb_not_json(self, table_name, column):
        # Generic JSON renders as Postgres `json`, which cannot be GIN-indexed
        # or compared for equality. SPEC 1 makes JSONB the reason Postgres is
        # mandatory rather than merely preferred.
        assert isinstance(table(table_name).c[column].type, JSONB)


class TestTimestamps:
    def test_every_datetime_column_is_timezone_aware(self):
        # TIMESTAMP WITHOUT TIME ZONE plus now() (which returns timestamptz)
        # makes Postgres silently cast to server-local time. The fix lives in
        # db.py's type_annotation_map, not on the column — so name the columns.
        naive = [
            (t.name, col.name)
            for t in Base.metadata.tables.values()
            for col in t.columns
            if isinstance(col.type, sa.DateTime) and not col.type.timezone
        ]
        assert not naive, f"naive datetime columns: {naive}"
