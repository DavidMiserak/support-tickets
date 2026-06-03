"""Health and root endpoint tests."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from asgi_correlation_id.middleware import is_valid_uuid4
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch check_database_connection to return True (no live DB needed)."""

    async def _db_ok() -> bool:
        return True

    monkeypatch.setattr("app.main.check_database_connection", _db_ok)


# ---------------------------------------------------------------------------
# /health — liveness probe (pure 200, no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_liveness_returns_200(async_client: AsyncClient) -> None:
    """Liveness probe is unconditionally 200 with no dependency checks."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_liveness_no_component_keys(async_client: AsyncClient) -> None:
    """Liveness response never includes database or redis keys."""
    response = await async_client.get("/health")
    data = response.json()
    assert "database" not in data
    assert "redis" not in data


@pytest.mark.asyncio
async def test_health_includes_request_id_header(
    async_client: AsyncClient,
) -> None:
    """Health endpoint echoes a valid UUID4 X-Request-ID back when provided."""
    response = await async_client.get(
        "/health", headers={"X-Request-ID": "550e8400-e29b-41d4-a716-446655440000"}
    )
    assert response.status_code == 200
    assert (
        response.headers.get("x-request-id") == "550e8400-e29b-41d4-a716-446655440000"
    )


@pytest.mark.asyncio
async def test_health_generates_request_id_header(
    async_client: AsyncClient,
) -> None:
    """Health endpoint generates a hyphenated UUID4 X-Request-ID when none supplied."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert "x-request-id" in response.headers
    returned_id = response.headers["x-request-id"]
    assert is_valid_uuid4(
        returned_id
    ), f"generated ID is not valid UUID4: {returned_id!r}"
    assert (
        "-" in returned_id
    ), f"generated ID should be hyphenated, got: {returned_id!r}"


@pytest.mark.asyncio
async def test_health_rejects_invalid_request_id(
    async_client: AsyncClient,
) -> None:
    """Non-UUID X-Request-ID is rejected; middleware generates a fresh UUID."""
    response = await async_client.get("/health", headers={"X-Request-ID": "not-a-uuid"})
    assert response.status_code == 200
    returned_id = response.headers.get("x-request-id", "")
    assert returned_id != "not-a-uuid"
    assert is_valid_uuid4(returned_id)


# ---------------------------------------------------------------------------
# /ready — readiness probe (Postgres + Redis, with timeouts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_check(async_client: AsyncClient, mock_db_ok: None) -> None:
    """Readiness probe returns 200 when both Postgres and Redis are healthy."""
    response = await async_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


@pytest.mark.asyncio
async def test_ready_returns_503_when_database_unavailable(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe returns 503 when the database probe fails."""

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr("app.main.check_database_connection", unavailable)

    response = await async_client.get("/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "unavailable"
    assert data["redis"] == "ok"


@pytest.mark.asyncio
async def test_ready_returns_503_when_redis_unavailable(
    async_client: AsyncClient, mock_db_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe returns 503 when the arq pool is None (Redis down at startup)."""
    from app.arq_pool import get_arq_pool
    from app.main import app

    app.dependency_overrides[get_arq_pool] = lambda: None

    try:
        response = await async_client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_arq_pool, None)

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "ok"
    assert data["redis"] == "unavailable"


@pytest.mark.asyncio
async def test_ready_returns_503_when_redis_ping_fails(
    async_client: AsyncClient, mock_db_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe returns 503 when Redis ping raises an exception."""
    from app.arq_pool import get_arq_pool
    from app.main import app

    failing_pool = AsyncMock()
    failing_pool.ping.side_effect = ConnectionRefusedError("redis is down")
    app.dependency_overrides[get_arq_pool] = lambda: failing_pool

    try:
        response = await async_client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_arq_pool, None)

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["redis"] == "unavailable"


@pytest.mark.asyncio
async def test_ready_returns_503_when_both_unavailable(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe returns 503 with both components unavailable."""
    from app.arq_pool import get_arq_pool
    from app.main import app

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr("app.main.check_database_connection", unavailable)
    app.dependency_overrides[get_arq_pool] = lambda: None

    try:
        response = await async_client.get("/ready")
    finally:
        app.dependency_overrides.pop(get_arq_pool, None)

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "unavailable"
    assert data["redis"] == "unavailable"


@pytest.mark.asyncio
async def test_ready_db_timeout(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe returns 503 and marks database unavailable on timeout.

    The mock raises asyncio.TimeoutError directly rather than sleeping past
    the probe deadline — both paths reach the same except asyncio.TimeoutError
    handler in ready(), so this correctly tests the error-handling branch without
    the 2-second wall-clock wait.
    """

    async def db_timeout() -> bool:
        raise asyncio.TimeoutError()

    monkeypatch.setattr("app.main.check_database_connection", db_timeout)

    response = await async_client.get("/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "unavailable"
    assert data["redis"] == "ok"


@pytest.mark.asyncio
async def test_ready_redis_timeout(
    async_client: AsyncClient, mock_db_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readiness probe returns 503 and marks redis unavailable on timeout.

    See test_ready_db_timeout for rationale on the direct-raise pattern.
    """

    async def redis_timeout(pool: object) -> bool:
        raise asyncio.TimeoutError()

    monkeypatch.setattr("app.main._check_redis", redis_timeout)

    response = await async_client.get("/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    assert data["database"] == "ok"
    assert data["redis"] == "unavailable"


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_redirects_to_docs(async_client: AsyncClient) -> None:
    """Root path redirects to the interactive API docs."""
    response = await async_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
