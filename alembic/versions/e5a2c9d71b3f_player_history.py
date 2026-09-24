"""prior-season history on players

Revision ID: e5a2c9d71b3f
Revises: 0dccb8c24ffc
Create Date: 2026-09-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a2c9d71b3f"
down_revision: str | Sequence[str] | None = "0dccb8c24ffc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Prior seasons' actual totals, captured at sync.

    NOT NULL with a '{}' default rather than nullable: an empty history already
    means "no seasons known", which is exactly the truth for every row written
    before this column existed. A separate NULL would be a fourth state with
    the same meaning. One re-sync per league fills them.
    """
    op.add_column(
        "players",
        sa.Column(
            "history",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("players", "history")
