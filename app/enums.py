"""Domain enums."""

from enum import Enum


class TicketStatus(str, Enum):
    """Ticket status lifecycle."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class Priority(str, Enum):
    """Ticket priority levels."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Category(str, Enum):
    """Ticket categories."""

    BILLING = "BILLING"
    TECHNICAL = "TECHNICAL"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    OTHER = "OTHER"


class EventType(str, Enum):
    """Audit-trail event types.

    Stored as VARCHAR + CHECK (not a native PG enum) so values can be added with
    a plain migration instead of an ``ALTER TYPE``. ``ASSIGNED`` lands when the
    assign-agent endpoint does.
    """

    CREATED = "CREATED"
    STATUS_CHANGED = "STATUS_CHANGED"
    PRIORITY_CHANGED = "PRIORITY_CHANGED"
