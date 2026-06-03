"""Pydantic request/response schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.enums import Category, EventType, Priority, TicketStatus

# Generous upper bound so a client can't POST a multi-MB body into the
# unbounded Text column, while still allowing long descriptions.
MAX_DESCRIPTION_LENGTH = 20_000


class CreateTicketRequest(BaseModel):
    """Create ticket request."""

    customer_name: str = Field(..., min_length=1, max_length=255)
    customer_email: EmailStr
    subject: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    priority: Priority = Field(default=Priority.MEDIUM)
    category: Category

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "customer_name": "Ada Lovelace",
                    "customer_email": "ada@example.com",
                    "subject": "Cannot reset my password",
                    "description": "The reset link 404s after I click it.",
                    "priority": "HIGH",
                    "category": "TECHNICAL",
                }
            ]
        }
    )


class UpdateStatusRequest(BaseModel):
    """Update ticket status request."""

    status: TicketStatus

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "IN_PROGRESS"}]}
    )


class TicketEventResponse(BaseModel):
    """Single audit event on a ticket."""

    id: int
    event_type: EventType
    field_changed: str | None
    previous_value: str | None
    new_value: str | None
    actor_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TicketResponse(BaseModel):
    """Ticket response (used by list and write endpoints)."""

    id: int
    customer_name: str
    customer_email: str
    subject: str
    description: str
    status: TicketStatus
    priority: Priority
    category: Category
    assigned_agent_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TicketDetailResponse(TicketResponse):
    """Ticket detail response — includes the full audit event history.

    Used by GET /tickets/{id}. Events are ordered by created_at ascending so
    the CREATED event is always first and worker-written events follow in order.
    Not included in list responses to avoid N+1 queries on the collection.
    """

    events: list[TicketEventResponse] = []


class ListTicketsResponse(BaseModel):
    """List tickets response."""

    items: list[TicketResponse]
    total: int
    skip: int
    limit: int


class ErrorEnvelope(BaseModel):
    """Uniform error body for every non-2xx response."""

    detail: str
    error_type: str

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"detail": "ticket 42 not found", "error_type": "ticket_not_found"}
            ]
        }
    )


class ValidationErrorEnvelope(ErrorEnvelope):
    """422 body: the common envelope plus Pydantic's per-field error detail."""

    errors: list[dict[str, Any]]

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "detail": "request validation failed",
                    "error_type": "validation_error",
                    "errors": [
                        {
                            "type": "string_too_long",
                            "loc": ["body", "description"],
                            "msg": "String should have at most 20000 characters",
                        }
                    ],
                }
            ]
        }
    )
