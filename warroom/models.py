"""The relational schema (SPEC 3). UUID primary keys throughout.

One module rather than a package: all ten tables are densely cross-referenced
(boards -> rankings -> players, boards <-> users via board_shares), and keeping
them together avoids the circular-import dance that splitting them invites.

Tables, and the constraints that carry meaning rather than decoration:

  users          email unique, stored lowercased
  leagues        unique(user_id, espn_league_id, season) — season-scoped because
                 a league's roster shape really does change year to year: this
                 league ran F/C in 2026 and SG/SF in 2027
  players        unique(espn_player_id, season), index(season) — shared reference
                 data, cached from ESPN, owned by no user (SPEC 0.2)
  valuations     unique(league_id, player_id) — the engine's output, cached
  boards         a user may keep several per league as competing strategies
  tiers          board-scoped, ordered by sort_order
  rankings       unique(board_id, player_id), index(board_id, user_rank) — the
                 CRUD-heavy table
  mock_drafts    board-scoped
  mock_picks     unique(mock_draft_id, pick_number)
  board_shares   unique(board_id, shared_with_user_id), permission read|edit —
                 the many-to-many that makes authz interesting

Every user-owned row must trace to a user_id: that chain is what authz.py walks.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    UUID,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from warroom.db import Base

# ==============================================================================
# Helpers & Enums
# ==============================================================================


def enum_column(enum_cls: type[enum.Enum], name: str):
    """Helper to enforce explicit lowercased enum names and labels in PostgreSQL DDL."""
    return mapped_column(
        Enum(enum_cls, name=name, values_callable=lambda e: [x.value for x in e]), nullable=False
    )


def uuid_pk():

    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


class SharePermission(enum.Enum):
    READ = "read"
    EDIT = "edit"


class ReplacementBasis(enum.Enum):
    """What this league's valuations measure a player against.

    Mirrors valuation.engine.ReplacementBasis. Stored per league rather than
    read from config because it changes what every cached number in
    `valuations` MEANS — a board computed one way and read the other is
    silently wrong, and there would be nothing in the row to say so.
    """

    STARTER = "starter"
    WAIVER = "waiver"
    MARGINAL = "marginal"


class ScoringFormat(enum.Enum):
    POINTS = "points"
    CATEGORIES = "categories"


# ==============================================================================
# Mixins
# ==============================================================================


class TimestampMixin:
    """Provides unified audit tracking for temporal-sensitive tables.

    clock_timestamp(), not now(). Postgres `now()` is transaction_timestamp():
    it returns the moment the transaction began, so every row written or touched
    in one transaction shares a timestamp and updated_at never advances past
    created_at. That makes the audit columns useless for anything batched — and
    it makes the whole integration suite, which runs each test in a single
    transaction, unable to tell an update from an insert. clock_timestamp()
    reads the actual wall clock per statement.
    """

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), onupdate=func.clock_timestamp(), nullable=False
    )


# ==============================================================================
# Models
# ==============================================================================


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(nullable=False)
    password_hash: Mapped[str] = mapped_column(nullable=False)

    # Graph Traversal Anchors
    leagues: Mapped[list["League"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    boards: Mapped[list["Board"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    shares_received: Mapped[list["BoardShare"]] = relationship(
        back_populates="shared_with_user", cascade="all, delete-orphan", passive_deletes=True
    )


class League(Base, TimestampMixin):
    __tablename__ = "leagues"
    __table_args__ = (UniqueConstraint("user_id", "espn_league_id", "season"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    espn_league_id: Mapped[int] = mapped_column(nullable=False)
    season: Mapped[int] = mapped_column(nullable=False)

    # Payload
    name: Mapped[str] = mapped_column(nullable=False)
    scoring_format: Mapped[ScoringFormat] = enum_column(ScoringFormat, "scoring_format")
    num_teams: Mapped[int] = mapped_column(nullable=False)
    roster_size: Mapped[int] = mapped_column(nullable=False)
    espn_s2_encrypted: Mapped[str | None] = mapped_column(nullable=True)
    # Defaults to STARTER so an existing league's numbers do not move until
    # somebody asks them to; server_default so the migration can backfill.
    replacement_basis: Mapped[ReplacementBasis] = mapped_column(
        Enum(
            ReplacementBasis,
            name="replacement_basis",
            values_callable=lambda e: [x.value for x in e],
        ),
        nullable=False,
        default=ReplacementBasis.STARTER,
        server_default="starter",
    )

    # PostgreSQL JSONB Fields (Via db.Base.type_annotation_map)
    roster_slots: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    point_weights: Mapped[dict[str, Any]] = mapped_column(nullable=False)

    # Graph Traversal Anchors
    user: Mapped["User"] = relationship(back_populates="leagues")
    valuations: Mapped[list["Valuation"]] = relationship(
        back_populates="league", cascade="all, delete-orphan", passive_deletes=True
    )
    boards: Mapped[list["Board"]] = relationship(
        back_populates="league", cascade="all, delete-orphan", passive_deletes=True
    )


class Player(Base, TimestampMixin):
    """Shared global reference data cached from ESPN; owned by no user (SPEC 0.2)."""

    __tablename__ = "players"
    __table_args__ = (
        UniqueConstraint("espn_player_id", "season"),
        Index("ix_players_season", "season"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    espn_player_id: Mapped[int] = mapped_column(nullable=False)
    season: Mapped[int] = mapped_column(nullable=False)

    # Payload
    name: Mapped[str] = mapped_column(nullable=False)
    pro_team: Mapped[str] = mapped_column(nullable=False)

    # Explicit JSONB mapping bypassing the dict-only map rule
    positions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    projections: Mapped[dict[str, Any]] = mapped_column(nullable=False)

    # Graph Traversal Anchors (Accidental global cache mutation must fail loudly)
    valuations: Mapped[list["Valuation"]] = relationship(back_populates="player")
    rankings: Mapped[list["Ranking"]] = relationship(back_populates="player")
    mock_picks: Mapped[list["MockPick"]] = relationship(back_populates="player")


class Valuation(Base, TimestampMixin):
    """The analytical engine's output cache."""

    __tablename__ = "valuations"
    __table_args__ = (
        UniqueConstraint("league_id", "player_id"),
        # GET /leagues/{id}/valuations reads one league's rows best-first and
        # pages through them, which is an index-only walk with this and a sort
        # of the whole league without it. DESC matches the query's direction so
        # Postgres reads it forward rather than backward.
        Index("ix_valuations_league_id_value", "league_id", text("value DESC")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    league_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )

    # Payload
    projected_points: Mapped[float] = mapped_column(nullable=False)
    replacement_points: Mapped[float] = mapped_column(nullable=False)
    value: Mapped[float] = mapped_column(nullable=False)
    assigned_slot: Mapped[str] = mapped_column(nullable=False)
    # One standard deviation on this number, in points. Stored beside the
    # value rather than derived on read because it depends on the stat
    # coverage of the run that produced it — a later recompute with better
    # ESPN data would give a different band for the same value, and the row
    # has to say which one it came with.
    value_sd: Mapped[float] = mapped_column(nullable=False, server_default="0")
    # The share of that band contributed by THIS app's estimators rather
    # than by ESPN. The part that does not cancel when comparing players.
    model_sd: Mapped[float] = mapped_column(nullable=False, server_default="0")
    # clock_timestamp(), for the same reason TimestampMixin uses it: now() is
    # transaction_timestamp(), so a recompute inside one transaction would write
    # back the moment the transaction opened. The upsert in services.valuation
    # must also name this column in its ON CONFLICT set_ — a Core upsert does
    # not re-apply a server default on update, so leaving it out would pin every
    # row at its first-ever compute time and make a fresh cache look stale.
    computed_at: Mapped[datetime] = mapped_column(
        server_default=func.clock_timestamp(), nullable=False
    )

    # Graph Traversal Anchors
    league: Mapped["League"] = relationship(back_populates="valuations")
    player: Mapped["Player"] = relationship(back_populates="valuations")


class Board(Base, TimestampMixin):
    """A user-owned sandbox supporting multiple strategies per league."""

    __tablename__ = "boards"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    league_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("leagues.id", ondelete="CASCADE"), nullable=False
    )

    # Payload
    name: Mapped[str] = mapped_column(nullable=False)

    # Graph Traversal Anchors
    user: Mapped["User"] = relationship(back_populates="boards")
    league: Mapped["League"] = relationship(back_populates="boards")
    tiers: Mapped[list["Tier"]] = relationship(
        back_populates="board", cascade="all, delete-orphan", passive_deletes=True
    )
    rankings: Mapped[list["Ranking"]] = relationship(
        back_populates="board", cascade="all, delete-orphan", passive_deletes=True
    )
    mock_drafts: Mapped[list["MockDraft"]] = relationship(
        back_populates="board", cascade="all, delete-orphan", passive_deletes=True
    )
    shares: Mapped[list["BoardShare"]] = relationship(
        back_populates="board", cascade="all, delete-orphan", passive_deletes=True
    )


