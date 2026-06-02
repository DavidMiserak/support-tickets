"""Schema validation tests."""

import pytest
from pydantic import ValidationError

from app.enums import Category, Priority
from app.schemas import CreateTicketRequest


def test_create_ticket_request_defaults_priority_to_medium():
    """A request without an explicit priority defaults to MEDIUM."""
    req = CreateTicketRequest(
        customer_name="John Doe",
        customer_email="john@example.com",
        subject="Cannot login",
        description="I forgot my password",
        category=Category.TECHNICAL,
    )
    assert req.priority == Priority.MEDIUM


def test_create_ticket_request_rejects_invalid_email():
    """An invalid email is rejected by EmailStr validation."""
    with pytest.raises(ValidationError):
        CreateTicketRequest(
            customer_name="John Doe",
            customer_email="not-an-email",
            subject="Help",
            description="...",
            category=Category.OTHER,
        )


def test_create_ticket_request_rejects_empty_subject():
    """An empty subject fails the min_length constraint."""
    with pytest.raises(ValidationError):
        CreateTicketRequest(
            customer_name="John Doe",
            customer_email="john@example.com",
            subject="",
            description="Something is broken",
            category=Category.TECHNICAL,
        )
