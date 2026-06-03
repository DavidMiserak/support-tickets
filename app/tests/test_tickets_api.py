"""API integration tests for /tickets."""

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import EventType
from app.models import TicketEvent
from app.repositories.ticket import TicketRepository
from app.schemas import MAX_TICKET_EVENTS_ON_DETAIL

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
    # Move to CLOSED (terminal), then verify no further transitions are allowed.
    await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "CLOSED"}
    )
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "OPEN"}
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
    assert body["events_total"] == len(body["events"])
    assert body["events_truncated"] is False


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
async def test_update_status_response_includes_events(async_client):
    """PATCH /tickets/{id}/status returns TicketDetailResponse with audit events."""
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "IN_PROGRESS"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    event_types = [e["event_type"] for e in body["events"]]
    assert "CREATED" in event_types
    assert "STATUS_CHANGED" in event_types


@pytest.mark.asyncio
async def test_update_status_noop_still_returns_events(async_client):
    """Idempotent status PATCH still returns bounded events (same shape as GET)."""
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/status", json={"status": "OPEN"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) >= 1
    assert body["events"][0]["event_type"] == "CREATED"


@pytest.mark.asyncio
async def test_assign_agent_returns_200(
    async_client: AsyncClient, test_agent: int
) -> None:
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/assign", json={"agent_id": test_agent}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["assigned_agent_id"] == test_agent
    assert "events" in body
    event_types = [e["event_type"] for e in body["events"]]
    assert "ASSIGNED" in event_types


@pytest.mark.asyncio
async def test_assign_agent_missing_agent_returns_404(
    async_client: AsyncClient,
) -> None:
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/assign", json={"agent_id": 999_999}
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": "agent 999999 not found",
        "error_type": "agent_not_found",
    }


@pytest.mark.asyncio
async def test_assign_agent_missing_ticket_returns_404(
    async_client: AsyncClient, test_agent: int
) -> None:
    resp = await async_client.patch(
        "/tickets/999999/assign", json={"agent_id": test_agent}
    )
    assert resp.status_code == 404
    assert resp.json()["error_type"] == "ticket_not_found"


@pytest.mark.asyncio
async def test_assign_agent_idempotent_same_agent(
    async_client: AsyncClient, test_agent: int
) -> None:
    created = await _create(async_client)
    url = f"/tickets/{created['id']}/assign"
    first = await async_client.patch(url, json={"agent_id": test_agent})
    second = await async_client.patch(url, json={"agent_id": test_agent})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["assigned_agent_id"] == test_agent


@pytest.mark.asyncio
async def test_assign_agent_invalid_agent_id_returns_422(
    async_client: AsyncClient,
) -> None:
    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/assign", json={"agent_id": 0}
    )
    assert resp.status_code == 422
    assert resp.json()["error_type"] == "validation_error"


@pytest.mark.asyncio
async def test_get_ticket_includes_assigned_event(
    async_client: AsyncClient, test_agent: int
) -> None:
    created = await _create(async_client)
    await async_client.patch(
        f"/tickets/{created['id']}/assign", json={"agent_id": test_agent}
    )
    resp = await async_client.get(f"/tickets/{created['id']}")
    event_types = [e["event_type"] for e in resp.json()["events"]]
    assert "ASSIGNED" in event_types


@pytest.mark.asyncio
async def test_assign_agent_concurrent_update_returns_409(
    async_client: AsyncClient, test_agent: int, monkeypatch: MonkeyPatch
) -> None:
    from app.errors import ConcurrentUpdateError
    from app.services.ticket import TicketService

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise ConcurrentUpdateError(1)

    monkeypatch.setattr(TicketService, "assign_agent", boom)

    created = await _create(async_client)
    resp = await async_client.patch(
        f"/tickets/{created['id']}/assign", json={"agent_id": test_agent}
    )
    assert resp.status_code == 409
    assert resp.json()["error_type"] == "concurrent_update"


@pytest.mark.asyncio
async def test_get_ticket_events_truncated_when_over_limit(
    async_client: AsyncClient, test_db: AsyncSession
) -> None:
    created = await _create(async_client)
    raw_id = created["id"]
    assert isinstance(raw_id, int)
    ticket_id = raw_id
    repo = TicketRepository(test_db)
    for _ in range(MAX_TICKET_EVENTS_ON_DETAIL + 3):
        repo.add_event(
            TicketEvent(
                ticket_id=ticket_id,
                event_type=EventType.STATUS_CHANGED,
                field_changed="status",
                previous_value="OPEN",
                new_value="IN_PROGRESS",
            )
        )
    await test_db.commit()

    resp = await async_client.get(f"/tickets/{ticket_id}")
    body = resp.json()
    assert body["events_total"] == MAX_TICKET_EVENTS_ON_DETAIL + 4  # CREATED + extras
    assert len(body["events"]) == MAX_TICKET_EVENTS_ON_DETAIL
    assert body["events_truncated"] is True


# ---------------------------------------------------------------------------
# Full-text search (?q=)
# ---------------------------------------------------------------------------


async def _create_with(async_client, **overrides):
    """Create a ticket with field overrides; return the parsed body."""
    payload = {**VALID_TICKET, **overrides}
    resp = await async_client.post("/tickets", json=payload)
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
async def test_search_finds_match_in_subject(async_client):
    await _create_with(async_client, subject="Password reset broken", description="x")
    await _create_with(async_client, subject="Billing question", description="x")
    resp = await async_client.get("/tickets?q=password+reset")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["subject"] == "Password reset broken"


@pytest.mark.asyncio
async def test_search_finds_match_in_description(async_client):
    await _create_with(async_client, subject="Login", description="Two-factor fails.")
    await _create_with(async_client, subject="Other", description="Unrelated content.")
    resp = await async_client.get("/tickets?q=two-factor")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_search_no_match_returns_empty(async_client):
    await _create(async_client)
    resp = await async_client.get("/tickets?q=xyznonexistentterm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_search_is_case_insensitive(async_client):
    await _create_with(async_client, subject="Critical Outage Alert", description="x")
    resp = await async_client.get("/tickets?q=OUTAGE")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_search_combines_with_status_filter(async_client):
    await _create_with(async_client, subject="API outage", description="x")
    resp = await async_client.get("/tickets?q=outage&status=OPEN")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    resp = await async_client.get("/tickets?q=outage&status=CLOSED")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_search_blank_q_returns_all(async_client):
    await _create(async_client)
    await _create(async_client)
    resp_all = await async_client.get("/tickets")
    resp_blank = await async_client.get("/tickets?q=   ")
    assert resp_blank.status_code == 200
    assert resp_blank.json()["total"] == resp_all.json()["total"]


@pytest.mark.asyncio
async def test_search_q_too_long_returns_422(async_client):
    resp = await async_client.get(f"/tickets?q={'x' * 501}")  # noqa: E501
    assert resp.status_code == 422
    assert resp.json()["error_type"] == "validation_error"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_standard_envelope(async_client, monkeypatch):
    """POST /tickets enforces the per-IP rate limit and returns the standard
    {detail, error_type} envelope on 429."""
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "rate_limit_create_ticket", "2/minute")

    # First two requests succeed (limit is 2/minute).
    for _ in range(2):
        resp = await async_client.post("/tickets", json=VALID_TICKET)
        assert resp.status_code == 201

    # Third request is blocked; verify the standard error envelope.
    resp = await async_client.post("/tickets", json=VALID_TICKET)
    assert resp.status_code == 429
    body = resp.json()
    assert body["error_type"] == "rate_limit_exceeded"
    assert "detail" in body