class Tier(Base):
    __tablename__ = "tiers"
    __table_args__ = (
        # Redundant against the PK on its own, and load-bearing anyway: it is
        # what rankings' composite FK below references, which is what makes
        # "a ranking's tier belongs to the same board" a database invariant
        # rather than a rule every writer has to remember.
        UniqueConstraint("id", "board_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    board_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(nullable=False)

    # Payload
    label: Mapped[str] = mapped_column(nullable=False)
    color: Mapped[str | None] = mapped_column(nullable=True)

    # Graph Traversal Anchors
    board: Mapped["Board"] = relationship(back_populates="tiers")
    # Joined on tier_id only. board_id is part of the composite FK into this
    # table, so an inferred join would also copy tiers.board_id into
    # rankings.board_id and collide with Board.rankings over who owns that
    # column. The composite constraint stays where it belongs — in the DDL.
    rankings: Mapped[list["Ranking"]] = relationship(
        back_populates="tier",
        primaryjoin=lambda: Tier.id == foreign(Ranking.tier_id),
    )


class Ranking(Base, TimestampMixin):
    """The high-throughput CRUD table handling user rankings."""

    __tablename__ = "rankings"
    __table_args__ = (
        UniqueConstraint("board_id", "player_id"),
        Index("ix_rankings_board_id_user_rank", "board_id", "user_rank"),
        # A tier from ANOTHER board must not be attachable to this ranking. The
        # single-column FK this replaces only checked that the tier existed, so
        # the tier/board mismatch was reachable through the API and would have
        # silently broken every "group this board by tier" read.
        #
        # SET NULL (tier_id) — the column list is Postgres 15+ and is the whole
        # reason this works: an unqualified ON DELETE SET NULL would null every
        # referencing column, board_id included, and board_id is NOT NULL, so
        # deleting a tier would error instead of clearing the tier off its
        # rankings.
        ForeignKeyConstraint(
            ["tier_id", "board_id"],
            ["tiers.id", "tiers.board_id"],
            ondelete="SET NULL (tier_id)",
            name="fk_rankings_tier_id_board_id_tiers",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    board_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("players.id", ondelete="RESTRICT"), nullable=False
    )
    # FK declared at table level (composite, with board_id) — see __table_args__.
    tier_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Nullable indicates no manual override has occurred
    user_rank: Mapped[int | None] = mapped_column(nullable=True)

    # Payload
    note: Mapped[str | None] = mapped_column(nullable=True)
    is_target: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    is_avoid: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )

    # Graph Traversal Anchors
    board: Mapped["Board"] = relationship(back_populates="rankings")
    player: Mapped["Player"] = relationship(back_populates="rankings")
    tier: Mapped["Tier | None"] = relationship(
        back_populates="rankings",
        primaryjoin=lambda: Tier.id == foreign(Ranking.tier_id),
    )


class MockDraft(Base, TimestampMixin):
    __tablename__ = "mock_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()
    board_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), nullable=False
    )

    # Payload
    name: Mapped[str] = mapped_column(nullable=False)
    my_draft_slot: Mapped[int] = mapped_column(nullable=False)

    # Graph Traversal Anchors
    board: Mapped["Board"] = relationship(back_populates="mock_drafts")
    picks: Mapped[list["MockPick"]] = relationship(
        back_populates="mock_draft", cascade="all, delete-orphan", passive_deletes=True
    )


