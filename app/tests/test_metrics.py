"""Tests for Prometheus business counters.

All assertions use delta style: capture the counter value before the operation,
assert the increment after. Counters are process-global singletons and accumulate
across tests, so absolute-value assertions would be order-dependent.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import (
    ticket_status_transitions_total,
    ticket_summarization_outcomes_total,
    ticket_worker_enqueue_outcomes_total,
    tickets_created_total,
)


def _counter_value(counter: object) -> float:
    """Read the current value of a Counter (no labels)."""
    return float(counter._value.get())  # type: ignore[attr-defined]


def _labeled_value(counter: object, **labels: str) -> float:
    """Read the current value of a labeled Counter."""
    return float(counter.labels(**labels)._value.get())  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_200(async_client: AsyncClient) -> None:
    """GET /metrics returns 200 with Prometheus text format."""
    response = await async_client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_metrics_endpoint_contains_custom_counters(
    async_client: AsyncClient,
) -> None:
    """GET /metrics body includes all three custom counter names."""
    response = await async_client.get("/metrics")
    body = response.text
    assert "tickets_created_total" in body
    assert "ticket_status_transitions_total" in body
    assert "ticket_summarization_outcomes_total" in body


@pytest.mark.asyncio
async def test_metrics_endpoint_omits_process_collectors(
    async_client: AsyncClient,
) -> None:
    """GET /metrics must not leak process/platform/GC internals (issue #9)."""
    await async_client.get("/health")  # warm up so http_requests_total has a sample
    response = await async_client.get("/metrics")
    body = response.text

    # Process/platform/GC collectors are unregistered in app.main.
    assert "process_cpu_seconds_total" not in body
    assert "process_resident_memory_bytes" not in body
    assert "process_open_fds" not in body
    assert "process_start_time_seconds" not in body
    assert "python_info" not in body
    assert "python_gc_objects_collected_total" not in body

    # HTTP + business metrics are still served.
    assert "http_requests_total" in body
    assert "tickets_created_total" in body


# ---------------------------------------------------------------------------
# tickets_created_total
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tickets_created_counter_increments(test_db: AsyncSession) -> None:
    """Creating a ticket increments tickets_created_total by 1."""
    from app.enums import Category
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    before = _counter_value(tickets_created_total)

    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=None)
    await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Ada",
            customer_email="ada@example.com",
            subject="Test",
            description="Testing the counter increment path here",
            category=Category.TECHNICAL,
        )
    )

    assert _counter_value(tickets_created_total) - before == pytest.approx(1)


# ---------------------------------------------------------------------------
# ticket_status_transitions_total
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_transition_counter_increments(test_db: AsyncSession) -> None:
    """A successful status transition increments the labeled counter."""
    from app.enums import Category, TicketStatus
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=None)
    ticket = await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Bob",
            customer_email="bob@example.com",
            subject="Transition",
            description="Testing transition counter incrementing now",
            category=Category.BILLING,
        )
    )

    before = _labeled_value(
        ticket_status_transitions_total,
        from_status="OPEN",
        to_status="IN_PROGRESS",
    )
    await svc.update_status(ticket.id, TicketStatus.IN_PROGRESS)
    after = _labeled_value(
        ticket_status_transitions_total,
        from_status="OPEN",
        to_status="IN_PROGRESS",
    )

    assert after - before == pytest.approx(1)


@pytest.mark.asyncio
async def test_same_status_no_op_does_not_increment(test_db: AsyncSession) -> None:
    """A same-status PATCH (idempotent no-op) does NOT increment the counter."""
    from app.enums import Category, TicketStatus
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=None)
    ticket = await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Carol",
            customer_email="carol@example.com",
            subject="Idempotent",
            description="Testing that same status does not increment counter",
            category=Category.TECHNICAL,
        )
    )

    # Capture total value of all OPEN→OPEN transitions (there should be none)
    before = _labeled_value(
        ticket_status_transitions_total,
        from_status="OPEN",
        to_status="OPEN",
    )
    await svc.update_status(ticket.id, TicketStatus.OPEN)  # same status → no-op
    after = _labeled_value(
        ticket_status_transitions_total,
        from_status="OPEN",
        to_status="OPEN",
    )

    assert after - before == pytest.approx(0)


