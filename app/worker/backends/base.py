"""SummarizerBackend protocol — the contract every backend must satisfy."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SummarizerBackend(Protocol):
    """Async summarization backend.

    Implementations must be safe to call from an arq worker coroutine.
    Sync-heavy work (e.g. HuggingFace pipeline inference) must be wrapped
    in ``asyncio.get_running_loop().run_in_executor`` inside ``summarize``.
    """

    async def summarize(self, text: str) -> str:
        """Return a summary of *text*. Never mutates state."""
        ...

    def is_available(self) -> bool:
        """Return True if this backend can be initialized on this host.

        Called before ``load_model`` — a False return causes the registry to
        fall through to the next backend without attempting initialization.
        """
        ...
