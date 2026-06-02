"""Pydantic request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.enums import Category, Priority, TicketStatus


class CreateTicketRequest(BaseModel):
    """Create ticket request."""

    customer_name: str = Field(..., min_length=1, max_length=255)
    customer_email: EmailStr
    subject: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1)
    priority: Priority = Field(default=Priority.MEDIUM)
    category: Category


class TicketResponse(BaseModel):
    """Ticket response."""

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


class ListTicketsResponse(BaseModel):
    """List tickets response."""

    items: list[TicketResponse]
    total: int
    skip: int
    limit: int
