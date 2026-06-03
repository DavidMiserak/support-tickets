"""AnthropicSummarizer — summarize tickets via the Anthropic Messages API.

Runs the synchronous Anthropic SDK call wrapped in run_in_executor so it does
not block the arq event loop (same pattern as TransformerSummarizer). The client
is created once at worker startup via ``load_model()``.

The ``anthropic`` package is imported lazily (inside ``is_available`` /
``load_model`` / ``summarize``) so importing this module never requires the
optional dependency — the default stack must run without it.
"""

import asyncio
import logging
from typing import Any, cast

from app.config import settings

logger = logging.getLogger(__name__)

_MODEL_NAME = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 150
_PROMPT = (
    "Summarize this customer support ticket in one or two sentences, focusing on "
    "the core issue and any requested action. Respond with only the summary.\n\n"
    "{text}"
)


class AnthropicSummarizer:
    """Summarizer backed by the Anthropic Messages API (Claude Haiku)."""

    def __init__(self) -> None:
        self._client: Any = None

    def is_available(self) -> bool:
        """True only when an API key is set and the anthropic SDK is importable.

        Returning False lets the registry fall through to the next backend
        (ultimately NoopSummarizer) rather than failing at startup.
        """
        if settings.anthropic_api_key is None:
            logger.warning(
                "AnthropicSummarizer unavailable: ANTHROPIC_API_KEY is not set"
            )
            return False
        try:
            import anthropic  # noqa: F401

            return True
        except ImportError:
            logger.warning(
                "AnthropicSummarizer unavailable: anthropic not installed. "
                "Install optional deps with: pip install -r requirements-optional.txt"
            )
            return False

    def load_model(self) -> None:
        """Create the Anthropic client. Called once in arq on_startup.

        No network call — the client is constructed lazily and connects on first
        request.
        """
        from anthropic import Anthropic

        self._client = Anthropic(api_key=settings.anthropic_api_key)
        logger.info("AnthropicSummarizer: client initialized (%s)", _MODEL_NAME)

    async def summarize(self, text: str) -> str:
        """Summarize *text* via a Claude Haiku message, off the event loop."""
        if self._client is None:
            raise RuntimeError("AnthropicSummarizer.load_model() was not called")

        loop = asyncio.get_running_loop()

        def _call() -> str:
            message = self._client.messages.create(
                model=_MODEL_NAME,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": _PROMPT.format(text=text)}],
            )
            return cast(str, message.content[0].text)

        return await loop.run_in_executor(None, _call)
