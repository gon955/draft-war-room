"""tier belongs to board; valuation computed_at and value index

Three changes, all of them groundwork for the tiering and valuation phases.

1. rankings.tier_id becomes half of a COMPOSITE foreign key into
   (tiers.id, tiers.board_id). The single-column FK it replaces only checked
   that the tier existed, so a tier belonging to a different board could be
   attached to a ranking through the API — which silently breaks every read
   that groups a board by its tiers. Referencing two columns needs a unique
   constraint on them, hence uq_tiers_id_board_id.

   ON DELETE SET NULL (tier_id) names the column deliberately: the plain form
   would null every referencing column, board_id included, and board_id is
   NOT NULL — so deleting a tier would raise instead of clearing that tier off
   its rankings. The column list is Postgres 15+.

2. valuations.computed_at moves to clock_timestamp(), for the reason
   851c95b6de3d moved the audit columns: now() is transaction_timestamp(), so
   a recompute inside one transaction writes back the moment the transaction
   opened rather than the moment the work happened.

3. An index on (league_id, value DESC) for the paginated best-first read that
   GET /leagues/{id}/valuations will do.

Revision ID: bb43414b6cec
Revises: 851c95b6de3d
Create Date: 2026-09-17 13:43:05.489936

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb43414b6cec"
down_revision: str | Sequence[str] | None = "851c95b6de3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_computed_at_default(function: str) -> None:
    op.alter_column(
        "valuations",
        "computed_at",
        server_default=sa.text(function),
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=False,
    )


def upgrade() -> None:
    """Upgrade schema."""
    # Order matters: the composite FK cannot be created until the columns it
    # references carry a unique constraint.
    op.create_unique_constraint(op.f("uq_tiers_id_board_id"), "tiers", ["id", "board_id"])
    op.drop_constraint(op.f("fk_rankings_tier_id_tiers"), "rankings", type_="foreignkey")
    op.create_foreign_key(
        "fk_rankings_tier_id_board_id_tiers",
        "rankings",
        "tiers",
        ["tier_id", "board_id"],
        ["id", "board_id"],
        ondelete="SET NULL (tier_id)",
    )

    _set_computed_at_default("clock_timestamp()")

    op.create_index(
        "ix_valuations_league_id_value",
        "valuations",
        ["league_id", sa.literal_column("value DESC")],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_valuations_league_id_value", table_name="valuations")

    _set_computed_at_default("now()")

    # Mirror of upgrade, in reverse: the FK has to go before the unique
    # constraint it depends on.
    op.drop_constraint("fk_rankings_tier_id_board_id_tiers", "rankings", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_rankings_tier_id_tiers"),
        "rankings",
        "tiers",
        ["tier_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(op.f("uq_tiers_id_board_id"), "tiers", type_="unique")
