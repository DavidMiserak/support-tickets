"""Zero-shot ML classifiers — one shared MNLI pipeline for all three tasks.

Uses HuggingFace ``pipeline("zero-shot-classification")``. Zero-shot needs no
labeled training data: it scores the ticket text against candidate labels via
natural-language-inference entailment. The same loaded pipeline serves priority,
spam, and routing by passing different ``candidate_labels`` per call, so the
model loads once.

All torch/transformers imports are lazy (inside ``is_available`` / ``load_model``
/ inference) so importing this module never requires the ML dependencies — the
default stack must run without them.
"""

import asyncio
import logging
import threading
from typing import Any, cast

from app.enums import Category, Priority
from app.worker.backends.rules_classifier import DEPARTMENTS, RulesRoutingClassifier

logger = logging.getLogger(__name__)

# Distilled MNLI model (~700 MB), matching the project's lightweight
# distilbart-cnn-6-6 choice. Override with CLASSIFIER_MODEL; e.g.
# facebook/bart-large-mnli (~1.6 GB) trades size for accuracy.
DEFAULT_MODEL_NAME = "valhalla/distilbart-mnli-12-3"


class ZeroShotPipeline:
    """Shared holder for the zero-shot pipeline with serialized inference.

    The HuggingFace pipeline object is not thread-safe, so inference is
    serialized with a process-wide lock (same discipline as
    ``TransformerSummarizer``). A single instance is shared by all three
    classifiers so the model is loaded only once per worker process.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME) -> None:
        self._model_name = model_name
        self._pipe: Any = None
        self._inference_lock = threading.Lock()

    def is_available(self) -> bool:
        """True only when both torch and transformers are importable."""
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401

            return True
        except ImportError:
            logger.warning(
                "ZeroShotPipeline unavailable: torch or transformers not installed. "
                "Install optional deps with: pip install -r requirements-ml.txt"
            )
            return False

    def load_model(self) -> None:
        """Load the zero-shot pipeline. Called once in arq on_startup.

        Blocks while weights load (cached: 1–5 s; cold: tens of seconds plus a
        one-time model download).
        """
        from transformers import pipeline

        logger.info("ZeroShotPipeline: loading %s (CPU)", self._model_name)
        self._pipe = pipeline(
            "zero-shot-classification",
            model=self._model_name,
            device=-1,  # CPU
        )
        logger.info("ZeroShotPipeline: model loaded")

    async def classify(
        self,
        text: str,
        candidate_labels: list[str],
        *,
        multi_label: bool = False,
    ) -> dict[str, float]:
        """Score *text* against *candidate_labels*; return {label: score}.

        Inference runs in a thread under the shared lock so the event loop stays
        unblocked and concurrent jobs do not corrupt pipeline state.
        """
        if self._pipe is None:
            raise RuntimeError("ZeroShotPipeline.load_model() was not called")

        loop = asyncio.get_running_loop()

        def _run_inference() -> dict[str, Any]:
            with self._inference_lock:
                return cast(
                    dict[str, Any],
                    self._pipe(
                        text,
                        candidate_labels=candidate_labels,
                        multi_label=multi_label,
                    ),
                )

        result = await loop.run_in_executor(None, _run_inference)
        return dict(zip(result["labels"], result["scores"], strict=True))


# ---------------------------------------------------------------------------
# Priority — candidate labels exclude MEDIUM to match the LOW/HIGH/CRITICAL floor
# ---------------------------------------------------------------------------

_PRIORITY_LABELS: dict[str, Priority] = {
    "critical incident or service outage": Priority.CRITICAL,
    "high priority or urgent issue": Priority.HIGH,
    "routine or low priority request": Priority.LOW,
}


class ZeroShotPriorityClassifier:
    """Maps the top zero-shot label to a priority floor."""

    def __init__(self, pipeline: ZeroShotPipeline) -> None:
        self._pipeline = pipeline

    def is_available(self) -> bool:
        return self._pipeline.is_available()

    async def classify_priority(self, subject: str, description: str) -> Priority:
        scores = await self._pipeline.classify(
            subject + ". " + description, list(_PRIORITY_LABELS)
        )
        top_label = max(scores, key=lambda label: scores[label])
        return _PRIORITY_LABELS[top_label]


# ---------------------------------------------------------------------------
# Spam — independent binary probability, thresholded
# ---------------------------------------------------------------------------

_SPAM_LABEL = "spam or promotional message"
_HAM_LABEL = "legitimate customer support request"
_SPAM_THRESHOLD = 0.5


class ZeroShotSpamClassifier:
    """Flags spam via single-label scoring of spam vs. legitimate.

    Single-label (softmax) weighs the two labels against each other, so the spam
    and ham scores sum to 1. This avoids the multi_label failure mode where a
    long but legitimate ticket scores high on the spam label in isolation; here
    a confident "legitimate" reading suppresses the spam score directly. With
    two labels, the 0.5 threshold means "spam must win", with headroom to raise
    it if false positives remain.
    """

    def __init__(self, pipeline: ZeroShotPipeline) -> None:
        self._pipeline = pipeline

    def is_available(self) -> bool:
        return self._pipeline.is_available()

    async def classify_spam(self, subject: str, description: str) -> bool:
        scores = await self._pipeline.classify(
            subject + ". " + description,
            [_SPAM_LABEL, _HAM_LABEL],
        )
        return scores[_SPAM_LABEL] > _SPAM_THRESHOLD


# ---------------------------------------------------------------------------
# Routing — top label maps back to a canonical department string
# ---------------------------------------------------------------------------

_DEPARTMENT_LABELS: dict[str, str] = {
    "billing, payment, refund, or invoice": "billing",
    "technical issue, bug, error, or outage": "technical-support",
    "feature request or product suggestion": "product",
    "general inquiry or something else": "general",
}

assert set(_DEPARTMENT_LABELS.values()) == set(DEPARTMENTS), (
    "Zero-shot routing labels must map onto the canonical department set "
    "from rules_classifier.DEPARTMENTS"
)

# Below this top-label score, defer to the deterministic category map rather
# than commit to a low-confidence guess (softmax over 4 labels; 0.25 = random).
_ROUTING_CONFIDENCE_FLOOR = 0.5


class ZeroShotRoutingClassifier:
    """Routes via zero-shot, falling back to the category rule when unsure.

    Zero-shot scoring over the department labels is unreliable on multi-topic
    tickets. When the top label's score is below the confidence floor, defer to
    the deterministic category→department map (the rules backend) instead of
    committing to a low-confidence guess. ML only overrides the category rule
    when it is confident.
    """

    def __init__(self, pipeline: ZeroShotPipeline) -> None:
        self._pipeline = pipeline
        self._rules = RulesRoutingClassifier()

    def is_available(self) -> bool:
        return self._pipeline.is_available()

    async def classify_department(
        self, category: Category, subject: str, description: str
    ) -> str:
        scores = await self._pipeline.classify(
            subject + ". " + description, list(_DEPARTMENT_LABELS)
        )
        top_label = max(scores, key=lambda label: scores[label])
        if scores[top_label] < _ROUTING_CONFIDENCE_FLOOR:
            return await self._rules.classify_department(category, subject, description)
        return _DEPARTMENT_LABELS[top_label]
