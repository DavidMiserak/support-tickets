"""Pytest configuration and fixtures."""

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database import Base, get_session
from app.main import app

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://ticketsupport:ticketsupport@localhost:5432/ticketsupport_test",
)


@pytest.fixture(autouse=True)
async def setup_test_db(request: pytest.FixtureRequest) -> AsyncGenerator[None, None]:
    """Create the schema before each test, drop it after for clean isolation.

    Tests marked ``no_auto_schema`` manage their own schema (e.g. Alembic
    migration tests) and opt out of this fixture.
    """
    if request.node.get_closest_marker("no_auto_schema") is not None:
        yield
        return

    engine = create_async_engine(TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture
async def test_db() -> AsyncGenerator[AsyncSession, None]:
    """Create a fresh database session for each test."""
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.fixture
async def async_client(test_db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create an async HTTP client wired to the test database session."""

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        yield test_db

    app.dependency_overrides[get_session] = override_get_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