# ---------------------------------------------------------------------------
# ticket_summarization_outcomes_total
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarization_outcome_enqueued(test_db: AsyncSession) -> None:
    """Successful enqueue increments the 'enqueued' outcome label."""
    from app.enums import Category
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    mock_pool = AsyncMock()
    mock_pool.enqueue_job.return_value = MagicMock()  # truthy job object

    before = _labeled_value(ticket_summarization_outcomes_total, outcome="enqueued")
    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=mock_pool)
    await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Dan",
            customer_email="dan@example.com",
            subject="Enqueue",
            description="Testing enqueue outcome counter label properly",
            category=Category.TECHNICAL,
        )
    )
    after = _labeled_value(ticket_summarization_outcomes_total, outcome="enqueued")
    assert after - before == pytest.approx(1)


@pytest.mark.asyncio
async def test_summarization_outcome_deduped(test_db: AsyncSession) -> None:
    """Deduped enqueue (arq returns None) increments the 'deduped' label."""
    from app.enums import Category
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    mock_pool = AsyncMock()
    mock_pool.enqueue_job.return_value = None  # arq returns None when already queued

    before = _labeled_value(ticket_summarization_outcomes_total, outcome="deduped")
    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=mock_pool)
    await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Eve",
            customer_email="eve@example.com",
            subject="Dedup",
            description="Testing deduped outcome counter label when job exists",
            category=Category.FEATURE_REQUEST,
        )
    )
    after = _labeled_value(ticket_summarization_outcomes_total, outcome="deduped")
    assert after - before == pytest.approx(1)


@pytest.mark.asyncio
async def test_worker_enqueue_outcome_deduped(test_db: AsyncSession) -> None:
    """Analysis-task dedupe (arq returns None) increments the 'deduped' label."""
    from app.enums import Category
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    mock_pool = AsyncMock()
    mock_pool.enqueue_job.return_value = None

    before = _labeled_value(
        ticket_worker_enqueue_outcomes_total,
        task="assign_priority",
        outcome="deduped",
    )
    enqueued_before = _labeled_value(
        ticket_worker_enqueue_outcomes_total,
        task="assign_priority",
        outcome="enqueued",
    )
    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=mock_pool)
    await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Hank",
            customer_email="hank@example.com",
            subject="Worker dedup",
            description="Testing worker enqueue deduped counter for analysis tasks",
            category=Category.BILLING,
        )
    )
    after = _labeled_value(
        ticket_worker_enqueue_outcomes_total,
        task="assign_priority",
        outcome="deduped",
    )
    enqueued_after = _labeled_value(
        ticket_worker_enqueue_outcomes_total,
        task="assign_priority",
        outcome="enqueued",
    )
    assert after - before == pytest.approx(1)
    assert enqueued_after == enqueued_before


@pytest.mark.asyncio
async def test_summarization_outcome_skipped_no_pool(test_db: AsyncSession) -> None:
    """No arq pool (Redis down at startup) increments 'skipped_no_pool'."""
    from app.enums import Category
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    before = _labeled_value(
        ticket_summarization_outcomes_total, outcome="skipped_no_pool"
    )
    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=None)
    await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Frank",
            customer_email="frank@example.com",
            subject="NoPool",
            description="Testing skipped no pool outcome when redis unavailable",
            category=Category.TECHNICAL,
        )
    )
    after = _labeled_value(
        ticket_summarization_outcomes_total, outcome="skipped_no_pool"
    )
    assert after - before == pytest.approx(1)


@pytest.mark.asyncio
async def test_summarization_outcome_enqueue_failed(test_db: AsyncSession) -> None:
    """A pool that raises during enqueue increments 'enqueue_failed'."""
    from app.enums import Category
    from app.repositories.ticket import TicketRepository
    from app.schemas import CreateTicketRequest
    from app.services.ticket import TicketService

    mock_pool = AsyncMock()
    mock_pool.enqueue_job.side_effect = RuntimeError("redis timeout")

    before = _labeled_value(
        ticket_summarization_outcomes_total, outcome="enqueue_failed"
    )
    svc = TicketService(test_db, TicketRepository(test_db), arq_pool=mock_pool)
    await svc.create_ticket(
        CreateTicketRequest(
            customer_name="Grace",
            customer_email="grace@example.com",
            subject="Fail",
            description="Testing enqueue failed outcome when redis raises error",
            category=Category.OTHER,
        )
    )
    after = _labeled_value(
        ticket_summarization_outcomes_total, outcome="enqueue_failed"
    )
    assert after - before == pytest.approx(1)
