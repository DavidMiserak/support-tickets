"""Tests for worker-side Prometheus metrics (worker_jobs_total, duration).

Delta-style assertions: the counters are process-global singletons, so capture
the value before running a task and assert the increment after.
"""

from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket
from app.worker.metrics import worker_job_duration_seconds, worker_jobs_total
from app.worker.tasks import assign_priority, route_ticket, summarize_ticket


def _jobs(task: str, outcome: str) -> float:
    """Current value of worker_jobs_total for a (task, outcome) pair."""
    return float(worker_jobs_total.labels(task=task, outcome=outcome)._value.get())


def _duration_sum(task: str) -> float:
    """Running sum of the duration histogram for a task."""
    return float(worker_job_duration_seconds.labels(task=task)._sum.get())


async def _make_ticket(test_db: AsyncSession) -> Ticket:
    t = Ticket(
        customer_name="Metric",
        customer_email="metric@example.com",
        subject="Cannot log in",
        description="I cannot log in to my account after the password reset email.",
        priority="MEDIUM",
        category="TECHNICAL",
    )
    test_db.add(t)
    await test_db.flush()
    await test_db.commit()
    await test_db.refresh(t)
    return t


async def test_completed_increments_counter_and_duration(
    test_db: AsyncSession, worker_ctx: dict[str, object]
) -> None:
    """A successful summarize records completed + observes a duration."""
    ticket = await _make_ticket(test_db)

    before = _jobs("summarize_ticket", "completed")
    before_sum = _duration_sum("summarize_ticket")

    await summarize_ticket(worker_ctx, ticket.id)

    assert _jobs("summarize_ticket", "completed") - before == 1.0
    assert _duration_sum("summarize_ticket") > before_sum


async def test_not_found_outcome(
    test_db: AsyncSession, worker_ctx: dict[str, object]
) -> None:
    """A task for a missing ticket records the not_found outcome."""
    before = _jobs("route_ticket", "not_found")
    await route_ticket(worker_ctx, ticket_id=999999)
    assert _jobs("route_ticket", "not_found") - before == 1.0


async def test_failed_outcome_on_classifier_error(
    test_db: AsyncSession, worker_ctx: dict[str, object]
) -> None:
    """A classifier exception records the failed outcome (task does not raise)."""
    ticket = await _make_ticket(test_db)
    clf = AsyncMock()
    clf.classify_priority.side_effect = RuntimeError("boom")
    ctx = {**worker_ctx, "priority_classifier": clf}

    before = _jobs("assign_priority", "failed")
    await assign_priority(ctx, ticket.id)
    assert _jobs("assign_priority", "failed") - before == 1.0
