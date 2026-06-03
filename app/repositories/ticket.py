"""Ticket data access.

The repository owns SQLAlchemy: it builds queries and stages writes with
``add`` / ``flush`` but never commits. The service owns the transaction.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.enums import Category, Priority, TicketStatus
from app.models import Ticket, TicketEvent


class TicketRepository:
    """CRUD + queries for tickets and their audit events."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, ticket: Ticket) -> Ticket:
        """Stage a new ticket and flush so its server-generated id is available."""
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    def add_event(self, event: TicketEvent) -> None:
        """Stage an audit event (no flush; committed with the parent change)."""
        self.session.add(event)

    async def get(self, ticket_id: int) -> Ticket | None:
        """Return a ticket by id with its events eager-loaded, or None."""
        result = await self.session.execute(
            select(Ticket)
            .options(selectinload(Ticket.events))
            .where(Ticket.id == ticket_id)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        status: TicketStatus | None = None,
        priority: Priority | None = None,
        category: Category | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[Ticket], int]:
        """Return a filtered page of tickets plus the post-filter total count."""
        filters = []
        if status is not None:
            filters.append(Ticket.status == status)
        if priority is not None:
            filters.append(Ticket.priority == priority)
        if category is not None:
            filters.append(Ticket.category == category)

        total = await self.session.scalar(
            select(func.count()).select_from(Ticket).where(*filters)
        )

        result = await self.session.execute(
            select(Ticket)
            .where(*filters)
            # id is the tiebreaker: created_at uses now() (the transaction
            # timestamp), so same-transaction rows tie and would otherwise
            # paginate unstably (skipped/duplicated rows across pages).
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all()), int(total or 0)
