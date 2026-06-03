"""arq task definitions for the background worker."""

import logging
import re
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from asgi_correlation_id import correlation_id as _correlation_id_var
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.enums import Category, EventType, Priority
from app.models import Ticket, TicketEvent

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
        return

    description = ticket.description or ""
    words = description.split()
    if len(words) < _MIN_SUMMARY_WORDS:
        logger.info(
            "summarize_ticket: description too short to summarize, skipping",
            extra={"ticket_id": ticket_id, "word_count": len(words)},
        )
        return

    try:
        summary = await summarizer.summarize(description)
    except Exception:
        elapsed = round(time.monotonic() - start, 3)
        logger.exception(
            "summarize_ticket: summarizer raised, skipping event write",
            extra={"ticket_id": ticket_id, "elapsed_seconds": elapsed},
        )
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


# ---------------------------------------------------------------------------
# Priority heuristics
# ---------------------------------------------------------------------------

_PRIORITY_RANK: dict[Priority, int] = {
    Priority.LOW: 0,
    Priority.MEDIUM: 1,
    Priority.HIGH: 2,
    Priority.CRITICAL: 3,
}

_CRITICAL_KEYWORDS = (
    "outage",
    "down",
    "critical",
    "emergency",
    "urgent",
    "production",
)
_HIGH_KEYWORDS = (
    "asap",
    "important",
    "major",
    "severe",
    "degraded",
    "performance",
)

# Precompiled word-boundary patterns prevent false positives from substrings:
# "down" won't match "download"/"markdown"; "production" won't match inside
# "nonproduction". Patterns are built once at import time.
_CRITICAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _CRITICAL_KEYWORDS) + r")\b"
)
_HIGH_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in _HIGH_KEYWORDS) + r")\b"
)


def _detect_priority(subject: str, description: str) -> Priority:
    text = (subject + " " + description).lower()
    if _CRITICAL_RE.search(text):
        return Priority.CRITICAL
    if _HIGH_RE.search(text):
        return Priority.HIGH
    return Priority.MEDIUM


async def assign_priority(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Upgrade ticket priority based on subject/description keyword heuristics.

    Only upgrades — never downgrades what the customer submitted. If the
    heuristic suggests a lower or equal priority, the task is a no-op.
    Writes a PRIORITY_CHANGED event and updates Ticket.priority on upgrade.
    """
    async with _ticket_task(ctx, ticket_id, correlation_id, "assign_priority") as ctx_:
        if ctx_ is None:
            return
        session, ticket, start = ctx_
        computed = _detect_priority(ticket.subject, ticket.description)

        if _PRIORITY_RANK[computed] <= _PRIORITY_RANK[ticket.priority]:
            logger.info(
                "assign_priority: no upgrade needed",
                extra={
                    "ticket_id": ticket_id,
                    "current": ticket.priority.value,
                    "computed": computed.value,
                },
            )
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


# ---------------------------------------------------------------------------
# Spam detection heuristics
# ---------------------------------------------------------------------------

_SPAM_PHRASES = (
    "click here",
    "free offer",
    "you have won",
    "congratulations",
    "act now",
    "limited time",
    "buy now",
    "make money",
)
_MAX_URLS = 2


def _is_spam(subject: str, description: str) -> bool:
    text = (subject + " " + description).lower()
    url_count = text.count("http://") + text.count("https://")
    if url_count > _MAX_URLS:
        return True
    return any(phrase in text for phrase in _SPAM_PHRASES)


async def detect_spam(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Flag potential spam tickets based on content heuristics.

    Writes a SPAM_FLAGGED event when the ticket content triggers one or more
    spam signals (known phrases, excessive URLs). No event is written for
    clean tickets. Ticket status is not changed — a human reviews flagged
    tickets.
    """
    async with _ticket_task(ctx, ticket_id, correlation_id, "detect_spam") as ctx_:
        if ctx_ is None:
            return
        session, ticket, start = ctx_
        flagged = _is_spam(ticket.subject, ticket.description)

        if not flagged:
            logger.info("detect_spam: clean", extra={"ticket_id": ticket_id})
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
            return

        elapsed = round(time.monotonic() - start, 3)
        logger.info(
            "detect_spam: flagged",
            extra={"ticket_id": ticket_id, "elapsed_seconds": elapsed},
        )


# ---------------------------------------------------------------------------
# Routing heuristics
# ---------------------------------------------------------------------------

_DEPARTMENT: dict[Category, str] = {
    Category.BILLING: "billing",
    Category.TECHNICAL: "technical-support",
    Category.FEATURE_REQUEST: "product",
    Category.OTHER: "general",
}


async def route_ticket(
    ctx: dict[str, Any],
    ticket_id: int,
    correlation_id: str | None = None,
) -> None:
    """Route a ticket to the appropriate support department based on category.

    Writes a ROUTED event recording the destination department. The mapping
    is deterministic: category → department string. A ROUTED event is always
    written (every ticket has a category).
    """
    async with _ticket_task(ctx, ticket_id, correlation_id, "route_ticket") as ctx_:
        if ctx_ is None:
            return
        session, ticket, start = ctx_
        department = _DEPARTMENT[ticket.category]

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
