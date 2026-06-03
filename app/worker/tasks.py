"""arq task definitions for the background worker."""

import logging
import time
from typing import Any

from asgi_correlation_id import correlation_id as _correlation_id_var
from sqlalchemy.exc import IntegrityError

from app.enums import EventType
from app.models import Ticket, TicketEvent

logger = logging.getLogger(__name__)

_MIN_SUMMARY_WORDS = 5


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
    request_id as the originating API request.

    Failure modes:
    - Ticket deleted before execution: no-op (logged, no event written).
    - Summarizer raises: logged, no event written; no retry (max_tries=1 in WorkerSettings).
    - Ticket deleted between read and write sessions: IntegrityError caught, no-op.
    - DB commit fails for other reasons: exception propagates; no retry (max_tries=1).
    """
    if correlation_id is not None:
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
