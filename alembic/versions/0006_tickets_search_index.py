"""Add GIN index for full-text search on tickets.subject + description.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Expression index on the combined tsvector. to_tsvector('english', ...)
    # with a constant config argument is IMMUTABLE, so PostgreSQL permits it
    # as an index expression. The query in TicketRepository.list() uses the
    # identical expression, so the planner will choose this index on search
    # requests and fall back to seq-scan when no search term is present.
    op.execute(
        "CREATE INDEX idx_tickets_search ON tickets "
        "USING gin(to_tsvector('english', subject || ' ' || description))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX idx_tickets_search")
