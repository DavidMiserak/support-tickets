"""Phase 2a: audit-trail fields, event_type CHECK, agent ON DELETE SET NULL.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-02 13:00:00.000000

Adds ``actor_id`` and ``field_changed`` to ``ticket_events``, converts
``event_type`` from a free String into a VARCHAR + CHECK constraint, and makes
both agent foreign keys ``ON DELETE SET NULL`` so deleting an agent unassigns
tickets and detaches audit rows instead of erroring.
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Auto-generated name PostgreSQL gave the unnamed FK created in 0001.
_OLD_TICKETS_AGENT_FK = "tickets_assigned_agent_id_fkey"
_NEW_TICKETS_AGENT_FK = "fk_tickets_assigned_agent_id_agents"
_EVENTS_ACTOR_FK = "fk_ticket_events_actor_id_agents"
_EVENT_TYPE_CHECK = "ck_ticket_events_event_type"
_ALLOWED_EVENT_TYPES = "('CREATED', 'STATUS_CHANGED', 'PRIORITY_CHANGED')"


def upgrade() -> None:
    """Apply Phase 2a schema changes."""
    # tickets.assigned_agent_id -> ON DELETE SET NULL (drop + recreate the FK).
    op.drop_constraint(_OLD_TICKETS_AGENT_FK, "tickets", type_="foreignkey")
    op.create_foreign_key(
        _NEW_TICKETS_AGENT_FK,
        "tickets",
        "agents",
        ["assigned_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # New audit-trail columns.
    op.add_column(
        "ticket_events", sa.Column("field_changed", sa.String(length=50), nullable=True)
    )
    op.add_column("ticket_events", sa.Column("actor_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        _EVENTS_ACTOR_FK,
        "ticket_events",
        "agents",
        ["actor_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Defensive backfill: 0001 stored event_type as a free String, so existing
    # rows may hold lowercase values (e.g. "created"). Normalize before adding
    # the CHECK, or constraint creation fails on legacy data.
    op.execute("UPDATE ticket_events SET event_type = upper(event_type)")

    # event_type stays VARCHAR(50) but gains a CHECK (the VARCHAR+CHECK pattern,
    # so future event types are added with a plain migration, no ALTER TYPE).
    op.create_check_constraint(
        _EVENT_TYPE_CHECK,
        "ticket_events",
        f"event_type IN {_ALLOWED_EVENT_TYPES}",
    )


def downgrade() -> None:
    """Revert Phase 2a schema changes."""
    op.drop_constraint(_EVENT_TYPE_CHECK, "ticket_events", type_="check")
    op.drop_constraint(_EVENTS_ACTOR_FK, "ticket_events", type_="foreignkey")
    op.drop_column("ticket_events", "actor_id")
    op.drop_column("ticket_events", "field_changed")

    op.drop_constraint(_NEW_TICKETS_AGENT_FK, "tickets", type_="foreignkey")
    op.create_foreign_key(
        _OLD_TICKETS_AGENT_FK,
        "tickets",
        "agents",
        ["assigned_agent_id"],
        ["id"],
    )
