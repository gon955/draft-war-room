"""league replacement basis

Adds leagues.replacement_basis: what this league's valuations measure a player
against. See valuation/engine.py ReplacementBasis.

Defaults to 'starter', which is what every existing row was computed with, so
the backfill is a no-op in meaning as well as in value — nobody's cached
numbers change until they ask for the other basis and recompute.

Revision ID: 53fd6f9ca747
Revises: bb43414b6cec
Create Date: 2026-09-21 16:22:14.638931

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53fd6f9ca747"
down_revision: str | Sequence[str] | None = "bb43414b6cec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Named once and reused by both directions. The downgrade has to drop the TYPE
# as well as the column: Postgres keeps an enum type after its last column
# goes, so a bare drop_column leaves it behind and the next upgrade fails with
# "type already exists". CI runs `downgrade -1 && upgrade head` precisely to
# catch that, and the initial migration drops its own enums for the same
# reason.
replacement_basis = sa.Enum("starter", "waiver", name="replacement_basis")


def upgrade() -> None:
    """Upgrade schema."""
    replacement_basis.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "leagues",
        sa.Column(
            "replacement_basis",
            replacement_basis,
            server_default="starter",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("leagues", "replacement_basis")
    replacement_basis.drop(op.get_bind(), checkfirst=True)
