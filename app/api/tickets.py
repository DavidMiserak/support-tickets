"""/tickets endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.arq_pool import get_arq_pool
from app.database import get_session
from app.enums import Category, Priority, TicketStatus
from app.repositories.ticket import TicketRepository
from app.schemas import (
    CreateTicketRequest,
    ErrorEnvelope,
    ListTicketsResponse,
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
    arq_pool: Annotated[Any, Depends(get_arq_pool)] = None,
) -> TicketService:
    """Build the ticket service for a request (route -> service -> repo)."""
    return TicketService(session, TicketRepository(session), arq_pool)


ServiceDep = Annotated[TicketService, Depends(get_ticket_service)]


@router.post(
    "",
    response_model=TicketResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses=_UNPROCESSABLE,
)
async def create_ticket(
    req: CreateTicketRequest, service: ServiceDep
) -> TicketResponse:
    """Create a ticket."""
    ticket = await service.create_ticket(req)
    return TicketResponse.model_validate(ticket)


@router.get(
    "/{ticket_id}",
    response_model=TicketResponse,
    responses={**_NOT_FOUND, **_UNPROCESSABLE},
)
async def get_ticket(ticket_id: int, service: ServiceDep) -> TicketResponse:
    """Fetch a single ticket by id."""
    ticket = await service.get_ticket(ticket_id)
    return TicketResponse.model_validate(ticket)


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
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ListTicketsResponse:
    """List tickets with optional filters and pagination."""
    tickets, total = await service.list_tickets(
        status=status, priority=priority, category=category, skip=skip, limit=limit
    )
    return ListTicketsResponse(
        items=[TicketResponse.model_validate(t) for t in tickets],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.patch(
    "/{ticket_id}/status",
    response_model=TicketResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNPROCESSABLE},
)
async def update_status(
    ticket_id: int, req: UpdateStatusRequest, service: ServiceDep
) -> TicketResponse:
    """Transition a ticket to a new status."""
    ticket = await service.update_status(ticket_id, req.status)
    return TicketResponse.model_validate(ticket)
