"""ClassifierRegistry — selects and initializes the priority/spam/routing backends.

Mirrors :mod:`app.worker.registry`: a module-level singleton initialized once in
arq ``on_startup`` and retrieved by tasks from the arq ``ctx``. ``rules`` is the
default and needs no ML dependencies; ``zeroshot`` loads a single shared MNLI
pipeline used by all three classifiers and falls through to ``rules`` if the ML
dependencies are missing or the model fails to load.

To override in tests, patch ``settings.classifier_backend`` (or
``app.worker.classifier_registry.settings``) before calling
``initialize_classifiers()``.
"""

import logging
from typing import TypedDict

from app.config import settings
from app.worker.backends.classifier_base import (
    PriorityClassifier,
    RoutingClassifier,
    SpamClassifier,
)
from app.worker.backends.rules_classifier import (
    RulesPriorityClassifier,
    RulesRoutingClassifier,
    RulesSpamClassifier,
)

logger = logging.getLogger(__name__)

_VALID_BACKENDS = ("rules", "zeroshot")


class Classifiers(TypedDict):
    """The trio of classifiers injected into the worker ctx."""

    priority: PriorityClassifier
    spam: SpamClassifier
    routing: RoutingClassifier


_initialized_classifiers: Classifiers | None = None


def _build_rules() -> Classifiers:
    return {
        "priority": RulesPriorityClassifier(),
        "spam": RulesSpamClassifier(),
        "routing": RulesRoutingClassifier(),
    }


def _try_build_zeroshot() -> Classifiers | None:
    """Build the zero-shot trio sharing one pipeline, or None on any failure."""
    # Imported lazily so the default (rules) path never imports the ML module's
    # transitive expectations.
    from app.worker.backends.zeroshot_classifier import (
        ZeroShotPipeline,
        ZeroShotPriorityClassifier,
        ZeroShotRoutingClassifier,
        ZeroShotSpamClassifier,
    )

    pipeline = ZeroShotPipeline(settings.classifier_model)
    if not pipeline.is_available():
        return None
    try:
        pipeline.load_model()
    except Exception:
        logger.exception(
            "ClassifierRegistry: load_model() failed for zeroshot, falling through"
        )
        return None

    return {
        "priority": ZeroShotPriorityClassifier(pipeline),
        "spam": ZeroShotSpamClassifier(pipeline),
        "routing": ZeroShotRoutingClassifier(pipeline),
    }


def initialize_classifiers() -> Classifiers:
    """Select, initialize, and cache the classifier trio for this worker process.

    Reads ``classifier_backend`` from :data:`~app.config.settings`. Falls through
    to the rules backend if ``zeroshot`` is requested but unavailable or fails to
    load. Always returns a working trio (at minimum the rules classifiers).
    """
    global _initialized_classifiers

    name = settings.classifier_backend

    if name not in _VALID_BACKENDS:
        logger.warning(
            "ClassifierRegistry: unknown CLASSIFIER_BACKEND=%r, valid values: %s. "
            "Defaulting to rules.",
            name,
            list(_VALID_BACKENDS),
        )
        name = "rules"

    classifiers: Classifiers | None = None
    if name == "zeroshot":
        classifiers = _try_build_zeroshot()
        if classifiers is None:
            logger.warning(
                "ClassifierRegistry: fell through from 'zeroshot' to 'rules'"
            )

    if classifiers is None:
        classifiers = _build_rules()

    logger.info(
        "ClassifierRegistry: initialized %s classifiers (requested: %s)",
        type(classifiers["priority"]).__name__,
        name,
    )
    _initialized_classifiers = classifiers
    return classifiers


def get_initialized_classifiers() -> Classifiers:
    """Return the singleton classifiers initialized in ``on_startup``.

    Raises RuntimeError if called before ``initialize_classifiers()``. This means
    ``on_startup`` did not complete — check worker startup logs for errors.
    """
    if _initialized_classifiers is None:
        raise RuntimeError(
            "ClassifierRegistry not initialized. "
            "on_startup did not complete — check worker startup logs for errors."
        )
    return _initialized_classifiers
