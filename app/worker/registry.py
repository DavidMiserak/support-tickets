"""BackendRegistry — selects and initializes the summarizer backend.

The registry is a module-level singleton: ``initialize_backend()`` is called
once in arq ``on_startup`` and stores the result. Worker tasks call
``get_initialized_backend()`` to retrieve it from the module state.

To override in tests, set ``SUMMARIZER_BACKEND=noop`` in the environment
before importing this module, or patch ``_initialized_backend`` directly.
"""

import logging
import os

from app.worker.backends.base import SummarizerBackend
from app.worker.backends.noop import NoopSummarizer
from app.worker.backends.transformer import TransformerSummarizer

logger = logging.getLogger(__name__)

_BACKENDS: dict[str, type] = {
    "transformer": TransformerSummarizer,
    "noop": NoopSummarizer,
    # Phase 4: "anthropic": AnthropicSummarizer,
}

# Order used when the requested backend is unavailable.
_FALLBACK_ORDER = ["transformer", "noop"]

assert set(_FALLBACK_ORDER) == set(
    _BACKENDS
), "FALLBACK_ORDER and BACKENDS are out of sync — add new backends to both"

_initialized_backend: SummarizerBackend | None = None


def _try_init(cls: type, name: str) -> SummarizerBackend | None:
    """Instantiate *cls*, call load_model if present, return instance or None."""
    backend: SummarizerBackend = cls()
    if not backend.is_available():
        return None
    if hasattr(backend, "load_model"):
        try:
            backend.load_model()
        except Exception:
            logger.exception(
                "BackendRegistry: load_model() failed for %s, falling through", name
            )
            return None
    return backend


def initialize_backend() -> SummarizerBackend:
    """Select, initialize, and cache the backend for this worker process.

    Reads ``SUMMARIZER_BACKEND`` from the environment. Falls through to the
    next entry in ``_FALLBACK_ORDER`` if the requested backend is unavailable
    or fails to load. Always returns a working backend (at minimum NoopSummarizer).
    """
    global _initialized_backend

    name = os.getenv("SUMMARIZER_BACKEND", "noop")

    if name not in _BACKENDS:
        logger.warning(
            "BackendRegistry: unknown SUMMARIZER_BACKEND=%r, valid values: %s. "
            "Falling through to first available.",
            name,
            list(_BACKENDS),
        )

    cls = _BACKENDS.get(name, NoopSummarizer)
    backend = _try_init(cls, name)

    if backend is None:
        for fallback_name in _FALLBACK_ORDER:
            if fallback_name == name:
                continue  # already tried this one
            backend = _try_init(_BACKENDS[fallback_name], fallback_name)
            if backend is not None:
                logger.warning(
                    "BackendRegistry: fell through from %r to %r", name, fallback_name
                )
                break

    if backend is None:
        logger.error("BackendRegistry: all backends unavailable, using NoopSummarizer")
        backend = NoopSummarizer()

    logger.info(
        "BackendRegistry: initialized %s (requested: %s)",
        type(backend).__name__,
        name,
    )
    _initialized_backend = backend
    return backend


def get_initialized_backend() -> SummarizerBackend:
    """Return the singleton backend initialized in ``on_startup``.

    Raises RuntimeError if called before ``initialize_backend()``. This means
    ``on_startup`` did not complete — check worker startup logs for errors.
    """
    if _initialized_backend is None:
        raise RuntimeError(
            "BackendRegistry not initialized. "
            "on_startup did not complete — check worker startup logs for errors."
        )
    return _initialized_backend
