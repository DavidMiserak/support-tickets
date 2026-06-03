"""Structured JSON logging configuration.

Call setup_logging() at module level in main.py and worker/main.py — before
FastAPI() is created and before arq WorkerSettings is defined. Uvicorn installs
its own handlers after the app object is created but before the lifespan runs,
so calling setup_logging() inside a lifespan would be overwritten.
"""

import logging

from asgi_correlation_id import correlation_id
from pythonjsonlogger.json import JsonFormatter


class RequestIdFilter(logging.Filter):
    """Inject the current correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = correlation_id.get(None)
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with JSON output and request-ID injection.

    Raises ValueError for unrecognised level names so misconfiguration surfaces
    immediately at startup rather than silently degrading to WARNING.
    """
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        raise ValueError(
            f"Invalid LOG_LEVEL: {level!r}. "
            "Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL."
        )

    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "asctime": "timestamp",
                "levelname": "level",
                "name": "logger",
            },
        )
    )
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers = [handler]

    # Prevent uvicorn's access logger from duplicating lines in JSON mode.
    logging.getLogger("uvicorn.access").propagate = False
