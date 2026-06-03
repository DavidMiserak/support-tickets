"""Classifier protocols — the contracts the priority/spam/routing backends satisfy.

Mirrors :class:`~app.worker.backends.base.SummarizerBackend`: each task has its
own classifier protocol with an ``is_available()`` gate so the registry can fall
through to a rules backend when an ML backend cannot initialize on this host.

Implementations must be safe to call from an arq worker coroutine. Sync-heavy
work (e.g. HuggingFace pipeline inference) must be wrapped in
``asyncio.get_running_loop().run_in_executor`` inside the classify method.
"""

from typing import Protocol, runtime_checkable

from app.enums import Category, Priority


@runtime_checkable
class PriorityClassifier(Protocol):
    """Proposes a priority *floor* for a ticket from its text."""

    async def classify_priority(self, subject: str, description: str) -> Priority:
        """Return the detected priority floor (LOW when no signal).

        The classifier only proposes; the ``assign_priority`` task enforces the
        upgrade-only rule, so returning LOW means "no change".
        """
        ...

    def is_available(self) -> bool:
        """Return True if this backend can be initialized on this host."""
        ...


@runtime_checkable
class SpamClassifier(Protocol):
    """Decides whether a ticket looks like spam."""

    async def classify_spam(self, subject: str, description: str) -> bool:
        """Return True when the content should be flagged for human review."""
        ...

    def is_available(self) -> bool:
        """Return True if this backend can be initialized on this host."""
        ...


@runtime_checkable
class RoutingClassifier(Protocol):
    """Maps a ticket to a destination department string."""

    async def classify_department(
        self, category: Category, subject: str, description: str
    ) -> str:
        """Return the destination department.

        ``category`` is included so the rules backend can use the deterministic
        category→department map; ML backends may use the text instead but must
        return one of the canonical department strings.
        """
        ...

    def is_available(self) -> bool:
        """Return True if this backend can be initialized on this host."""
        ...
