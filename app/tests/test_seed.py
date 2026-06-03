"""Tests for scripts.seed — idempotent agent and ticket seeding."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import scripts.seed as seed_module
from app.enums import EventType, TicketStatus
from app.models import Agent, Ticket, TicketEvent
from app.tests.conftest import TEST_DATABASE_URL


@pytest.fixture
async def seed_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Point seed helpers at the isolated test database."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(seed_module, "async_session_factory", factory)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_agents_creates_all_defaults(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await seed_module.seed_agents()

    assert created == len(seed_module.DEFAULT_AGENTS)

    async with seed_session_factory() as session:
        emails = (
            await session.scalars(select(Agent.email).order_by(Agent.email))
        ).all()

    assert emails == sorted(email for _, email in seed_module.DEFAULT_AGENTS)


@pytest.mark.asyncio
async def test_seed_agents_is_idempotent(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await seed_module.seed_agents() == len(seed_module.DEFAULT_AGENTS)
    assert await seed_module.seed_agents() == 0

    async with seed_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Agent))

    assert count == len(seed_module.DEFAULT_AGENTS)


@pytest.mark.asyncio
async def test_seed_agents_skips_existing_agents(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with seed_session_factory() as session:
        session.add(Agent(name="Ada Support", email="ada@support.example.com"))
        await session.commit()

    created = await seed_module.seed_agents()

    assert created == len(seed_module.DEFAULT_AGENTS) - 1

    async with seed_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Agent))

    assert count == len(seed_module.DEFAULT_AGENTS)


@pytest.mark.asyncio
async def test_seed_tickets_creates_all_defaults(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_module.seed_agents()
    created = await seed_module.seed_tickets()

    assert created == len(seed_module.DEFAULT_TICKETS)

    async with seed_session_factory() as session:
        tickets = (await session.scalars(select(Ticket).order_by(Ticket.id))).all()

    assert len(tickets) == len(seed_module.DEFAULT_TICKETS)
    statuses = {t.status for t in tickets}
    assert statuses == {
        TicketStatus.OPEN,
        TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED,
        TicketStatus.CLOSED,
    }


@pytest.mark.asyncio
async def test_seed_tickets_is_idempotent(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_module.seed_agents()
    assert await seed_module.seed_tickets() == len(seed_module.DEFAULT_TICKETS)
    assert await seed_module.seed_tickets() == 0

    async with seed_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(Ticket))

    assert count == len(seed_module.DEFAULT_TICKETS)


@pytest.mark.asyncio
async def test_seed_tickets_writes_audit_trail(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_module.seed_agents()
    await seed_module.seed_tickets()

    async with seed_session_factory() as session:
        ticket = await session.scalar(
            select(Ticket).where(Ticket.subject == "Production API outage — urgent")
        )
        assert ticket is not None
        assert ticket.assigned_agent_id is not None

        event_types = (
            await session.scalars(
                select(TicketEvent.event_type)
                .where(TicketEvent.ticket_id == ticket.id)
                .order_by(TicketEvent.id)
            )
        ).all()

    assert EventType.CREATED in event_types
    assert EventType.STATUS_CHANGED in event_types
    assert EventType.SUMMARIZED in event_types
    assert EventType.ROUTED in event_types


@pytest.mark.asyncio
async def test_run_seed_is_idempotent(
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await seed_module.run_seed()
    second = await seed_module.run_seed()

    assert first == (
        len(seed_module.DEFAULT_AGENTS),
        len(seed_module.DEFAULT_TICKETS),
    )
    assert second == (0, 0)
