"""Ticket business logic.

The service owns the unit of work: it drives the repository, writes audit
events, and is the single place that commits. It also owns the status
state machine and translates an optimistic-lock clash into a domain error.
"""

import asyncio
import logging

from arq.connections import ArqRedis
from asgi_correlation_id import correlation_id
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.enums import Category, EventType, Priority, TicketStatus
from app.errors import (
    AgentNotFoundError,
    ConcurrentUpdateError,
    InvalidStatusTransitionError,
    TicketNotFoundError,
)
from app.metrics import (
    ticket_status_transitions_total,
    ticket_summarization_outcomes_total,
    ticket_worker_enqueue_outcomes_total,
    tickets_created_total,
)
from app.models import Ticket, TicketEvent
from app.repositories.ticket import TicketRepository
from app.schemas import MAX_TICKET_EVENTS_ON_DETAIL, CreateTicketRequest

logger = logging.getLogger(__name__)

# Allowed status moves. CLOSED is terminal; a ticket reopens via
# RESOLVED -> IN_PROGRESS. A status is never in its own set (same-status
# updates are handled as idempotent no-ops, not transitions).
ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.OPEN: {TicketStatus.IN_PROGRESS, TicketStatus.CLOSED},
    TicketStatus.IN_PROGRESS: {
        TicketStatus.RESOLVED,
        TicketStatus.OPEN,
        TicketStatus.CLOSED,
    },
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},
    TicketStatus.CLOSED: set(),
}


