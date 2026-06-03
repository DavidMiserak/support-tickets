"""Tests for structured JSON logging configuration."""

import json
import logging

import pytest


def _capture_json_log(
    logger_name: str, level: str, message: str, **extra: object
) -> dict[str, object]:
    """Emit one log record and return it parsed from JSON."""
    import io

    from app.logging_config import setup_logging

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)

    from pythonjsonlogger.json import JsonFormatter

    from app.logging_config import RequestIdFilter

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

    log = logging.getLogger(logger_name)
    log.propagate = False
    log.addHandler(handler)
    log.setLevel(level)
    getattr(log, level.lower())(message, extra=extra)
    log.removeHandler(handler)

    buf.seek(0)
    return dict(json.loads(buf.read()))


def test_json_format_has_required_keys() -> None:
    """Every log record contains the expected top-level keys."""
    record = _capture_json_log("test.format", "INFO", "hello world")
    assert "timestamp" in record
    assert record["level"] == "INFO"
    assert "logger" in record
    assert record["message"] == "hello world"


def test_extra_fields_are_included() -> None:
    """extra={} kwargs appear as top-level keys in the JSON output."""
    record = _capture_json_log("test.extra", "DEBUG", "ticket event", ticket_id=42)
    assert record["ticket_id"] == 42


def test_request_id_injected_when_set() -> None:
    """RequestIdFilter injects the current correlation_id into every record."""
    from asgi_correlation_id import correlation_id

    token = correlation_id.set("abc-123")
    try:
        record = _capture_json_log("test.reqid", "INFO", "with id")
        assert record.get("request_id") == "abc-123"
    finally:
        correlation_id.reset(token)


def test_request_id_is_none_when_not_set() -> None:
    """request_id is null when no correlation ID is active."""
    from asgi_correlation_id import correlation_id

    # Ensure no ID is set
    token = correlation_id.set(None)
    try:
        record = _capture_json_log("test.noid", "INFO", "no id")
        assert record.get("request_id") is None
    finally:
        correlation_id.reset(token)


def test_setup_logging_rejects_invalid_level() -> None:
    """setup_logging raises ValueError for unrecognised level names."""
    from app.logging_config import setup_logging

    with pytest.raises(ValueError, match="Invalid LOG_LEVEL"):
        setup_logging("VERBOSE")


@pytest.mark.parametrize(
    "level",
    ["DEBUG", "info", "WARNING", "error", "CRITICAL"],
)
def test_setup_logging_accepts_valid_levels(level: str) -> None:
    """setup_logging resolves standard level names, case-insensitively."""
    from app.logging_config import setup_logging

    setup_logging(level)
    assert logging.getLogger().level == logging._nameToLevel[level.strip().upper()]


def test_setup_logging_idempotent() -> None:
    """Calling setup_logging twice does not accumulate duplicate handlers."""
    from app.logging_config import setup_logging

    setup_logging("INFO")
    setup_logging("INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 1
