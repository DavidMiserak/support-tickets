"""Unit tests for AnthropicSummarizer (no real API calls)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.worker.backends.anthropic import AnthropicSummarizer

pytestmark = pytest.mark.no_auto_schema


async def test_summarize_returns_message_text() -> None:
    """summarize() returns msg.content[0].text from the stubbed client."""
    summarizer = AnthropicSummarizer()
    # Stub the client so no real API call is made.
    summarizer._client = MagicMock()
    summarizer._client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(text="A concise summary of the issue.")]
    )

    result = await summarizer.summarize("Customer cannot log in after a reset.")

    assert result == "A concise summary of the issue."
    kwargs = summarizer._client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5-20251001"
    assert kwargs["max_tokens"] == 150
    assert "Customer cannot log in" in kwargs["messages"][0]["content"]


async def test_summarize_before_load_raises() -> None:
    """Calling summarize() before load_model() raises a clear error."""
    summarizer = AnthropicSummarizer()
    with pytest.raises(RuntimeError, match="load_model"):
        await summarizer.summarize("text")


def test_is_available_false_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ANTHROPIC_API_KEY -> backend reports unavailable (registry falls back)."""
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    assert AnthropicSummarizer().is_available() is False
