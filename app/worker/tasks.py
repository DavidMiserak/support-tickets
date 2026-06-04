"""arq task definitions for the background worker."""

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from asgi_correlation_id import correlation_id as _correlation_id_var
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.enums import EventType, Priority
from app.models import Ticket, TicketEvent
from app.worker.metrics import record_job

logger = logging.getLogger(__name__)

_MIN_SUMMARY_WORDS = 5


# ---------------------------------------------------------------------------
# Shared task scaffold
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _ticket_task(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None,
    task_name: str,
) -> AsyncGenerator[tuple[AsyncSession, Ticket, float] | None, None]:
    """Set up correlation ID, timer, session, and ticket fetch for a worker task.

    Yields ``(session, ticket, start_time)`` when the ticket exists, or
    ``None`` when it has been deleted. Callers check for ``None`` and return
    early — the warning log is already emitted here so callers don't need to.

    summarize_ticket is intentionally excluded: it opens two separate sessions
    to release the DB connection during CPU-bound inference, so it cannot share
    this single-session scaffold.
    """
    _correlation_id_var.set(correlation_id)
    start = time.monotonic()
    logger.info("%s: started", task_name, extra={"ticket_id": ticket_id})

    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            logger.warning(
                "%s: ticket not found, skipping",
                task_name,
                extra={"ticket_id": ticket_id},
            )
            yield None
            return
        yield session, ticket, start


# ---------------------------------------------------------------------------
# summarize_ticket (two-session pattern — does not use _ticket_task)
# ---------------------------------------------------------------------------


