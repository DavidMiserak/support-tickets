"""arq task definitions for the background worker."""

import logging
from typing import Any

from app.enums import EventType
from app.models import Ticket, TicketEvent

logger = logging.getLogger(__name__)


async def summarize_ticket(ctx: dict[str, Any], ticket_id: int) -> None:
    """Summarize a ticket's description and store the result as an audit event.

    The summarizer and session factory are injected via arq's ``ctx`` dict so
    tests can supply stubs without touching global state.

    Failure modes:
    - Ticket deleted before execution: no-op (logged, no event written).
    - Summarizer raises: logged, no event written; arq will not retry
      (max_tries=1 on this function).
    - DB commit fails: exception propagates, arq retries up to max_tries.
    """
    logger.info("summarize_ticket: started", extra={"ticket_id": ticket_id})

    summarizer = ctx["summarizer"]
    session_factory = ctx["session_factory"]

    # Fetch and summarize before opening a DB session — keeps the connection
    # free during CPU-bound inference (avoids pool exhaustion under load).
    async with session_factory() as session:
        ticket = await session.get(Ticket, ticket_id)

    if ticket is None:
        logger.warning(
            "summarize_ticket: ticket not found, skipping",
            extra={"ticket_id": ticket_id},
        )
        return

    description = ticket.description or ""
    if len(description.split()) < 5:
        logger.info(
            "summarize_ticket: description too short to summarize, skipping",
            extra={"ticket_id": ticket_id, "word_count": len(description.split())},
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

    async with session_factory() as session:
        event = TicketEvent(
            ticket_id=ticket_id,
            event_type=EventType.SUMMARIZED,
            new_value=summary,
        )
        session.add(event)
        await session.commit()

    logger.info(
        "summarize_ticket: complete",
        extra={"ticket_id": ticket_id, "summary_len": len(summary)},
    )
