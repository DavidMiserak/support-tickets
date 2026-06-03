"""Optional integration tests for TransformerSummarizer (DistilBART).

Requires ``requirements-ml.txt``. Skipped in the default suite via the ``ml``
marker; run explicitly with: ``pytest -m ml``
"""

import pytest

from app.worker.backends.transformer import TransformerSummarizer
from app.worker.ml_sample_text import ML_SAMPLE_TICKET_DESCRIPTION


@pytest.fixture(scope="module")
def transformer_summarizer() -> TransformerSummarizer:
    """Load DistilBART once per module; skip the whole module if ML deps missing."""
    summarizer = TransformerSummarizer()
    if not summarizer.is_available():
        pytest.skip("Install requirements-ml.txt to run ML tests (pytest -m ml)")
    summarizer.load_model()
    return summarizer


@pytest.mark.ml
@pytest.mark.no_auto_schema
@pytest.mark.asyncio
async def test_transformer_summarizer_produces_shorter_summary(
    transformer_summarizer: TransformerSummarizer,
) -> None:
    """DistilBART returns a non-empty summary shorter than the source text."""
    summary = await transformer_summarizer.summarize(ML_SAMPLE_TICKET_DESCRIPTION)

    assert summary.strip()
    assert summary != ML_SAMPLE_TICKET_DESCRIPTION
    assert len(summary) < len(ML_SAMPLE_TICKET_DESCRIPTION)
    # Long input should compress meaningfully, not just trim whitespace.
    assert len(summary) <= int(len(ML_SAMPLE_TICKET_DESCRIPTION) * 0.75)