class MockPick(Base):
    __tablename__ = "mock_picks"
    __table_args__ = (UniqueConstraint("mock_draft_id", "pick_number"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    mock_draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mock_drafts.id", ondelete="CASCADE"), nullable=False
    )
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("players.id", ondelete="RESTRICT"), nullable=True
    )
    pick_number: Mapped[int] = mapped_column(nullable=False)
    # Payload
    round: Mapped[int] = mapped_column(nullable=False)
    team_slot: Mapped[int] = mapped_column(nullable=False)
    is_mine: Mapped[bool] = mapped_column(
        default=False, server_default=text("false"), nullable=False
    )
    # Graph Traversal Anchors
    mock_draft: Mapped["MockDraft"] = relationship(back_populates="picks")
    player: Mapped["Player | None"] = relationship(back_populates="mock_picks")


class BoardShare(Base, TimestampMixin):
    __tablename__ = "board_shares"
    __table_args__ = (UniqueConstraint("board_id", "shared_with_user_id"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    board_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("boards.id", ondelete="CASCADE"), nullable=False
    )
    shared_with_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    permission: Mapped[SharePermission] = enum_column(SharePermission, "share_permission")
    # Graph Traversal Anchors
    board: Mapped["Board"] = relationship(back_populates="shares")
    shared_with_user: Mapped["User"] = relationship(back_populates="shares_received")
