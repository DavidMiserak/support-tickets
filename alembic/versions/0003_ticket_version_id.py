"""Phase 2b: optimistic-lock version_id on tickets.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-02 14:00:00.000000

Adds ``tickets.version_id`` for SQLAlchemy optimistic concurrency control.
Existing rows default to 1.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the version_id column with a server default so existing rows backfill."""
    op.add_column(
        "tickets",
        sa.Column(
            "version_id",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    """Drop the version_id column."""
    op.drop_column("tickets", "version_id")
