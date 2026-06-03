"""Health and root endpoint tests."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(async_client: AsyncClient) -> None:
    """Test /health returns 200 when the database is reachable."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


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
    assert response.json() == {"status": "unavailable", "database": "down"}


@pytest.mark.asyncio
async def test_root_redirects_to_docs(async_client: AsyncClient) -> None:
    """Root path redirects to the interactive API docs."""
    response = await async_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
