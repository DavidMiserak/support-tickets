"""Tests for the summarize_ticket arq task."""

import pytest
from sqlalchemy import select

from app.enums import EventType
from app.models import Ticket, TicketEvent
from app.worker.tasks import summarize_ticket


@pytest.fixture
async def ticket(test_db):
    """Insert a minimal ticket and return it."""
    t = Ticket(
        customer_name="Alice",
        customer_email="alice@example.com",
        subject="Billing question",
        description=(
            "I was charged twice for my subscription this month. "
            "Please review my account and issue a refund for the duplicate charge."
        ),
        priority="MEDIUM",
        category="BILLING",
    )
    test_db.add(t)
    await test_db.flush()
    await test_db.commit()
    await test_db.refresh(t)
    return t


async def test_summarize_ticket_writes_summarized_event(ticket, worker_ctx, test_db):
    """Happy path: a SUMMARIZED event is written after the task runs."""
    await summarize_ticket(worker_ctx, ticket.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    event = result.scalar_one()
    assert event.new_value == ticket.description


async def test_summarize_ticket_no_op_when_ticket_missing(worker_ctx, test_db):
    """Task silently returns when the ticket has been deleted."""
    await summarize_ticket(worker_ctx, ticket_id=99999)

    result = await test_db.execute(
        select(TicketEvent).where(TicketEvent.event_type == EventType.SUMMARIZED)
    )
    assert result.scalars().all() == []


async def test_summarize_ticket_skips_short_description(worker_ctx, test_db):
    """Tickets with fewer than 5 words skip summarization."""
    t = Ticket(
        customer_name="Bob",
        customer_email="bob@example.com",
        subject="Help",
        description="Please help me.",
        priority="LOW",
        category="OTHER",
    )
    test_db.add(t)
    await test_db.flush()
    await test_db.commit()
    await test_db.refresh(t)

    await summarize_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    assert result.scalar_one_or_none() is None


async def test_summarize_ticket_boundary_exactly_4_words_skips(worker_ctx, test_db):
    """Exactly 4 words (one below the threshold) still skips summarization."""
    t = Ticket(
        customer_name="Carol",
        customer_email="carol@example.com",
        subject="Help",
        description="one two three four",
        priority="LOW",
        category="OTHER",
    )
    test_db.add(t)
    await test_db.flush()
    await test_db.commit()
    await test_db.refresh(t)

    await summarize_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    assert result.scalar_one_or_none() is None


async def test_summarize_ticket_boundary_exactly_5_words_proceeds(worker_ctx, test_db):
    """Exactly 5 words (at the threshold) does proceed to summarization."""
    t = Ticket(
        customer_name="Dan",
        customer_email="dan@example.com",
        subject="Help",
        description="one two three four five",
        priority="LOW",
        category="OTHER",
    )
    test_db.add(t)
    await test_db.flush()
    await test_db.commit()
    await test_db.refresh(t)

    await summarize_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    assert result.scalar_one() is not None


async def test_summarize_ticket_skips_on_summarizer_error(ticket, test_db, worker_ctx):
    """When the summarizer raises, no event is written and no exception leaks."""
    from unittest.mock import AsyncMock

    broken_summarizer = AsyncMock()
    broken_summarizer.summarize.side_effect = RuntimeError("inference failed")
    ctx = {**worker_ctx, "summarizer": broken_summarizer}

    await summarize_ticket(ctx, ticket.id)  # must not raise

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    assert result.scalar_one_or_none() is None


async def test_summarize_ticket_skips_on_pipeline_value_error(
    ticket, test_db, worker_ctx
):
    """HuggingFace pipeline ValueError (e.g. sequence too long) is treated as a summarizer error."""
    from unittest.mock import AsyncMock

    broken_summarizer = AsyncMock()
    broken_summarizer.summarize.side_effect = ValueError(
        "sequence length exceeds model max"
    )
    ctx = {**worker_ctx, "summarizer": broken_summarizer}

    await summarize_ticket(ctx, ticket.id)  # must not raise

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    assert result.scalar_one_or_none() is None