class TicketService:
    """Ticket operations: create, read, list, status transition."""

    def __init__(
        self,
        session: AsyncSession,
        repo: TicketRepository,
        arq_pool: ArqRedis | None = None,
    ) -> None:
        self.session = session
        self.repo = repo
        self._arq_pool = arq_pool

    async def create_ticket(self, req: CreateTicketRequest) -> Ticket:
        """Create a ticket (status OPEN) and record a CREATED audit event.

        Summarization policy: summarize-at-create, best-effort once. After
        commit, a single background job is enqueued (deduped by ticket id).
        Redis or worker failures are logged but do not fail the request, and
        failed jobs are not retried automatically.
        """
        ticket = Ticket(
            customer_name=req.customer_name,
            customer_email=req.customer_email,
            subject=req.subject,
            description=req.description,
            priority=req.priority,
            category=req.category,
        )
        await self.repo.add(ticket)  # flush -> ticket.id available
        self.repo.add_event(
            TicketEvent(
                ticket_id=ticket.id,
                event_type=EventType.CREATED,
                new_value=ticket.status.value,
            )
        )
        await self.session.commit()
        # Populate server-generated columns (created_at/updated_at/version_id)
        # on the returned instance, which the 201 response serializes.
        await self.session.refresh(ticket)

        tickets_created_total.inc()

        if self._arq_pool is None:
            ticket_summarization_outcomes_total.labels(outcome="skipped_no_pool").inc()
        else:
            try:
                # summarize-at-create, best-effort once: _job_id dedupes re-enqueue.
                # Pass the correlation ID explicitly so the worker can restore it
                # in its logging context (contextvars are not serialized to Redis).
                job = await self._arq_pool.enqueue_job(
                    "summarize_ticket",
                    ticket.id,
                    _job_id=f"summarize-{ticket.id}",
                    correlation_id=correlation_id.get(None),
                )
                if job is None:
                    logger.info(
                        "summarize_ticket already enqueued for ticket %d (deduped)",
                        ticket.id,
                    )
                    ticket_summarization_outcomes_total.labels(outcome="deduped").inc()
                else:
                    ticket_summarization_outcomes_total.labels(outcome="enqueued").inc()
            except Exception:
                logger.warning(
                    "Failed to enqueue summarization for ticket %d",
                    ticket.id,
                    exc_info=True,
                )
                ticket_summarization_outcomes_total.labels(
                    outcome="enqueue_failed"
                ).inc()

            _corr = correlation_id.get(None)
            _analysis = (
                ("assign_priority", "assign-priority"),
                ("detect_spam", "detect-spam"),
                ("route_ticket", "route"),
            )
            results = await asyncio.gather(
                *(
                    self._arq_pool.enqueue_job(
                        task,
                        ticket.id,
                        _job_id=f"{prefix}-{ticket.id}",
                        correlation_id=_corr,
                    )
                    for task, prefix in _analysis
                ),
                return_exceptions=True,
            )
            for (task, _), outcome in zip(_analysis, results):
                if isinstance(outcome, BaseException):
                    logger.warning(
                        "Failed to enqueue %s for ticket %d",
                        task,
                        ticket.id,
                        exc_info=outcome,
                    )
                    ticket_worker_enqueue_outcomes_total.labels(
                        task=task, outcome="enqueue_failed"
                    ).inc()
                elif outcome is None:
                    logger.info(
                        "%s already enqueued for ticket %d (deduped)",
                        task,
                        ticket.id,
                    )
                    ticket_worker_enqueue_outcomes_total.labels(
                        task=task, outcome="deduped"
                    ).inc()
                else:
                    ticket_worker_enqueue_outcomes_total.labels(
                        task=task, outcome="enqueued"
                    ).inc()

        return ticket

    async def get_ticket(self, ticket_id: int) -> Ticket:
        """Return a ticket or raise TicketNotFoundError. Events are not loaded."""
        ticket = await self.repo.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    async def get_ticket_detail(
        self, ticket_id: int
    ) -> tuple[Ticket, list[TicketEvent], int]:
        """Return a ticket, bounded events, and total event count."""
        ticket, events, events_total = await self.repo.get_with_events(
            ticket_id, event_limit=MAX_TICKET_EVENTS_ON_DETAIL
        )
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket, events, events_total

    async def list_tickets(
        self,
        *,
        status: TicketStatus | None = None,
        priority: Priority | None = None,
        category: Category | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """Return a filtered, paginated page plus the total count."""
        return await self.repo.list(
            status=status,
            priority=priority,
            category=category,
            skip=skip,
            limit=limit,
        )

    async def update_status(
        self,
        ticket_id: int,
        new_status: TicketStatus,
        actor_id: int | None = None,
    ) -> Ticket:
        """Transition a ticket's status, recording a STATUS_CHANGED event.

        Same-status requests are idempotent no-ops. Illegal moves raise
        InvalidStatusTransitionError; a concurrent write raises
        ConcurrentUpdateError (optimistic lock).
        """
        ticket = await self.get_ticket(ticket_id)

        if new_status == ticket.status:
            return ticket  # idempotent: no transition, no event

        allowed = ALLOWED_TRANSITIONS.get(ticket.status, set())
        if new_status not in allowed:
            raise InvalidStatusTransitionError(
                ticket_id, ticket.status, new_status, allowed
            )

        previous = ticket.status
        ticket.status = new_status
        self.repo.add_event(
            TicketEvent(
                ticket_id=ticket.id,
                event_type=EventType.STATUS_CHANGED,
                field_changed="status",
                previous_value=previous.value,
                new_value=new_status.value,
                actor_id=actor_id,
            )
        )
        try:
            await self.session.commit()
        except StaleDataError as exc:
            await self.session.rollback()
            raise ConcurrentUpdateError(ticket_id) from exc

        await self.session.refresh(ticket)
        ticket_status_transitions_total.labels(
            from_status=previous.value, to_status=new_status.value
        ).inc()
        return ticket

    async def assign_agent(
        self,
        ticket_id: int,
        agent_id: int,
        *,
        actor_id: int | None = None,
    ) -> Ticket:
        """Assign a ticket to an agent, recording an ASSIGNED audit event.

        Same-agent requests are idempotent no-ops. Unknown agents raise
        AgentNotFoundError; a concurrent write raises ConcurrentUpdateError.
        """
        ticket = await self.get_ticket(ticket_id)

        if ticket.assigned_agent_id == agent_id:
            return ticket

        agent = await self.repo.get_agent(agent_id)
        if agent is None:
            raise AgentNotFoundError(agent_id)

        previous = (
            str(ticket.assigned_agent_id)
            if ticket.assigned_agent_id is not None
            else None
        )
        ticket.assigned_agent_id = agent_id
        self.repo.add_event(
            TicketEvent(
                ticket_id=ticket.id,
                event_type=EventType.ASSIGNED,
                field_changed="assigned_agent_id",
                previous_value=previous,
                new_value=str(agent_id),
                actor_id=actor_id if actor_id is not None else agent_id,
            )
        )
        try:
            await self.session.commit()
        except StaleDataError as exc:
            await self.session.rollback()
            raise ConcurrentUpdateError(ticket_id) from exc

        await self.session.refresh(ticket)
        return ticket
