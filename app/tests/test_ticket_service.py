"""Service-layer unit tests with a mocked repository (no database).

These cover the logic the service owns — the state machine, idempotent
no-ops, audit-event emission, and optimistic-lock translation — without
re-testing Pydantic validation or SQL.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.enums import EventType, TicketStatus
from app.errors import (
    AgentNotFoundError,
    ConcurrentUpdateError,
    InvalidStatusTransitionError,
    TicketNotFoundError,
)
from app.models import Ticket, TicketEvent
from app.repositories.ticket import TicketRepository
from app.services.ticket import ALLOWED_TRANSITIONS, TicketService


def test_every_status_is_a_transition_key() -> None:
    """Every TicketStatus must be a key in the map, so .get() never falls back
    to an empty set by accident and a newly added status can't be forgotten."""
    assert set(ALLOWED_TRANSITIONS) == set(TicketStatus)


def _service(
    ticket: Ticket | None, *, agent_exists: bool = True
) -> tuple[TicketService, MagicMock, AsyncMock]:
    repo = MagicMock(spec=TicketRepository)
    repo.get = AsyncMock(return_value=ticket)
    repo.get_agent = AsyncMock(return_value=MagicMock() if agent_exists else None)
    repo.add_event = MagicMock()
    session = AsyncMock(spec=AsyncSession)
    return TicketService(session, repo), repo, session


@pytest.mark.asyncio
async def test_update_status_legal_transition_writes_event() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN)
    service, repo, session = _service(ticket)

    await service.update_status(1, TicketStatus.IN_PROGRESS, actor_id=7)

    assert ticket.status == TicketStatus.IN_PROGRESS
    repo.add_event.assert_called_once()
    event: TicketEvent = repo.add_event.call_args.args[0]
    assert event.event_type == EventType.STATUS_CHANGED
    assert event.previous_value == "OPEN"
    assert event.new_value == "IN_PROGRESS"
    assert event.field_changed == "status"
    assert event.actor_id == 7
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_status_same_status_is_idempotent_noop() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN)
    service, repo, session = _service(ticket)

    result = await service.update_status(1, TicketStatus.OPEN)

    assert result is ticket
    repo.add_event.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_status_illegal_transition_raises() -> None:
    ticket = Ticket(id=1, status=TicketStatus.CLOSED)
    service, repo, session = _service(ticket)

    with pytest.raises(InvalidStatusTransitionError):
        await service.update_status(1, TicketStatus.OPEN)
    repo.add_event.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_status_unknown_ticket_raises_not_found() -> None:
    service, repo, session = _service(None)

    with pytest.raises(TicketNotFoundError):
        await service.update_status(999, TicketStatus.IN_PROGRESS)


@pytest.mark.asyncio
async def test_update_status_stale_data_becomes_concurrent_update() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN)
    service, repo, session = _service(ticket)
    session.commit.side_effect = StaleDataError("UPDATE", 1, 0)

    with pytest.raises(ConcurrentUpdateError):
        await service.update_status(1, TicketStatus.IN_PROGRESS)
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_is_terminal_for_every_target() -> None:
    for target in TicketStatus:
        if target == TicketStatus.CLOSED:
            continue
        ticket = Ticket(id=1, status=TicketStatus.CLOSED)
        service, _, _ = _service(ticket)
        with pytest.raises(InvalidStatusTransitionError):
            await service.update_status(1, target)


@pytest.mark.asyncio
async def test_assign_agent_writes_assigned_event() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN, assigned_agent_id=None)
    service, repo, session = _service(ticket)

    result = await service.assign_agent(1, 7)

    assert result.assigned_agent_id == 7
    repo.get_agent.assert_awaited_once_with(7)
    repo.add_event.assert_called_once()
    event: TicketEvent = repo.add_event.call_args.args[0]
    assert event.event_type == EventType.ASSIGNED
    assert event.field_changed == "assigned_agent_id"
    assert event.previous_value is None
    assert event.new_value == "7"
    assert event.actor_id == 7
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_assign_agent_same_agent_is_idempotent_noop() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN, assigned_agent_id=7)
    service, repo, session = _service(ticket)

    result = await service.assign_agent(1, 7)

    assert result is ticket
    repo.get_agent.assert_not_awaited()
    repo.add_event.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_agent_unknown_agent_raises() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN)
    service, repo, session = _service(ticket, agent_exists=False)

    with pytest.raises(AgentNotFoundError):
        await service.assign_agent(1, 99)
    repo.add_event.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_agent_unknown_ticket_raises_not_found() -> None:
    service, repo, session = _service(None)

    with pytest.raises(TicketNotFoundError):
        await service.assign_agent(999, 1)
    repo.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_assign_agent_stale_data_becomes_concurrent_update() -> None:
    ticket = Ticket(id=1, status=TicketStatus.OPEN)
    service, repo, session = _service(ticket)
    session.commit.side_effect = StaleDataError("UPDATE", 1, 0)

    with pytest.raises(ConcurrentUpdateError):
        await service.assign_agent(1, 3)
    session.rollback.assert_awaited_once()
