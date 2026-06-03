"""API integration tests for /tickets."""

import pytest
from httpx import AsyncClient

VALID_TICKET = {
    "customer_name": "Ada Lovelace",
    "customer_email": "ada@example.com",
    "subject": "Cannot reset password",
    "description": "The reset link 404s.",
    "category": "TECHNICAL",
}


async def _create(async_client: AsyncClient, **overrides: object) -> dict[str, object]:
    payload = {**VALID_TICKET, **overrides}
    resp = await async_client.post("/tickets", json=payload)
    assert resp.status_code == 201, resp.text
    data: dict[str, object] = resp.json()
    return data


@pytest.mark.asyncio
async def test_create_returns_201_with_populated_timestamps(async_client):
    body = await _create(async_client)
    assert body["id"] is not None
    assert body["status"] == "OPEN"
    # Server-default timestamps are present in the response (no 500).
    assert body["created_at"] is not None
    assert body["updated_at"] is not None


@pytest.mark.asyncio
async def test_create_accepts_case_insensitive_enums(async_client):
    body = await _create(async_client, category="technical", priority="high")
    assert body["category"] == "TECHNICAL"
    assert body["priority"] == "HIGH"


@pytest.mark.asyncio
async def test_create_oversized_description_returns_normalized_422(async_client):
    resp = await async_client.post(
        "/tickets", json={**VALID_TICKET, "description": "x" * 20_001}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert body["detail"] == "request validation failed"
    assert "errors" in body  # per-field detail preserved


@pytest.mark.asyncio
async def test_get_missing_ticket_returns_404_envelope(async_client):
    resp = await async_client.get("/tickets/999999")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {
        "detail": "ticket 999999 not found",
        "error_type": "ticket_not_found",
    }


@pytest.mark.asyncio
async def test_get_existing_ticket(async_client):
    created = await _create(async_client)
    resp = await async_client.get(f"/tickets/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_list_filters_and_pagination(async_client):
    for _ in range(3):
        await _create(async_client)
    resp = await async_client.get("/tickets", params={"status": "OPEN", "limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["skip"] == 0 and body["limit"] == 2


@pytest.mark.asyncio
async def test_list_rejects_out_of_range_limit(async_client):
    resp = await async_client.get("/tickets", params={"limit": 0})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_rejects_limit_too_large(async_client):
    resp = await async_client.get("/tickets", params={"limit": 101})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_rejects_negative_skip(async_client):
    resp = await async_client.get("/tickets", params={"skip": -1})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_enum_returns_422_envelope(async_client):
    resp = await async_client.post(
        "/tickets", json={**VALID_TICKET, "category": "BADVAL"}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_type"] == "validation_error"
    assert "errors" in body


@pytest.mark.asyncio
async def test_patch_invalid_status_returns_422_envelope(async_client):
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "BOGUS"}
    )
    assert resp.status_code == 422
    assert resp.json()["error_type"] == "validation_error"


@pytest.mark.asyncio
async def test_concurrent_update_returns_409(async_client, monkeypatch):
    """ConcurrentUpdateError from the service maps to 409 concurrent_update."""
    from app.errors import ConcurrentUpdateError
    from app.services.ticket import TicketService

    async def boom(*_args, **_kwargs):
        raise ConcurrentUpdateError(1)

    monkeypatch.setattr(TicketService, "update_status", boom)

    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "IN_PROGRESS"}
    )
    assert resp.status_code == 409
    assert resp.json()["error_type"] == "concurrent_update"


@pytest.mark.asyncio
async def test_status_transition_legal(async_client):
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "IN_PROGRESS"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_status_transition_illegal_returns_409(async_client):
    created = await _create(async_client)
    # OPEN -> RESOLVED is not allowed.
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "RESOLVED"}
    )
    assert resp.status_code == 409
    assert resp.json()["error_type"] == "invalid_status_transition"


@pytest.mark.asyncio
async def test_status_same_value_is_idempotent_200(async_client):
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "OPEN"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "OPEN"


@pytest.mark.asyncio
async def test_create_ticket_returns_201_when_enqueue_fails(
    async_client, stub_arq_pool
):
    """Redis failure during enqueue must not fail the ticket creation."""
    stub_arq_pool.enqueue_job.side_effect = ConnectionError("redis down")
    body = await _create(async_client)
    assert body["id"] is not None
    assert body["status"] == "OPEN"


@pytest.mark.asyncio
async def test_create_ticket_logs_when_enqueue_deduped(
    async_client, stub_arq_pool, caplog
):
    """When arq deduplicates a job (returns None), a log line is emitted."""
    import logging

    stub_arq_pool.enqueue_job.return_value = None
    with caplog.at_level(logging.INFO, logger="app.services.ticket"):
        await _create(async_client)
    assert "deduped" in caplog.text


@pytest.mark.asyncio
async def test_get_ticket_includes_created_event(async_client):
    """GET /tickets/{id} returns an events list with a CREATED entry."""
    created = await _create(async_client)
    resp = await async_client.get(f"/tickets/{created['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert len(body["events"]) >= 1
    assert body["events"][0]["event_type"] == "CREATED"


@pytest.mark.asyncio
async def test_get_ticket_events_include_status_change(async_client):
    """GET /tickets/{id} reflects a STATUS_CHANGED event after a PATCH."""
    created = await _create(async_client)
    await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "IN_PROGRESS"}
    )
    resp = await async_client.get(f"/tickets/{created['id']}")
    event_types = [e["event_type"] for e in resp.json()["events"]]
    assert "CREATED" in event_types
    assert "STATUS_CHANGED" in event_types
    # CREATED must come before STATUS_CHANGED (ordered by created_at).
    assert event_types.index("CREATED") < event_types.index("STATUS_CHANGED")


@pytest.mark.asyncio
async def test_list_tickets_items_have_no_events_field(async_client):
    """GET /tickets list items must not expose the events field."""
    await _create(async_client)
    resp = await async_client.get("/tickets")
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert "events" not in item


@pytest.mark.asyncio
async def test_get_ticket_invalid_id_returns_422(async_client):
    """Path(ge=1) rejects id=0 and negative ids with 422."""
    for bad_id in (0, -1):
        resp = await async_client.get(f"/tickets/{bad_id}")
        assert resp.status_code == 422, f"expected 422 for id={bad_id}"


@pytest.mark.asyncio
async def test_update_status_response_has_no_events_field(async_client):
    """PATCH /tickets/{id}/status returns TicketResponse (no events field)."""
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "IN_PROGRESS"}
    )
    assert resp.status_code == 200
    assert "events" not in resp.json()
