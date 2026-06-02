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


class ValidationError(TicketError):
    """Validation failed."""

    pass
