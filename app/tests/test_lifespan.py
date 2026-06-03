"""Lifespan startup/shutdown tests."""

import logging

import pytest

from app.arq_pool import get_arq_pool, set_arq_pool
from app.main import app, lifespan


@pytest.mark.asyncio
async def test_lifespan_starts_without_redis(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Redis failure at startup must not prevent the API from serving requests."""
    set_arq_pool(None)

    async def redis_down(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.main.create_pool", redis_down)

    with caplog.at_level(logging.ERROR, logger="app.main"):
        async with lifespan(app):
            assert get_arq_pool() is None

    assert get_arq_pool() is None
    assert "without background enqueue" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_redacts_redis_url_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Redis URL credentials are stripped before appearing in log lines."""
    set_arq_pool(None)

    monkeypatch.setattr(
        "app.main.settings.redis_url", "redis://:secret_password@redis:6379"
    )

    async def redis_down(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.main.create_pool", redis_down)

    with caplog.at_level(logging.ERROR, logger="app.main"):
        async with lifespan(app):
            pass

    assert "secret_password" not in caplog.text
    assert "redis:6379" in caplog.text
