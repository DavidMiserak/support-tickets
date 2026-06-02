"""Custom exceptions.

Each carries a stable ``error_type`` string (a closed vocabulary clients can
branch on) and its HTTP ``status_code``. The handlers in ``app.main`` turn these
into the ``{detail, error_type}`` envelope.
"""

from collections.abc import Iterable

from app.enums import TicketStatus


class TicketError(Exception):
    """Base exception for ticket operations."""

    error_type = "ticket_error"
    status_code = 400


class TicketNotFoundError(TicketError):
    """Ticket does not exist."""

    error_type = "ticket_not_found"
    status_code = 404

    def __init__(self, ticket_id: int) -> None:
        super().__init__(f"ticket {ticket_id} not found")


class InvalidStatusTransitionError(TicketError):
    """Status transition is not allowed."""

    error_type = "invalid_status_transition"
    status_code = 409

    def __init__(
        self,
        ticket_id: int,
        current: TicketStatus,
        attempted: TicketStatus,
        allowed: Iterable[TicketStatus],
    ) -> None:
        allowed_str = ", ".join(sorted(s.value for s in allowed)) or "(none)"
        super().__init__(
            f"cannot transition ticket {ticket_id} from {current.value} to "
            f"{attempted.value}; allowed: {allowed_str}"
        )


class ConcurrentUpdateError(TicketError):
    """A concurrent write modified the ticket first (optimistic-lock failure)."""

    error_type = "concurrent_update"
    status_code = 409

    def __init__(self, ticket_id: int) -> None:
        super().__init__(f"ticket {ticket_id} was modified by another request; retry")


class TicketValidationError(TicketError):
    """Validation failed.

    Named to avoid shadowing Pydantic's ``ValidationError``.
    """

    error_type = "validation_error"
    status_code = 422
