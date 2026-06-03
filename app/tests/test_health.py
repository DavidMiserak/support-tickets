"""Health and root endpoint tests."""

from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(async_client: AsyncClient) -> None:
    """Test /health returns 200 with ok status for all components."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


@pytest.mark.asyncio
async def test_health_returns_503_when_database_unavailable(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test /health returns 503 when the database probe fails."""

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr("app.main.check_database_connection", unavailable)

    response = await async_client.get("/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "unavailable"
    assert data["redis"] == "ok"


@pytest.mark.asyncio
async def test_health_returns_503_when_redis_unavailable(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test /health returns 503 when the arq pool is None (Redis down at startup)."""
    from app.arq_pool import get_arq_pool
    from app.main import app

    # DB probe returns True so this test is isolated to the Redis path.
    async def db_ok() -> bool:
        return True

    monkeypatch.setattr("app.main.check_database_connection", db_ok)

    # Override the stub pool with None (no Redis at startup)
    app.dependency_overrides[get_arq_pool] = lambda: None

    try:
        response = await async_client.get("/health")
    finally:
        mock_pool = AsyncMock()
        app.dependency_overrides[get_arq_pool] = lambda: mock_pool

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "ok"
    assert data["redis"] == "unavailable"


@pytest.mark.asyncio
async def test_health_returns_503_when_redis_ping_fails(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test /health returns 503 when Redis ping raises an exception."""
    from app.arq_pool import get_arq_pool
    from app.main import app

    async def db_ok() -> bool:
        return True

    monkeypatch.setattr("app.main.check_database_connection", db_ok)

    failing_pool = AsyncMock()
    failing_pool.ping.side_effect = ConnectionRefusedError("redis is down")
    app.dependency_overrides[get_arq_pool] = lambda: failing_pool

    try:
        response = await async_client.get("/health")
    finally:
        mock_pool = AsyncMock()
        app.dependency_overrides[get_arq_pool] = lambda: mock_pool

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["redis"] == "unavailable"


@pytest.mark.asyncio
async def test_health_includes_request_id_header(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health endpoint echoes a valid UUID4 X-Request-ID back when provided."""

    async def db_ok() -> bool:
        return True

    monkeypatch.setattr("app.main.check_database_connection", db_ok)

    response = await async_client.get(
        "/health", headers={"X-Request-ID": "550e8400-e29b-41d4-a716-446655440000"}
    )
    assert response.status_code == 200
    assert (
        response.headers.get("x-request-id") == "550e8400-e29b-41d4-a716-446655440000"
    )


@pytest.mark.asyncio
async def test_health_generates_request_id_header(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Health endpoint generates an X-Request-ID when none is supplied."""

    async def db_ok() -> bool:
        return True

    monkeypatch.setattr("app.main.check_database_connection", db_ok)

    response = await async_client.get("/health")
    assert response.status_code == 200
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) > 0


@pytest.mark.asyncio
async def test_root_redirects_to_docs(async_client: AsyncClient) -> None:
    """Root path redirects to the interactive API docs."""
    response = await async_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
