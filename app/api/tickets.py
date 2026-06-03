"""/tickets endpoints."""

from typing import Annotated, Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.arq_pool import get_arq_pool
from app.config import settings
from app.database import get_session
from app.enums import Category, Priority, TicketStatus
from app.models import Ticket, TicketEvent
from app.rate_limit import limiter
from app.repositories.ticket import TicketRepository
from app.schemas import (
    AssignAgentRequest,
    CreateTicketRequest,
    ErrorEnvelope,
    ListTicketsResponse,
    TicketDetailResponse,
    TicketEventResponse,
    TicketResponse,
    UpdateStatusRequest,
    ValidationErrorEnvelope,
)
from app.services.ticket import TicketService

router = APIRouter(prefix="/tickets", tags=["tickets"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorEnvelope}}
_CONFLICT: dict[int | str, dict[str, Any]] = {409: {"model": ErrorEnvelope}}
_UNPROCESSABLE: dict[int | str, dict[str, Any]] = {
    422: {"model": ValidationErrorEnvelope}
}


async def get_ticket_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    arq_pool: Annotated[ArqRedis | None, Depends(get_arq_pool)],
) -> TicketService:
    """Build the ticket service for a request (route -> service -> repo)."""
    return TicketService(session, TicketRepository(session), arq_pool)


ServiceDep = Annotated[TicketService, Depends(get_ticket_service)]


def _ticket_detail_response(
    ticket: Ticket,
    events: list[TicketEvent],
    events_total: int,
) -> TicketDetailResponse:
    """Build a detail response from a ticket row and a preloaded event slice."""
    return TicketDetailResponse(
        **TicketResponse.model_validate(ticket).model_dump(),
        events=[TicketEventResponse.model_validate(e) for e in events],
        events_total=events_total,
        events_truncated=events_total > len(events),
    )


@router.post(
    "",
    response_model=TicketResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses={**_UNPROCESSABLE, 429: {"model": ErrorEnvelope}},
)
@limiter.limit(lambda: settings.rate_limit_create_ticket)  # type: ignore[untyped-decorator]
async def create_ticket(
    request: Request, req: CreateTicketRequest, service: ServiceDep
) -> TicketResponse:
    """Create a ticket.

    Rate-limited to ``RATE_LIMIT_CREATE_TICKET`` per IP (default 20/minute).
    """
    ticket = await service.create_ticket(req)
    return TicketResponse.model_validate(ticket)


@router.get(
    "/{ticket_id}",
    response_model=TicketDetailResponse,
    responses={**_NOT_FOUND, **_UNPROCESSABLE},
)
async def get_ticket(
    ticket_id: Annotated[int, Path(ge=1)], service: ServiceDep
) -> TicketDetailResponse:
    """Fetch a single ticket by id, including a bounded audit event history."""
    ticket, events, events_total = await service.get_ticket_detail(ticket_id)
    return _ticket_detail_response(ticket, events, events_total)


@router.get(
    "",
    response_model=ListTicketsResponse,
    responses=_UNPROCESSABLE,
)
async def list_tickets(
    service: ServiceDep,
    status: TicketStatus | None = None,
    priority: Priority | None = None,
    category: Category | None = None,
    q: Annotated[str | None, Query(max_length=500)] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ListTicketsResponse:
    """List tickets with optional filters, full-text search, and pagination.

    ``q`` searches subject and description using PostgreSQL full-text search
    (``websearch_to_tsquery``). Supports quoted phrases and ``-`` exclusions.
    Combines freely with status/priority/category filters.
    """
    search = q.strip() or None if q is not None else None
    tickets, total = await service.list_tickets(
        status=status,
        priority=priority,
        category=category,
        search=search,
        skip=skip,
        limit=limit,
    )
    return ListTicketsResponse(
        items=[TicketResponse.model_validate(t) for t in tickets],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.patch(
    "/{ticket_id}/status",
    response_model=TicketDetailResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNPROCESSABLE},
)
async def update_status(
    ticket_id: Annotated[int, Path(ge=1)],
    req: UpdateStatusRequest,
    service: ServiceDep,
) -> TicketDetailResponse:
    """Transition a ticket to a new status.

    Returns the same detail shape as GET /tickets/{id}, including bounded
    audit events (reload runs even for idempotent no-op transitions).
    """
    await service.update_status(ticket_id, req.status)
    ticket, events, events_total = await service.get_ticket_detail(ticket_id)
    return _ticket_detail_response(ticket, events, events_total)


@router.patch(
    "/{ticket_id}/assign",
    response_model=TicketDetailResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNPROCESSABLE},
)
async def assign_agent(
    ticket_id: Annotated[int, Path(ge=1)],
    req: AssignAgentRequest,
    service: ServiceDep,
) -> TicketDetailResponse:
    """Assign a ticket to a support agent.

    Returns the same detail shape as GET /tickets/{id}, including bounded
    audit events (reload runs even when the agent is already assigned).
    """
    await service.assign_agent(ticket_id, req.agent_id)
    ticket, events, events_total = await service.get_ticket_detail(ticket_id)
    return _ticket_detail_response(ticket, events, events_total)
