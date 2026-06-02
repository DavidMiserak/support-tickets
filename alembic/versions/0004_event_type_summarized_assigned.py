"""Add SUMMARIZED and ASSIGNED to event_type CHECK constraint.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Extend the event_type CHECK to include ASSIGNED and SUMMARIZED.

    ASSIGNED is included now even though the /assign endpoint is not yet built,
    so that endpoint's migration does not require a second constraint rebuild.
    """
    op.execute("ALTER TABLE ticket_events DROP CONSTRAINT ck_ticket_events_event_type")
    op.execute(
        "ALTER TABLE ticket_events ADD CONSTRAINT ck_ticket_events_event_type "
        "CHECK (event_type IN ("
        "'CREATED','STATUS_CHANGED','PRIORITY_CHANGED','ASSIGNED','SUMMARIZED'"
        "))"
    )


def downgrade() -> None:
    """Restore the original three-value CHECK constraint."""
    op.execute("ALTER TABLE ticket_events DROP CONSTRAINT ck_ticket_events_event_type")
    op.execute(
        "ALTER TABLE ticket_events ADD CONSTRAINT ck_ticket_events_event_type "
        "CHECK (event_type IN ('CREATED','STATUS_CHANGED','PRIORITY_CHANGED'))"
    )