async def summarize_ticket(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Summarize a ticket's description and store the result as an audit event.

    Part of the summarize-at-create, best-effort-once policy: the API enqueues
    one job per ticket at creation (deduped by job id); this task runs at most
    once per enqueue with max_tries=1 and no automatic retry on failure.
    Multiple SUMMARIZED audit rows per ticket are allowed (e.g. manual re-enqueue).

    The summarizer and session factory are injected via arq's ``ctx`` dict so
    tests can supply stubs without touching global state.

    ``correlation_id`` carries the request ID from the API process. contextvars
    are not serialized across the Redis job queue boundary, so it is passed as
    an explicit arg and restored here so worker log lines share the same
    request_id as the originating API request. The var is always set (including
    to ``None``) so a prior job cannot leave a stale ID on the worker context.

    Failure modes:
    - Ticket deleted before execution: no-op (logged, no event written).
    - Summarizer raises: logged, no event written; no retry (max_tries=1 in WorkerSettings).
    - Ticket deleted between read and write sessions: IntegrityError caught, no-op.
    - DB commit fails for other reasons: exception propagates; no retry (max_tries=1).
    """
    _correlation_id_var.set(correlation_id)

    start = time.monotonic()
    logger.info("summarize_ticket: started", extra={"ticket_id": ticket_id})

    summarizer = ctx["summarizer"]
    session_factory = ctx["session_factory"]

    # Read the ticket in a short-lived session, then release the connection
    # before CPU-bound inference. The write session opens only after summarize.
    async with session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)

    if ticket is None:
        logger.warning(
            "summarize_ticket: ticket not found, skipping",
            extra={"ticket_id": ticket_id},
        )
        record_job("summarize_ticket", "not_found")
        return

    description = ticket.description or ""
    words = description.split()
    if len(words) < _MIN_SUMMARY_WORDS:
        logger.info(
            "summarize_ticket: description too short to summarize, skipping",
            extra={"ticket_id": ticket_id, "word_count": len(words)},
        )
        record_job("summarize_ticket", "skipped")
        return

    try:
        summary = await summarizer.summarize(description)
    except Exception:
        elapsed = round(time.monotonic() - start, 3)
        logger.exception(
            "summarize_ticket: summarizer raised, skipping event write",
            extra={"ticket_id": ticket_id, "elapsed_seconds": elapsed},
        )
        record_job("summarize_ticket", "failed", start)
        return

    try:
        async with session_factory() as session:
            event = TicketEvent(
                ticket_id=ticket_id,
                event_type=EventType.SUMMARIZED,
                field_changed="summary",
                new_value=summary,
            )
            session.add(event)
            await session.commit()
    except IntegrityError:
        # Ticket was deleted between the read and write sessions — treat as no-op.
        logger.warning(
            "summarize_ticket: ticket deleted before event write, skipping",
            extra={"ticket_id": ticket_id},
        )
        record_job("summarize_ticket", "not_found")
        return

    elapsed = round(time.monotonic() - start, 3)
    logger.info(
        "summarize_ticket: complete",
        extra={
            "ticket_id": ticket_id,
            "summary_len": len(summary),
            "elapsed_seconds": elapsed,
        },
    )
    record_job("summarize_ticket", "completed", start)


# ---------------------------------------------------------------------------
# Priority heuristics
# ---------------------------------------------------------------------------

_PRIORITY_RANK: dict[Priority, int] = {
    Priority.LOW: 0,
    Priority.MEDIUM: 1,
    Priority.HIGH: 2,
    Priority.CRITICAL: 3,
}


async def assign_priority(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Upgrade ticket priority based on the configured priority classifier.

    Only upgrades — never downgrades what the customer submitted. If the
    classifier proposes a lower or equal priority, the task is a no-op.
    Writes a PRIORITY_CHANGED event and updates Ticket.priority on upgrade.

    The classifier (rules or ML) is injected via ``ctx``; a classifier failure
    is non-fatal — it is logged and the task skips without writing an event.
    """
    async with _ticket_task(ctx, ticket_id, correlation_id, "assign_priority") as ctx_:
        if ctx_ is None:
            record_job("assign_priority", "not_found")
            return
        session, ticket, start = ctx_
        try:
            computed = await ctx["priority_classifier"].classify_priority(
                ticket.subject, ticket.description
            )
        except Exception:
            logger.exception(
                "assign_priority: classifier raised, skipping",
                extra={"ticket_id": ticket_id},
            )
            record_job("assign_priority", "failed", start)
            return

        if _PRIORITY_RANK[computed] <= _PRIORITY_RANK[ticket.priority]:
            logger.info(
                "assign_priority: no upgrade needed",
                extra={
                    "ticket_id": ticket_id,
                    "current": ticket.priority.value,
                    "computed": computed.value,
                },
            )
            record_job("assign_priority", "completed", start)
            return

        previous = ticket.priority
        ticket.priority = computed
        session.add(
            TicketEvent(
                ticket_id=ticket_id,
                event_type=EventType.PRIORITY_CHANGED,
                field_changed="priority",
                previous_value=previous.value,
                new_value=computed.value,
            )
        )

        try:
            await session.commit()
        except StaleDataError:
            await session.rollback()
            logger.warning(
                "assign_priority: concurrent update, skipping",
                extra={"ticket_id": ticket_id},
            )
            record_job("assign_priority", "skipped", start)
            return

        elapsed = round(time.monotonic() - start, 3)
        logger.info(
            "assign_priority: complete",
            extra={
                "ticket_id": ticket_id,
                "previous": previous.value,
                "new_priority": computed.value,
                "elapsed_seconds": elapsed,
            },
        )
        record_job("assign_priority", "completed", start)


# ---------------------------------------------------------------------------
# Spam detection
# ---------------------------------------------------------------------------


async def detect_spam(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Flag potential spam tickets using the configured spam classifier.

    Writes a SPAM_FLAGGED event when the classifier flags the content. No event
    is written for clean tickets. Ticket status is not changed — a human reviews
    flagged tickets. A classifier failure is non-fatal: it is logged and treated
    as not-flagged (no event).
    """
    async with _ticket_task(ctx, ticket_id, correlation_id, "detect_spam") as ctx_:
        if ctx_ is None:
            record_job("detect_spam", "not_found")
            return
        session, ticket, start = ctx_
        try:
            flagged = await ctx["spam_classifier"].classify_spam(
                ticket.subject, ticket.description
            )
        except Exception:
            logger.exception(
                "detect_spam: classifier raised, treating as not spam",
                extra={"ticket_id": ticket_id},
            )
            record_job("detect_spam", "failed", start)
            return

        if not flagged:
            logger.info("detect_spam: clean", extra={"ticket_id": ticket_id})
            record_job("detect_spam", "completed", start)
            return

        try:
            session.add(
                TicketEvent(
                    ticket_id=ticket_id,
                    event_type=EventType.SPAM_FLAGGED,
                    field_changed="spam",
                    new_value="true",
                )
            )
            await session.commit()
        except IntegrityError:
            logger.warning(
                "detect_spam: ticket deleted before event write, skipping",
                extra={"ticket_id": ticket_id},
            )
            record_job("detect_spam", "not_found")
            return

        elapsed = round(time.monotonic() - start, 3)
        logger.info(
            "detect_spam: flagged",
            extra={"ticket_id": ticket_id, "elapsed_seconds": elapsed},
        )
        record_job("detect_spam", "completed", start)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def route_ticket(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Route a ticket to a support department using the configured router.

    Writes a ROUTED event recording the destination department. The rules
    router maps category → department deterministically; the ML router uses the
    ticket text. A ROUTED event is normally always written; a classifier failure
    is non-fatal and skips the event.
    """
    async with _ticket_task(ctx, ticket_id, correlation_id, "route_ticket") as ctx_:
        if ctx_ is None:
            record_job("route_ticket", "not_found")
            return
        session, ticket, start = ctx_
        try:
            department = await ctx["routing_classifier"].classify_department(
                ticket.category, ticket.subject, ticket.description
            )
        except Exception:
            logger.exception(
                "route_ticket: classifier raised, skipping",
                extra={"ticket_id": ticket_id},
            )
            record_job("route_ticket", "failed", start)
            return

        try:
            session.add(
                TicketEvent(
                    ticket_id=ticket_id,
                    event_type=EventType.ROUTED,
                    field_changed="department",
                    new_value=department,
                )
            )
            await session.commit()
        except IntegrityError:
            logger.warning(
                "route_ticket: ticket deleted before event write, skipping",
                extra={"ticket_id": ticket_id},
            )
            record_job("route_ticket", "not_found")
            return

        elapsed = round(time.monotonic() - start, 3)
        logger.info(
            "route_ticket: complete",
            extra={
                "ticket_id": ticket_id,
                "department": department,
                "elapsed_seconds": elapsed,
            },
        )
        record_job("route_ticket", "completed", start)
