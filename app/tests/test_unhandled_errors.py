"""Catch-all exception handler tests."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_unhandled_exception_returns_uniform_500_envelope(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unexpected failures use the shared envelope and hide internals."""

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("super secret database password")

    monkeypatch.setattr(
        "app.services.ticket.TicketService.get_ticket_detail",
        boom,
    )

    resp = await async_client.get("/tickets/1")
    assert resp.status_code == 500
    assert resp.json() == {
        "detail": "internal server error",
        "error_type": "internal_server_error",
    }
    assert "secret" not in resp.text
