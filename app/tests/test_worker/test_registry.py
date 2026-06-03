"""Tests for BackendRegistry initialization logic."""

import logging
from unittest.mock import patch

import pytest

import app.worker.registry as registry_module
from app.config import settings
from app.worker.backends.noop import NoopSummarizer


@pytest.fixture(autouse=True)
def reset_registry():
    """Reset the module-level singleton between tests."""
    registry_module._initialized_backend = None
    yield
    registry_module._initialized_backend = None


def test_registry_selects_noop_when_settings_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """settings.summarizer_backend='noop' selects NoopSummarizer."""
    monkeypatch.setattr(settings, "summarizer_backend", "noop")
    backend = registry_module.initialize_backend()
    assert isinstance(backend, NoopSummarizer)


def test_registry_uses_import_time_settings_default() -> None:
    """initialize_backend() reads the import-time settings singleton (default noop)."""
    assert settings.summarizer_backend == "noop"
    backend = registry_module.initialize_backend()
    assert isinstance(backend, NoopSummarizer)


def test_registry_warns_and_falls_through_on_unknown_backend(monkeypatch, caplog):
    """Unknown backend value logs a warning and defaults to NoopSummarizer."""
    monkeypatch.setattr(settings, "summarizer_backend", "anthropic")

    with caplog.at_level(logging.WARNING, logger="app.worker.registry"):
        backend = registry_module.initialize_backend()

    assert isinstance(backend, NoopSummarizer)
    assert "unknown SUMMARIZER_BACKEND" in caplog.text
    assert "Defaulting to noop" in caplog.text


def test_registry_falls_through_when_is_available_false(monkeypatch):
    """If is_available() returns False, the registry falls through to NoopSummarizer."""
    monkeypatch.setattr(settings, "summarizer_backend", "transformer")

    with patch(
        "app.worker.backends.transformer.TransformerSummarizer.is_available",
        return_value=False,
    ):
        backend = registry_module.initialize_backend()

    assert isinstance(backend, NoopSummarizer)


def test_registry_falls_through_when_load_model_fails(monkeypatch):
    """If load_model() raises, the registry falls through to NoopSummarizer."""
    monkeypatch.setattr(settings, "summarizer_backend", "transformer")

    with (
        patch(
            "app.worker.backends.transformer.TransformerSummarizer.is_available",
            return_value=True,
        ),
        patch(
            "app.worker.backends.transformer.TransformerSummarizer.load_model",
            side_effect=OSError("model download failed"),
        ),
    ):
        backend = registry_module.initialize_backend()

    assert isinstance(backend, NoopSummarizer)


def test_get_initialized_backend_raises_before_init():
    """Calling get_initialized_backend() before initialize_backend() raises."""
    with pytest.raises(RuntimeError, match="not initialized"):
        registry_module.get_initialized_backend()


def test_get_initialized_backend_returns_singleton(monkeypatch):
    """get_initialized_backend() returns the same instance after init."""
    monkeypatch.setattr(settings, "summarizer_backend", "noop")
    backend = registry_module.initialize_backend()
    assert registry_module.get_initialized_backend() is backend
