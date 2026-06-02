"""Optimistic-locking and audit-atomicity tests against a real database.

These need more than one session/connection, so they build their own session
factory rather than using the single-session ``test_db`` fixture.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm.exc import StaleDataError

from app.enums import Category, EventType, TicketStatus
from app.models import Ticket, TicketEvent
from app.repositories.ticket import TicketRepository
from app.schemas import CreateTicketRequest
from app.services.ticket import TicketService
from app.tests.conftest import TEST_DATABASE_URL


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _create_request() -> CreateTicketRequest:
    return CreateTicketRequest(
        customer_name="Cust",
        customer_email="cust@example.com",
        subject="Subj",
        description="Desc",
        category=Category.OTHER,
    )


async def _seed_open_ticket(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as setup:
        service = TicketService(setup, TicketRepository(setup))
        ticket = await service.create_ticket(_create_request())
        return ticket.id


@pytest.mark.asyncio
async def test_version_id_raises_stale_data_on_concurrent_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """version_id_col makes a write against a stale row raise StaleDataError.

    Two sessions load the ticket at version 1 (independent objects, held by
    reference so neither is re-read). The first write bumps the version; the
    second targets the now-stale version and loses. The service turns this
    StaleDataError into a 409 (covered in test_ticket_service)."""
    ticket_id = await _seed_open_ticket(session_factory)

    async with session_factory() as s1, session_factory() as s2:
        t1 = await s1.get(Ticket, ticket_id)
        t2 = await s2.get(Ticket, ticket_id)
        assert t1 is not None and t2 is not None
        assert t1.version_id == t2.version_id == 1

        t1.status = TicketStatus.IN_PROGRESS
        await s1.commit()  # bumps version to 2 in the DB

        t2.status = TicketStatus.CLOSED  # still believes it's version 1
        with pytest.raises(StaleDataError):
            await s2.commit()


@pytest.mark.asyncio
async def test_audit_write_is_atomic_with_ticket_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the event insert fails, the status change rolls back too."""
    ticket_id = await _seed_open_ticket(session_factory)

    async with session_factory() as s:
        ticket = await s.get(Ticket, ticket_id)
        assert ticket is not None
        ticket.status = TicketStatus.IN_PROGRESS
        # Event pointing at a non-existent ticket -> FK violation on commit,
        # after the status mutation is already staged.
        s.add(TicketEvent(ticket_id=999_999, event_type=EventType.STATUS_CHANGED))
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()

    # From a fresh connection: neither change landed.
    async with session_factory() as s2:
        ticket = await s2.get(Ticket, ticket_id)
        assert ticket is not None
        assert ticket.status == TicketStatus.OPEN
        event_count = await s2.scalar(
            select(func.count())
            .select_from(TicketEvent)
            .where(TicketEvent.ticket_id == ticket_id)
        )
        # Only the CREATED event from seeding; no STATUS_CHANGED was written.
        assert event_count == 1
