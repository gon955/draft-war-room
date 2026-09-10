"""clock_timestamp for audit columns

Postgres now() is transaction_timestamp(): it returns the moment the transaction
began, so every row written in one transaction shares a created_at, and an
UPDATE in that same transaction cannot move updated_at past created_at. That
makes the audit columns blind to anything batched — a sync writing several
hundred players, a bulk ranking reorder — and it makes the integration suite,
which wraps each test in a single transaction, unable to observe an update at
all.

clock_timestamp() reads the wall clock per statement instead. Only the column
DEFAULT changes; existing values are left exactly as they were, since
back-dating them would invent precision that was never recorded.

Revision ID: 851c95b6de3d
Revises: 8ce0b2956c2b
Create Date: 2026-09-10 21:11:41.507332

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "851c95b6de3d"
down_revision: str | Sequence[str] | None = "8ce0b2956c2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table carrying TimestampMixin. tiers and mock_picks are absent from this
# list on purpose: they have no audit columns.
TIMESTAMPED_TABLES = (
    "users",
    "leagues",
    "players",
    "valuations",
    "boards",
    "rankings",
    "mock_drafts",
    "board_shares",
)

AUDIT_COLUMNS = ("created_at", "updated_at")


def _set_default(function: str) -> None:
    for table in TIMESTAMPED_TABLES:
        for column in AUDIT_COLUMNS:
            op.alter_column(
                table,
                column,
                server_default=sa.text(function),
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
            )


def upgrade() -> None:
    """Upgrade schema."""
    _set_default("clock_timestamp()")


def downgrade() -> None:
    """Downgrade schema."""
    _set_default("now()")
