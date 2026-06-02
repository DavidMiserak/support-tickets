"""Domain enums."""

from enum import Enum


class CaseInsensitiveStrEnum(str, Enum):
    """A string enum that accepts any casing on input.

    Lets clients send ``"open"`` or ``"Open"`` for ``OPEN``; values always
    serialize back in canonical (upper) casing.
    """

    @classmethod
    def _missing_(cls, value: object) -> "CaseInsensitiveStrEnum | None":
        if isinstance(value, str):
            upper = value.upper()
            for member in cls:
                if member.value == upper:
                    return member
        return None


class TicketStatus(CaseInsensitiveStrEnum):
    """Ticket status lifecycle."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class Priority(CaseInsensitiveStrEnum):
    """Ticket priority levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Category(CaseInsensitiveStrEnum):
    """Ticket categories."""

    BILLING = "BILLING"
    TECHNICAL = "TECHNICAL"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    OTHER = "OTHER"


class EventType(str, Enum):
    """Audit-trail event types.

    Stored as VARCHAR + CHECK (not a native PG enum) so values can be added with
    a plain migration instead of an ``ALTER TYPE``.
    """

    CREATED = "CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    PRIORITY_CHANGED = "PRIORITY_CHANGED"
    ASSIGNED = "ASSIGNED"
    SUMMARIZED = "SUMMARIZED"
