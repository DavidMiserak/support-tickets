"""Health and root endpoint tests."""

import pytest


@pytest.mark.asyncio
async def test_health_check(async_client):
    """Test /health endpoint returns 200."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_root_redirects_to_docs(async_client):
    """Root path redirects to the interactive API docs."""
    response = await async_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
