"""Tests for the zero-shot ML classifiers.

The mapping tests stub the shared pipeline and assert label→domain mapping
without downloading or running a real model. The ``ml``-marked test at the
bottom exercises the real pipeline and is skipped in the default suite.
"""

import pytest

from app.enums import Category, Priority
from app.worker.backends.zeroshot_classifier import (
    ZeroShotPipeline,
    ZeroShotPriorityClassifier,
    ZeroShotRoutingClassifier,
    ZeroShotSpamClassifier,
)

pytestmark = pytest.mark.no_auto_schema


class _StubPipeline(ZeroShotPipeline):
    """Stand-in for ZeroShotPipeline returning canned {label: score} maps."""

    def __init__(self, scores: dict[str, float]) -> None:
        super().__init__()
        self._scores = scores

    def is_available(self) -> bool:
        return True

    async def classify(
        self, text: str, candidate_labels: list[str], *, multi_label: bool = False
    ) -> dict[str, float]:
        # Only return scores for the labels the caller asked about.
        return {label: self._scores[label] for label in candidate_labels}


# --- Import / availability guard ------------------------------------------


def test_module_imports_without_ml_deps() -> None:
    """Constructing the holder and probing availability must never raise.

    Guards the default image: torch/transformers imports must stay lazy.
    """
    holder = ZeroShotPipeline()
    assert isinstance(holder.is_available(), bool)


def test_classify_before_load_raises() -> None:
    holder = ZeroShotPipeline()
    with pytest.raises(RuntimeError, match="load_model"):
        # Run the coroutine to completion synchronously.
        import asyncio

        asyncio.run(holder.classify("hi", ["a", "b"]))


# --- Priority mapping -----------------------------------------------------


async def test_priority_maps_top_label_to_critical() -> None:
    stub = _StubPipeline(
        {
            "critical incident or service outage": 0.91,
            "high priority or urgent issue": 0.06,
            "routine or low priority request": 0.03,
        }
    )
    clf = ZeroShotPriorityClassifier(stub)
    assert await clf.classify_priority("s", "d") == Priority.CRITICAL


async def test_priority_floor_is_low() -> None:
    stub = _StubPipeline(
        {
            "critical incident or service outage": 0.10,
            "high priority or urgent issue": 0.20,
            "routine or low priority request": 0.70,
        }
    )
    clf = ZeroShotPriorityClassifier(stub)
    assert await clf.classify_priority("s", "d") == Priority.LOW


# --- Spam mapping ---------------------------------------------------------


async def test_spam_flagged_above_threshold() -> None:
    # Single-label softmax: the two scores sum to 1; spam wins.
    stub = _StubPipeline(
        {
            "spam or promotional message": 0.82,
            "legitimate customer support request": 0.18,
        }
    )
    clf = ZeroShotSpamClassifier(stub)
    assert await clf.classify_spam("s", "d") is True


async def test_spam_not_flagged_below_threshold() -> None:
    # A confident "legitimate" reading suppresses the spam score below 0.5.
    stub = _StubPipeline(
        {
            "spam or promotional message": 0.12,
            "legitimate customer support request": 0.88,
        }
    )
    clf = ZeroShotSpamClassifier(stub)
    assert await clf.classify_spam("s", "d") is False


# --- Routing mapping ------------------------------------------------------


async def test_routing_high_confidence_uses_ml() -> None:
    # Top label clears the floor → ML wins, overriding the stated category.
    stub = _StubPipeline(
        {
            "billing, payment, refund, or invoice": 0.05,
            "technical issue, bug, error, or outage": 0.80,
            "feature request or product suggestion": 0.10,
            "general inquiry or something else": 0.05,
        }
    )
    clf = ZeroShotRoutingClassifier(stub)
    # Category OTHER would route to "general" via rules; confident ML overrides.
    assert (
        await clf.classify_department(Category.OTHER, "s", "d") == "technical-support"
    )


async def test_routing_low_confidence_falls_back_to_category() -> None:
    # Top label is below the confidence floor → defer to the category rule.
    stub = _StubPipeline(
        {
            "billing, payment, refund, or invoice": 0.20,
            "technical issue, bug, error, or outage": 0.25,
            "feature request or product suggestion": 0.15,
            "general inquiry or something else": 0.40,  # top, but < 0.5 floor
        }
    )
    clf = ZeroShotRoutingClassifier(stub)
    # Zero-shot's top guess is "general", but low confidence → use category map,
    # which sends a TECHNICAL ticket to technical-support.
    assert (
        await clf.classify_department(Category.TECHNICAL, "s", "d")
        == "technical-support"
    )


# --- Optional real-model integration --------------------------------------


@pytest.fixture(scope="module")
def zeroshot_pipeline() -> ZeroShotPipeline:
    pipeline = ZeroShotPipeline()
    if not pipeline.is_available():
        pytest.skip("Install requirements-ml.txt to run ML tests (pytest -m ml)")
    pipeline.load_model()
    return pipeline


@pytest.mark.ml
async def test_zeroshot_priority_real_model(
    zeroshot_pipeline: ZeroShotPipeline,
) -> None:
    clf = ZeroShotPriorityClassifier(zeroshot_pipeline)
    result = await clf.classify_priority(
        "Production database outage",
        "All customers are getting 500 errors, the site is completely down.",
    )
    assert result in {Priority.HIGH, Priority.CRITICAL}
