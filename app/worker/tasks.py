"""arq task definitions for the background worker."""

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.enums import EventType
from app.models import Ticket, TicketEvent

logger = logging.getLogger(__name__)

_MIN_SUMMARY_WORDS = 5


async def summarize_ticket(ctx: dict[str, Any], ticket_id: int) -> None:
    """Summarize a ticket's description and store the result as an audit event.

    The summarizer and session factory are injected via arq's ``ctx`` dict so
    tests can supply stubs without touching global state.

    Failure modes:
    - Ticket deleted before execution: no-op (logged, no event written).
    - Summarizer raises: logged, no event written; no retry (max_tries=1 in WorkerSettings).
    - Ticket deleted between read and write sessions: IntegrityError caught, no-op.
    - DB commit fails for other reasons: exception propagates; no retry (max_tries=1).
    """
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
        logger.exception(
            "summarize_ticket: summarizer raised, skipping event write",
            extra={"ticket_id": ticket_id},
        )
        return

    try:
        async with session_factory() as session:
            event = TicketEvent(
                ticket_id=ticket_id,
                event_type=EventType.SUMMARIZED,
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

    logger.info(
        "summarize_ticket: complete",
        extra={"ticket_id": ticket_id, "summary_len": len(summary)},
    )
