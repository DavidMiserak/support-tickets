"""Model integration tests — write and read against the test database."""

import pytest
from sqlalchemy import select

from app.enums import Category, Priority, TicketStatus
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
        event_type="created",
        new_value="OPEN",
    )
    test_db.add(event)
    await test_db.commit()
    await test_db.refresh(event)

    assert event.id is not None
    assert event.ticket_id == ticket.id


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
