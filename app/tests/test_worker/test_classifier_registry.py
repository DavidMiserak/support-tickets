"""Tests for ClassifierRegistry initialization and fallback logic."""

import logging
from unittest.mock import patch

import pytest

import app.worker.classifier_registry as registry_module
from app.config import settings
from app.worker.backends.rules_classifier import (
    RulesPriorityClassifier,
    RulesRoutingClassifier,
    RulesSpamClassifier,
)
from app.worker.classifier_registry import Classifiers

pytestmark = pytest.mark.no_auto_schema


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the module-level singleton between tests."""
    registry_module._initialized_classifiers = None
    yield
    registry_module._initialized_classifiers = None


def _assert_rules_trio(classifiers: Classifiers) -> None:
    assert isinstance(classifiers["priority"], RulesPriorityClassifier)
    assert isinstance(classifiers["spam"], RulesSpamClassifier)
    assert isinstance(classifiers["routing"], RulesRoutingClassifier)


def test_registry_selects_rules_by_default() -> None:
    """The import-time default is the rules backend."""
    assert settings.classifier_backend == "rules"
    _assert_rules_trio(registry_module.initialize_classifiers())


def test_registry_warns_and_defaults_on_unknown_backend(monkeypatch, caplog):
    monkeypatch.setattr(settings, "classifier_backend", "magic")

    with caplog.at_level(logging.WARNING, logger="app.worker.classifier_registry"):
        classifiers = registry_module.initialize_classifiers()

    _assert_rules_trio(classifiers)
    assert "unknown CLASSIFIER_BACKEND" in caplog.text


def test_registry_falls_through_when_unavailable(monkeypatch):
    """zeroshot requested but ML deps missing → rules trio."""
    monkeypatch.setattr(settings, "classifier_backend", "zeroshot")

    with patch(
        "app.worker.backends.zeroshot_classifier.ZeroShotPipeline.is_available",
        return_value=False,
    ):
        classifiers = registry_module.initialize_classifiers()

    _assert_rules_trio(classifiers)


def test_registry_falls_through_when_load_model_fails(monkeypatch, caplog):
    monkeypatch.setattr(settings, "classifier_backend", "zeroshot")

    with (
        patch(
            "app.worker.backends.zeroshot_classifier.ZeroShotPipeline.is_available",
            return_value=True,
        ),
        patch(
            "app.worker.backends.zeroshot_classifier.ZeroShotPipeline.load_model",
            side_effect=OSError("model download failed"),
        ),
        caplog.at_level(logging.WARNING, logger="app.worker.classifier_registry"),
    ):
        classifiers = registry_module.initialize_classifiers()

    _assert_rules_trio(classifiers)
    assert "fell through from 'zeroshot' to 'rules'" in caplog.text


def test_registry_builds_zeroshot_when_available(monkeypatch):
    """zeroshot available + loads → zero-shot trio sharing one pipeline."""
    from app.worker.backends.zeroshot_classifier import (
        ZeroShotPriorityClassifier,
        ZeroShotRoutingClassifier,
        ZeroShotSpamClassifier,
    )

    monkeypatch.setattr(settings, "classifier_backend", "zeroshot")

    with (
        patch(
            "app.worker.backends.zeroshot_classifier.ZeroShotPipeline.is_available",
            return_value=True,
        ),
        patch(
            "app.worker.backends.zeroshot_classifier.ZeroShotPipeline.load_model",
            return_value=None,
        ),
    ):
        classifiers = registry_module.initialize_classifiers()

    assert isinstance(classifiers["priority"], ZeroShotPriorityClassifier)
    assert isinstance(classifiers["spam"], ZeroShotSpamClassifier)
    assert isinstance(classifiers["routing"], ZeroShotRoutingClassifier)
    # All three share the same loaded pipeline (model loaded once).
    assert (
        classifiers["priority"]._pipeline
        is classifiers["spam"]._pipeline
        is classifiers["routing"]._pipeline
    )


def test_get_initialized_classifiers_raises_before_init():
    with pytest.raises(RuntimeError, match="not initialized"):
        registry_module.get_initialized_classifiers()


def test_get_initialized_classifiers_returns_singleton(monkeypatch):
    monkeypatch.setattr(settings, "classifier_backend", "rules")
    classifiers = registry_module.initialize_classifiers()
    assert registry_module.get_initialized_classifiers() is classifiers
