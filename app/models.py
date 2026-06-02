"""SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.enums import Category, EventType, Priority, TicketStatus


class Ticket(Base):
    """Support ticket."""

    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus), default=TicketStatus.OPEN, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        Enum(Priority), default=Priority.MEDIUM, nullable=False
    )
    category: Mapped[Category] = mapped_column(Enum(Category), nullable=False)

    assigned_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Optimistic-lock counter. SQLAlchemy adds `WHERE version_id = :old` to every
    # UPDATE and raises StaleDataError if a concurrent write already bumped it.
    version_id: Mapped[int] = mapped_column(
        nullable=False, server_default="1", default=1
    )

    events: Mapped[list["TicketEvent"]] = relationship(
        back_populates="ticket", lazy="raise"
    )

    __mapper_args__ = {"version_id_col": version_id}
    __table_args__ = (Index("idx_status_created", status, created_at.desc()),)


class TicketEvent(Base):
    """Audit trail for ticket changes."""

    __tablename__ = "ticket_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    # Stored as VARCHAR + CHECK (native_enum=False) rather than a native PG enum,
    # so new event types can be added with a plain migration.
    event_type: Mapped[EventType] = mapped_column(
        Enum(
            EventType,
            native_enum=False,
            length=50,
            create_constraint=True,
            name="ck_ticket_events_event_type",
        ),
        nullable=False,
    )
    # Which ticket field this event describes (e.g. "status"); null for CREATED.
    field_changed: Mapped[str | None] = mapped_column(String(50))
    previous_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    # Agent who performed the action; null for system/customer-driven events.
    # SET NULL keeps the audit row when the agent is deleted.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    ticket: Mapped[Ticket] = relationship(back_populates="events")

    __table_args__ = (Index("idx_ticket_events_ticket_id", ticket_id),)


class Agent(Base):
    """Support agent."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
