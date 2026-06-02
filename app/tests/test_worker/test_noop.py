"""Tests for NoopSummarizer."""

import pytest

from app.worker.backends.noop import NoopSummarizer


@pytest.mark.no_auto_schema
@pytest.mark.asyncio
async def test_noop_summarizer_returns_text_unchanged():
    summarizer = NoopSummarizer()
    text = (
        "Line one\n"
        "Line two with more than eighty characters so truncation would differ "
        "from the original if the backend mutated the input."
    )
    assert await summarizer.summarize(text) == text
