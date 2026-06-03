# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Structured JSON logging via `python-json-logger` with `timestamp`,
  `level`, `logger`, `message` fields
- Per-request correlation IDs via `asgi-correlation-id` middleware;
  echoed in `X-Request-ID` response header
- `LOG_LEVEL` env var for runtime log verbosity control
  (DEBUG/INFO/WARNING/ERROR/CRITICAL)
- Prometheus metrics endpoint `GET /metrics` via
  `prometheus-fastapi-instrumentator` (request count, latency, in-flight)
- Business counters: `tickets_created_total`,
  `ticket_status_transitions_total`,
  `ticket_summarization_outcomes_total`
- Redis health probe in `GET /health`; response now includes
  `"redis": "ok" | "unavailable"`
- `elapsed_seconds` field in worker task complete/failure log lines
- Observability quickstart section in README with copy-paste curl examples

### Changed

- `setup_logging()` called at module level in `app/main.py` and
  `app/worker/main.py` (not inside lifespan) to survive uvicorn's handler
  installation
- Correlation ID passed as an explicit arq job argument (contextvars do not
  survive the Redis queue boundary); always reset in worker to prevent stale
  ID leakage across jobs
- `GET /health` response extended:
  `{"status": ..., "database": ..., "redis": ...}`

### Fixed
