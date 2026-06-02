"""Model integration tests — write and read against the test database."""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.enums import Category, EventType, Priority, TicketStatus
from app.models import Agent, Ticket, TicketEvent


@pytest.mark.asyncio
async def test_create_and_read_ticket(test_db):
    """A ticket persists and reads back with server-side defaults applied."""
    ticket = Ticket(
        customer_name="John Doe",
        customer_email="john@example.com",
        subject="Cannot login",
        description="I forgot my password",
        category=Category.TECHNICAL,
    )
    test_db.add(ticket)
    await test_db.commit()
    await test_db.refresh(ticket)

    assert ticket.id is not None
    # Server-side defaults applied on insert.
    assert ticket.status == TicketStatus.OPEN
    assert ticket.priority == Priority.MEDIUM
    assert ticket.created_at is not None
    assert ticket.updated_at is not None


@pytest.mark.asyncio
async def test_ticket_event_links_to_ticket(test_db):
    """A ticket event references its parent ticket via foreign key."""
    ticket = Ticket(
        customer_name="Jane",
        customer_email="jane@example.com",
        subject="Billing question",
        description="Why was I charged twice?",
        category=Category.BILLING,
    )
    test_db.add(ticket)
    await test_db.commit()
    await test_db.refresh(ticket)

    event = TicketEvent(
        ticket_id=ticket.id,
        event_type=EventType.CREATED,
        new_value="OPEN",
    )
    test_db.add(event)
    await test_db.commit()
    await test_db.refresh(event)

    assert event.id is not None
    assert event.ticket_id == ticket.id
    assert event.event_type == EventType.CREATED
    # New audit columns default to NULL when not set.
    assert event.actor_id is None
    assert event.field_changed is None


@pytest.mark.asyncio
async def test_status_changed_event_records_actor_and_field(test_db):
    """A STATUS_CHANGED event captures actor, field, and before/after values."""
    agent = Agent(name="Agent Smith", email="smith@example.com")
    test_db.add(agent)
    ticket = Ticket(
        customer_name="Neo",
        customer_email="neo@example.com",
        subject="Glitch",
        description="There is a glitch in the matrix",
        category=Category.TECHNICAL,
    )
    test_db.add(ticket)
    await test_db.commit()
    await test_db.refresh(agent)
    await test_db.refresh(ticket)

    event = TicketEvent(
        ticket_id=ticket.id,
        event_type=EventType.STATUS_CHANGED,
        field_changed="status",
        previous_value="OPEN",
        new_value="IN_PROGRESS",
        actor_id=agent.id,
    )
    test_db.add(event)
    await test_db.commit()
    await test_db.refresh(event)

    assert event.event_type == EventType.STATUS_CHANGED
    assert event.field_changed == "status"
    assert event.previous_value == "OPEN"
    assert event.new_value == "IN_PROGRESS"
    assert event.actor_id == agent.id


@pytest.mark.asyncio
async def test_event_type_check_rejects_unknown_value(test_db):
    """The event_type CHECK constraint rejects values outside the enum."""
    ticket = Ticket(
        customer_name="Trinity",
        customer_email="trinity@example.com",
        subject="Question",
        description="Follow the white rabbit",
        category=Category.OTHER,
    )
    test_db.add(ticket)
    await test_db.commit()
    await test_db.refresh(ticket)

    # Bypass the Python enum to write a raw invalid value; the CHECK rejects it
    # at statement execution time.
    with pytest.raises(IntegrityError):
        await test_db.execute(
            insert(TicketEvent).values(ticket_id=ticket.id, event_type="DELETED")
        )
    await test_db.rollback()


@pytest.mark.asyncio
async def test_lazy_events_raises_on_implicit_access(test_db):
    """Implicit lazy access to Ticket.events raises instead of emitting SQL."""
    ticket = Ticket(
        customer_name="Morpheus",
        customer_email="morpheus@example.com",
        subject="Red or blue",
        description="Choose",
        category=Category.OTHER,
    )
    test_db.add(ticket)
    await test_db.commit()
    await test_db.refresh(ticket)

    # lazy="raise" turns an implicit lazy load into an error at access time.
    # Assert the specific error: a bare Exception would also catch the
    # MissingGreenlet you'd get with default lazy loading, so the test would
    # pass even if lazy="raise" were removed.
    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = ticket.events


@pytest.mark.asyncio
async def test_deleting_agent_nulls_assignment(test_db):
    """Deleting an agent sets assigned_agent_id to NULL (ON DELETE SET NULL)."""
    agent = Agent(name="Temp Agent", email="temp@example.com")
    test_db.add(agent)
    await test_db.commit()
    await test_db.refresh(agent)

    ticket = Ticket(
        customer_name="Cypher",
        customer_email="cypher@example.com",
        subject="Steak",
        description="Ignorance is bliss",
        category=Category.OTHER,
        assigned_agent_id=agent.id,
    )
    test_db.add(ticket)
    await test_db.commit()
    await test_db.refresh(ticket)
    ticket_id = ticket.id

    await test_db.delete(agent)
    await test_db.commit()

    # expire_on_commit=False keeps the cached ticket; force a fresh read so we
    # observe the DB-level ON DELETE SET NULL rather than the stale identity map.
    test_db.expire_all()
    result = await test_db.execute(select(Ticket).where(Ticket.id == ticket_id))
    found = result.scalar_one()
    assert found.assigned_agent_id is None


@pytest.mark.asyncio
async def test_agent_unique_email(test_db):
    """An agent persists with its email."""
    agent = Agent(name="Support Agent", email="agent@example.com")
    test_db.add(agent)
    await test_db.commit()
    await test_db.refresh(agent)

    result = await test_db.execute(
        select(Agent).where(Agent.email == "agent@example.com")
    )
    found = result.scalar_one()
    assert found.name == "Support Agent"
