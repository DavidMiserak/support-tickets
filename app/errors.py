"""Custom exceptions."""


class TicketError(Exception):
    """Base exception for ticket operations."""

    pass


class TicketNotFoundError(TicketError):
    """Ticket does not exist."""

    pass


class InvalidStatusTransitionError(TicketError):
    """Status transition is not allowed."""

    pass


class TicketValidationError(TicketError):
    """Validation failed.

    Named to avoid shadowing Pydantic's ``ValidationError``.
    """

    pass
