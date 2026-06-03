"""Add SPAM_FLAGGED and ROUTED to event_type CHECK constraint.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ticket_events DROP CONSTRAINT ck_ticket_events_event_type")
    op.execute(
        "ALTER TABLE ticket_events ADD CONSTRAINT ck_ticket_events_event_type "
        "CHECK (event_type IN ("
        "'CREATED','STATUS_CHANGED','PRIORITY_CHANGED','ASSIGNED','SUMMARIZED',"
        "'SPAM_FLAGGED','ROUTED'"
        "))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM ticket_events " "WHERE event_type IN ('SPAM_FLAGGED', 'ROUTED')"
    )
    op.execute("ALTER TABLE ticket_events DROP CONSTRAINT ck_ticket_events_event_type")
    op.execute(
        "ALTER TABLE ticket_events ADD CONSTRAINT ck_ticket_events_event_type "
        "CHECK (event_type IN ("
        "'CREATED','STATUS_CHANGED','PRIORITY_CHANGED','ASSIGNED','SUMMARIZED'"
        "))"
    )
