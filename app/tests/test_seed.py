"""Tests for scripts.seed — idempotent agent seeding."""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import scripts.seed as seed_module
from app.models import Agent
from app.tests.conftest import TEST_DATABASE_URL


@pytest.fixture
async def seed_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Point seed_agents at the isolated test database."""
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
