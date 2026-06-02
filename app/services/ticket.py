"""Ticket business logic.

The service owns the unit of work: it drives the repository, writes audit
events, and is the single place that commits. It also owns the status
state machine and translates an optimistic-lock clash into a domain error.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.enums import Category, EventType, Priority, TicketStatus
from app.errors import (
    ConcurrentUpdateError,
    InvalidStatusTransitionError,
    TicketNotFoundError,
)
from app.models import Ticket, TicketEvent
from app.repositories.ticket import TicketRepository
from app.schemas import CreateTicketRequest

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

    def __init__(self, session: AsyncSession, repo: TicketRepository) -> None:
        self.session = session
        self.repo = repo

    async def create_ticket(self, req: CreateTicketRequest) -> Ticket:
        """Create a ticket (status OPEN) and record a CREATED audit event."""
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
        return ticket

    async def get_ticket(self, ticket_id: int) -> Ticket:
        """Return a ticket or raise TicketNotFoundError."""
        ticket = await self.repo.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

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
        return ticket
