"""Tests for arq worker tasks."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.enums import EventType, Priority
from app.models import Ticket, TicketEvent
from app.worker.tasks import (
    assign_priority,
    detect_spam,
    route_ticket,
    summarize_ticket,
)


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
    assert event.field_changed == "summary"
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


async def test_summarize_ticket_restores_correlation_id(ticket, worker_ctx):
    """Correlation ID passed as an arg is set on the correlation_id context var
    so it appears in all log lines emitted during the task.

    Because the test awaits the coroutine directly (not via asyncio.create_task),
    both share the same contextvars context, so the var is readable here after
    the task sets it. Uses a UUID4-shaped ID like API ``X-Request-ID`` values.
    """
    from asgi_correlation_id import correlation_id as corr_id_var

    request_id = str(uuid4())
    prior = corr_id_var.set(None)
    try:
        await summarize_ticket(worker_ctx, ticket.id, correlation_id=request_id)
        assert corr_id_var.get(None) == request_id
    finally:
        corr_id_var.reset(prior)


async def test_summarize_ticket_clears_stale_correlation_id(ticket, worker_ctx):
    """A job with no correlation_id clears a value left by a prior job."""
    from asgi_correlation_id import correlation_id as corr_id_var

    stale_id = str(uuid4())
    prior = corr_id_var.set(None)
    try:
        await summarize_ticket(worker_ctx, ticket.id, correlation_id=stale_id)
        assert corr_id_var.get(None) == stale_id

        await summarize_ticket(worker_ctx, ticket.id)
        assert corr_id_var.get(None) is None
    finally:
        corr_id_var.reset(prior)


async def test_summarize_ticket_elapsed_seconds_in_complete_log(
    ticket, worker_ctx, caplog
):
    """The complete log line includes an elapsed_seconds field."""
    import logging

    with caplog.at_level(logging.INFO, logger="app.worker.tasks"):
        await summarize_ticket(worker_ctx, ticket.id)

    complete_records = [r for r in caplog.records if "complete" in r.getMessage()]
    assert complete_records, "Expected a 'complete' log record"
    record = complete_records[0]
    assert hasattr(
        record, "elapsed_seconds"
    ), "Expected 'elapsed_seconds' attribute on complete log record"


async def test_summarize_ticket_skips_when_ticket_deleted_before_write(
    ticket, test_db, worker_ctx
):
    """Ticket deleted after read but before event write is a silent no-op."""
    from sqlalchemy import delete

    session_factory = worker_ctx["session_factory"]
    ticket_id = ticket.id

    class DeletingSummarizer:
        async def summarize(self, text: str) -> str:
            async with session_factory() as session:
                await session.execute(delete(Ticket).where(Ticket.id == ticket_id))
                await session.commit()
            return "summary after delete"

    ctx = {**worker_ctx, "summarizer": DeletingSummarizer()}

    await summarize_ticket(ctx, ticket_id)  # must not raise

    gone = await test_db.execute(select(Ticket).where(Ticket.id == ticket_id))
    assert gone.scalar_one_or_none() is None
    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket_id,
            TicketEvent.event_type == EventType.SUMMARIZED,
        )
    )
    assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# assign_priority tests
# ---------------------------------------------------------------------------


async def _make_ticket(
    test_db,
    *,
    subject="Help",
    description="I need help.",
    priority="MEDIUM",
    category="OTHER",
):
    t = Ticket(
        customer_name="Test",
        customer_email="test@example.com",
        subject=subject,
        description=description,
        priority=priority,
        category=category,
    )
    test_db.add(t)
    await test_db.flush()
    await test_db.commit()
    await test_db.refresh(t)
    return t


async def test_assign_priority_upgrades_on_critical_keyword(worker_ctx, test_db):
    """Ticket with 'urgent' in description is upgraded to CRITICAL."""
    t = await _make_ticket(
        test_db, description="This is urgent, system is completely down."
    )
    await assign_priority(worker_ctx, t.id)

    await test_db.refresh(t)
    assert t.priority == Priority.CRITICAL

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.PRIORITY_CHANGED,
        )
    )
    event = result.scalar_one()
    assert event.field_changed == "priority"
    assert event.previous_value == Priority.MEDIUM.value
    assert event.new_value == Priority.CRITICAL.value


async def test_assign_priority_upgrades_on_high_keyword(worker_ctx, test_db):
    """Ticket with 'important' in subject is upgraded to HIGH when current is LOW."""
    t = await _make_ticket(
        test_db, subject="Important billing question", priority="LOW"
    )
    await assign_priority(worker_ctx, t.id)

    await test_db.refresh(t)
    assert t.priority == Priority.HIGH


async def test_assign_priority_ignores_substring_false_positives(worker_ctx, test_db):
    """Word-boundary matching: 'download' must not match the 'down' critical keyword."""
    t = await _make_ticket(
        test_db,
        subject="Cannot download my invoice PDF",
        description="The download link in markdown docs is broken.",
        priority="MEDIUM",
    )
    await assign_priority(worker_ctx, t.id)

    await test_db.refresh(t)
    assert t.priority == Priority.MEDIUM

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.PRIORITY_CHANGED,
        )
    )
    assert result.scalar_one_or_none() is None


async def test_assign_priority_does_not_downgrade(worker_ctx, test_db):
    """Ticket already at CRITICAL is not downgraded by a MEDIUM heuristic result."""
    t = await _make_ticket(test_db, description="Normal question.", priority="CRITICAL")
    await assign_priority(worker_ctx, t.id)

    await test_db.refresh(t)
    assert t.priority == Priority.CRITICAL

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.PRIORITY_CHANGED,
        )
    )
    assert result.scalar_one_or_none() is None


async def test_assign_priority_no_op_when_same_level(worker_ctx, test_db):
    """Heuristic returns same level as current — no event written."""
    t = await _make_ticket(
        test_db, description="Normal support question.", priority="MEDIUM"
    )
    await assign_priority(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(TicketEvent.event_type == EventType.PRIORITY_CHANGED)
    )
    assert result.scalars().all() == []


async def test_assign_priority_no_op_when_low_without_keywords(worker_ctx, test_db):
    """Customer-submitted LOW must not be upgraded when no keywords match."""
    t = await _make_ticket(
        test_db,
        subject="Question about my account",
        description="When will my refund be processed?",
        priority="LOW",
    )
    await assign_priority(worker_ctx, t.id)

    await test_db.refresh(t)
    assert t.priority == Priority.LOW

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.PRIORITY_CHANGED,
        )
    )
    assert result.scalar_one_or_none() is None


async def test_assign_priority_no_op_when_ticket_missing(worker_ctx, test_db):
    """Missing ticket is silently skipped."""
    await assign_priority(worker_ctx, ticket_id=99999)

    result = await test_db.execute(
        select(TicketEvent).where(TicketEvent.event_type == EventType.PRIORITY_CHANGED)
    )
    assert result.scalars().all() == []


async def test_assign_priority_concurrent_update_records_skipped(worker_ctx, test_db):
    """StaleDataError on commit is a no-op and counts as skipped, not failed."""
    from unittest.mock import patch

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm.exc import StaleDataError

    from app.worker.metrics import worker_jobs_total

    def _jobs(outcome: str) -> float:
        return float(
            worker_jobs_total.labels(
                task="assign_priority", outcome=outcome
            )._value.get()
        )

    t = await _make_ticket(
        test_db, description="This is urgent, system is completely down."
    )
    skipped_before = _jobs("skipped")
    failed_before = _jobs("failed")

    with patch.object(
        AsyncSession, "commit", side_effect=StaleDataError("UPDATE", 1, 0)
    ):
        await assign_priority(worker_ctx, t.id)

    assert _jobs("skipped") - skipped_before == 1.0
    assert _jobs("failed") - failed_before == 0.0


# ---------------------------------------------------------------------------
# detect_spam tests
# ---------------------------------------------------------------------------


async def test_detect_spam_flags_known_phrase(worker_ctx, test_db):
    """Ticket containing a known spam phrase gets a SPAM_FLAGGED event."""
    t = await _make_ticket(
        test_db, description="Congratulations! You have won a free offer!"
    )
    await detect_spam(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.SPAM_FLAGGED,
        )
    )
    event = result.scalar_one()
    assert event.field_changed == "spam"
    assert event.new_value == "true"


async def test_detect_spam_flags_excessive_urls(worker_ctx, test_db):
    """Ticket with more than 2 URLs is flagged as spam."""
    desc = (
        "Visit https://example.com and http://foo.com and https://bar.com for details."
    )
    t = await _make_ticket(test_db, description=desc)
    await detect_spam(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.SPAM_FLAGGED,
        )
    )
    assert result.scalar_one() is not None


async def test_detect_spam_no_event_for_clean_ticket(worker_ctx, test_db):
    """Normal ticket content does not produce a SPAM_FLAGGED event."""
    t = await _make_ticket(test_db, description="I was charged twice this month.")
    await detect_spam(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(TicketEvent.event_type == EventType.SPAM_FLAGGED)
    )
    assert result.scalars().all() == []


async def test_detect_spam_no_op_when_ticket_missing(worker_ctx, test_db):
    """Missing ticket is silently skipped."""
    await detect_spam(worker_ctx, ticket_id=99999)

    result = await test_db.execute(
        select(TicketEvent).where(TicketEvent.event_type == EventType.SPAM_FLAGGED)
    )
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# route_ticket tests
# ---------------------------------------------------------------------------


async def test_route_ticket_billing_to_billing_dept(worker_ctx, test_db):
    t = await _make_ticket(test_db, category="BILLING")
    await route_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.ROUTED,
        )
    )
    event = result.scalar_one()
    assert event.field_changed == "department"
    assert event.new_value == "billing"


async def test_route_ticket_technical_to_technical_support(worker_ctx, test_db):
    t = await _make_ticket(test_db, category="TECHNICAL")
    await route_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.ROUTED,
        )
    )
    assert result.scalar_one().new_value == "technical-support"


async def test_route_ticket_feature_request_to_product(worker_ctx, test_db):
    t = await _make_ticket(test_db, category="FEATURE_REQUEST")
    await route_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.ROUTED,
        )
    )
    assert result.scalar_one().new_value == "product"


async def test_route_ticket_other_to_general(worker_ctx, test_db):
    t = await _make_ticket(test_db, category="OTHER")
    await route_ticket(worker_ctx, t.id)

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == t.id,
            TicketEvent.event_type == EventType.ROUTED,
        )
    )
    assert result.scalar_one().new_value == "general"


async def test_route_ticket_no_op_when_ticket_missing(worker_ctx, test_db):
    """Missing ticket is silently skipped."""
    await route_ticket(worker_ctx, ticket_id=99999)

    result = await test_db.execute(
        select(TicketEvent).where(TicketEvent.event_type == EventType.ROUTED)
    )
    assert result.scalars().all() == []


# ---------------------------------------------------------------------------
# Classifier wiring — each task must read its classifier from ctx and act on
# the result, regardless of which backend (rules/ML) is installed. The stubs
# return values the default rules backend would NOT produce for this ticket
# (BILLING, MEDIUM, clean text → rules give LOW / not-spam / "billing"), so a
# matching event proves the injected classifier was used.
# ---------------------------------------------------------------------------


async def test_assign_priority_uses_injected_classifier(ticket, worker_ctx, test_db):
    """assign_priority calls ctx['priority_classifier'] and applies its result."""
    clf = AsyncMock()
    clf.classify_priority.return_value = Priority.CRITICAL
    ctx = {**worker_ctx, "priority_classifier": clf}

    await assign_priority(ctx, ticket.id)

    clf.classify_priority.assert_awaited_once_with(ticket.subject, ticket.description)
    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.PRIORITY_CHANGED,
        )
    )
    assert result.scalar_one().new_value == "CRITICAL"


async def test_detect_spam_uses_injected_classifier(ticket, worker_ctx, test_db):
    """detect_spam calls ctx['spam_classifier'] and flags on a True result."""
    clf = AsyncMock()
    clf.classify_spam.return_value = True
    ctx = {**worker_ctx, "spam_classifier": clf}

    await detect_spam(ctx, ticket.id)

    clf.classify_spam.assert_awaited_once_with(ticket.subject, ticket.description)
    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.SPAM_FLAGGED,
        )
    )
    assert result.scalar_one().new_value == "true"


async def test_route_ticket_uses_injected_classifier(ticket, worker_ctx, test_db):
    """route_ticket calls ctx['routing_classifier'] (with category) and applies it."""
    clf = AsyncMock()
    clf.classify_department.return_value = "product"
    ctx = {**worker_ctx, "routing_classifier": clf}

    await route_ticket(ctx, ticket.id)

    clf.classify_department.assert_awaited_once_with(
        ticket.category, ticket.subject, ticket.description
    )
    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.ROUTED,
        )
    )
    assert result.scalar_one().new_value == "product"


async def test_classifier_failure_is_non_fatal(ticket, worker_ctx, test_db):
    """A classifier exception is logged and skips the event, never crashes the task."""
    clf = AsyncMock()
    clf.classify_priority.side_effect = RuntimeError("model boom")
    ctx = {**worker_ctx, "priority_classifier": clf}

    await assign_priority(ctx, ticket.id)  # must not raise

    result = await test_db.execute(
        select(TicketEvent).where(
            TicketEvent.ticket_id == ticket.id,
            TicketEvent.event_type == EventType.PRIORITY_CHANGED,
        )
    )
    assert result.scalar_one_or_none() is None
