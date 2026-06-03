<!-- /autoplan restore point: /home/david/.gstack/projects/DavidMiserak-support-tickets/feat-observability-4b-autoplan-restore-20260602-224447.md -->
# Phase 4 — Observability (ROUGH DRAFT)

Status: APPROVED (autoplan 2026-06-02, subagent-only) — 16 auto-decisions + 2 resolved at gate.
See "RESOLVED DECISIONS" at the bottom; review report above the audit trail.
Branch: develop
Depends on: Phase 3 (background worker) — done.

---

## Goal

Add structured JSON logging (with per-request correlation IDs), a Prometheus
`/metrics` endpoint, and Redis health visibility to the existing FastAPI + arq
stack. Every request, worker job, and error should be observable without grepping
plaintext lines — a developer should be able to tail logs, see JSON objects,
and drop them into any log aggregator.

After this phase a developer can:
- Set `LOG_LEVEL=DEBUG` and see every request/worker event as a JSON object.
- Grep any log line for `request_id` and find all lines from that request.
- Scrape `GET /metrics` and get HTTP request counts, latency histograms, and
  business counters (tickets created, status transitions, summarization outcomes).
- `GET /health` and see whether Redis is reachable in addition to Postgres.

No UI. No new business logic. No migrations.

### The debugging scenario this enables

Ticket 42 was created but the summary never appeared in the audit trail. Here's
how Phase 4 makes this debuggable end-to-end:

1. **Find the request:** search logs for `"ticket_id": 42` → find the line with
   `"message": "summarize_ticket: started"`.
2. **Trace the API request that created it:** the same log line has `"request_id": "a1b2c3"`.
   Search for `"request_id": "a1b2c3"` → find the `POST /tickets` request, its
   latency, and the `enqueued` summarization outcome counter.
3. **Check the outcome:** `GET /metrics` and look at
   `ticket_summarization_outcomes_total{outcome="enqueue_failed"}` — if it's non-zero,
   the job failed to enqueue. If `enqueued` is 1, check for
   `"message": "summarize_ticket: summarizer raised"` in the worker logs.
4. **Check Redis:** `GET /health` → if `"redis": "unavailable"`, the worker
   never received the job.

Every design decision in Phase 4 serves this flow.

---

## In scope

- **Structured JSON logging.** Replace plaintext uvicorn/stdlib output with
  JSON lines via `python-json-logger`. Every log record gets `timestamp`,
  `level`, `logger`, `message`, plus any `extra={}` fields already in the
  codebase (`ticket_id`, `word_count`, etc.).
- **Correlation IDs.** Add `asgi-correlation-id` middleware. Each request
  generates (or accepts) an `X-Request-ID` header; the value is injected into
  every log record for that request via a `logging.Filter` on `contextvars`.
- **`LOG_LEVEL` env var.** Add to `Settings` (default `INFO`), configure the
  root logger from it in both `app/main.py` and `app/worker/main.py`.
- **Prometheus metrics — HTTP layer.** `prometheus-fastapi-instrumentator`
  mounts on the app at startup; exposes `/metrics`. Covers request count,
  latency histogram, in-flight — no manual code.
- **Prometheus metrics — business counters.** Three custom counters registered
  in a new `app/metrics.py` module:
  - `tickets_created_total` — incremented in `TicketService.create_ticket`.
  - `ticket_status_transitions_total{from_status, to_status}` — incremented in
    `TicketService.update_status`.
  - `ticket_summarization_outcomes_total{outcome}` — labels `enqueued`,
    `deduped`, `skipped_short`, `enqueue_failed`; incremented in
    `TicketService.create_ticket`.
- **Health: Redis visibility.** `GET /health` currently probes Postgres only.
  Add a Redis ping (via the arq pool) so the response reports both:
  `{"status": "ok", "database": "ok", "redis": "ok"}`. Either component
  degraded → `{"status": "degraded", ...}` + 503.
- **Worker structured logging.** The worker already emits structured `extra=`
  logs. Add `elapsed_seconds` to `summarize_ticket` complete/failure lines;
  configure JSON logging in `worker/main.py` using the same `setup_logging()`
  helper.
- Tests: logging format test, `/metrics` smoke test, health Redis branch test,
  business counter unit tests, correlation ID propagation to worker.

> **Note: AnthropicSummarizer deferred to Phase 4b.** It's 1 file and plugs
> into the Phase 3 registry cleanly, but it's not an observability concept —
> it introduces a live external API, new dep, and `run_in_executor`, which would
> distract from the logging/metrics teaching arc. Ship it as a standalone PR
> after Phase 4 lands.

## Out of scope (defer)

- **Worker-side Prometheus endpoint.** The worker is a separate process. A
  dedicated `:9091/metrics` server or multiprocess Prometheus directory would
  work but adds noise for this scale. Defer to a later phase when
  multi-replica observability is needed.
- **Loki / Grafana / alerting.** Collecting JSON logs and building dashboards is
  infra, not app code. Out of scope for this project phase.
- **Distributed tracing (OpenTelemetry).** Overkill for a single-service stack.
  Correlation IDs give the same value for debugging.
- **`/assign` endpoint.** Deferred from Phase 2 — not related to observability.
- **`async_session` → `async_session_factory` rename.** Standalone rename,
  not bundled here.
- **Connection-pool metrics** (pool_size, overflow, checkedout). Useful but not
  in the first observability pass.

---

## Architecture for this phase

```
Request → CorrelationID middleware (asgi-correlation-id)
              → injects X-Request-ID header + contextvars
              → RequestIdFilter: all log records for this request get request_id
        → Prometheus instrumentator (prometheus-fastapi-instrumentator)
              → records HTTP metrics per route+method+status
        → Route → Service
              → TicketService mutates → increments business counters in app/metrics.py
              → logging.getLogger(__name__) → JSON formatter → stdout
                   (all log lines for this request share the same request_id)
arq worker:
  summarize_ticket → logs start/complete/error as JSON (elapsed_seconds added)
                  → JSON formatter configured via setup_logging()
GET /metrics → Prometheus default registry → scrape all counters + instrumentator metrics
GET /health  → Postgres probe (existing) + Redis ping (new)
```

New module `app/logging_config.py`:
```python
def setup_logging(level: str = "INFO") -> None:
    # configure root logger with JsonFormatter + RequestIdFilter
    # called from main.py lifespan and worker/main.py on_startup
```

New module `app/metrics.py`:
```python
tickets_created_total = Counter("tickets_created_total", ...)
ticket_status_transitions_total = Counter(
    "ticket_status_transitions_total", ..., labelnames=["from_status", "to_status"]
)
ticket_summarization_outcomes_total = Counter(
    "ticket_summarization_outcomes_total", ..., labelnames=["outcome"]
)
```

Wiring: these are module-level globals (same pattern as `prometheus_client`
standard practice); importing `app.metrics` anywhere registers them with the
default registry. The `prometheus-fastapi-instrumentator` also uses the default
registry, so `/metrics` serves both automatically.

---

## Logging configuration

### Setup

`app/logging_config.py` (call at module level in `main.py` before any route registration, and in `worker/main.py` before arq starts — NOT inside lifespan, because uvicorn installs its own handlers after the app object is created but before the lifespan runs):

```python
import logging
from asgi_correlation_id.context import correlation_id
from pythonjsonlogger.json import JsonFormatter  # python-json-logger>=3.x

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = correlation_id.get(default=None)
        return True

def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    ))
    handler.addFilter(RequestIdFilter())
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers = [handler]
    # Prevent uvicorn's access logger from duplicating lines in JSON mode:
    logging.getLogger("uvicorn.access").propagate = False
```

Call at the **module level** in `app/main.py` (before `app = FastAPI(...)`) and
at the top of `app/worker/main.py` (before `WorkerSettings` is defined):
```python
# app/main.py — top of file, after imports:
from app.logging_config import setup_logging
from app.config import settings
setup_logging(settings.log_level)
```

### Correlation ID middleware

The `asgi-correlation-id` library adds one middleware. **Ordering matters:**
`CorrelationIdMiddleware` must be added first (outermost) so the correlation ID
is set before the Prometheus instrumentator records the request. FastAPI adds
middleware in LIFO order, so add `CorrelationIdMiddleware` first in code:

```python
from asgi_correlation_id import CorrelationIdMiddleware
from asgi_correlation_id.context import correlation_id
import uuid

# Add FIRST (will be outermost in the middleware stack):
app.add_middleware(
    CorrelationIdMiddleware,
    header_name="X-Request-ID",
    generator=lambda: uuid.uuid4().hex,
    validator=lambda v: len(v) <= 64 and v.isascii(),
)
# Then mount the Instrumentator (inner):
Instrumentator().instrument(app).expose(app)
```

The library uses its own `contextvars` for the ID. Point the `RequestIdFilter`
at `correlation_id.get()` instead of a custom var. The middleware echoes the
value back in `X-Request-ID` on the response.

### Correlation ID propagation to worker

`contextvars` are process-local and not serialized to Redis. The correlation ID
will be `None` in the worker unless passed explicitly. Fix: include it as a job
arg at enqueue time, and set it on the worker's context var before logging:

```python
# In TicketService.create_ticket — enqueue with request_id:
job = await self._arq_pool.enqueue_job(
    "summarize_ticket",
    ticket.id,
    _job_id=f"summarize-{ticket.id}",
    _correlation_id=correlation_id.get(),  # explicit; serialized to Redis
)

# In summarize_ticket — restore the correlation ID context:
async def summarize_ticket(ctx, ticket_id: int, _correlation_id: str | None = None) -> None:
    if _correlation_id:
        set_correlation_id(_correlation_id)  # asgi_correlation_id.context
    logger.info("summarize_ticket: started", extra={"ticket_id": ticket_id})
    ...
```

### Log call sites already in place

The codebase already uses structured `extra={}`:
```python
logger.info("summarize_ticket: started", extra={"ticket_id": ticket_id})
logger.info("summarize_ticket: complete",
            extra={"ticket_id": ticket_id, "summary_len": len(summary)})
```
These will just work with the JSON formatter. No call-site changes needed —
only the formatter changes.

### `elapsed_seconds` for worker tasks

Add timing to `summarize_ticket`:
```python
import time
start = time.monotonic()
# ... existing logic ...
logger.info("summarize_ticket: complete", extra={
    "ticket_id": ticket_id,
    "summary_len": len(summary),
    "elapsed_seconds": round(time.monotonic() - start, 3),
})
```

---

## Prometheus metrics

### HTTP layer (zero-code)

```python
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)  # in lifespan or after app init
```

This registers and exposes:
- `http_requests_total{method, handler, status}` — count
- `http_request_duration_seconds{method, handler}` — histogram (0.001–30s buckets)
- `http_requests_inprogress{method, handler}` — gauge

### Business counters (app/metrics.py)

```python
from prometheus_client import Counter

tickets_created_total = Counter(
    "tickets_created_total",
    "Number of tickets created.",
)

ticket_status_transitions_total = Counter(
    "ticket_status_transitions_total",
    "Number of ticket status transitions.",
    ["from_status", "to_status"],
)

ticket_summarization_outcomes_total = Counter(
    "ticket_summarization_outcomes_total",
    "Outcomes of ticket summarization enqueue attempts.",
    ["outcome"],   # enqueued | deduped | skipped_no_pool | enqueue_failed
    # Note: "skipped_short" (description too short) is NOT included here —
    # that check happens inside the worker task, not at enqueue time.
    # It is observable via structured worker logs only.
)
```

Increment points:
- `tickets_created_total.inc()` — end of `TicketService.create_ticket` (after commit).
- `ticket_status_transitions_total.labels(...)` — end of `TicketService.update_status`
  (after commit; skip for same-status no-op).
- `ticket_summarization_outcomes_total.labels(...)` — in the enqueue block of
  `TicketService.create_ticket` (four branches: enqueued, deduped/None return,
  skipped because no pool, enqueue_failed).

### `/metrics` exposure

`prometheus-fastapi-instrumentator` calls `.expose(app)` which adds a `GET /metrics`
route. No extra code. The business counters from `app/metrics.py` are in the
default registry and will appear automatically.

---

## Health endpoint

Current: `{"status": "ok", "database": "ok"}` or `{"status": "degraded"}`.

New: probe Redis (ping via arq pool) and include in response:
```json
{"status": "ok",   "database": "ok",  "redis": "ok"}
{"status": "degraded", "database": "ok", "redis": "unavailable"}
```

The arq pool is optional (`ArqRedis | None`). If `None` (Redis was down at
startup), report `"redis": "unavailable"` without attempting a ping.

`check_redis_connection(pool: ArqRedis | None) -> str` → `"ok"` | `"unavailable"`:
```python
if pool is None:
    return "unavailable"
try:
    await pool.ping()
    return "ok"
except Exception:
    logger.warning("redis health check failed", exc_info=True)
    return "unavailable"
```

The `/health` route already lives in `main.py`. It will need access to
`get_arq_pool()` — use the existing module-level accessor (same as the API
route already does for enqueue).

Status logic: `"degraded"` + 503 if either probe fails; `"ok"` + 200 if both pass.
Either component can be degraded independently.

---

## AnthropicSummarizer (light scope)

New file `app/worker/backends/anthropic.py`:
```python
from anthropic import Anthropic
from app.worker.backends.base import BaseSummarizer

class AnthropicSummarizer(BaseSummarizer):
    def __init__(self, api_key: str) -> None:
        self._client = Anthropic(api_key=api_key)

    async def summarize(self, text: str) -> str:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._call, text)

    def _call(self, text: str) -> str:
        msg = self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content":
                f"Summarize this support ticket in 1-2 sentences: {text}"}],
        )
        return msg.content[0].text
```

`BackendRegistry._BACKENDS["anthropic"]` → `AnthropicSummarizer`. The key is
registered only if `settings.anthropic_api_key is not None` (avoids an import
error when the `anthropic` package is not installed). If the key is absent and
`SUMMARIZER_BACKEND=anthropic` is set, `initialize_backend()` falls back to
NoopSummarizer with a warning (existing registry fallback behavior).

Config additions:
```python
anthropic_api_key: str | None = None  # Settings
```
Compose: no change (noop remains the default; override with env var).

---

## Files touched

New:
- `app/logging_config.py` — `setup_logging()` + `RequestIdFilter`
- `app/metrics.py` — three Prometheus counters
- `app/worker/backends/anthropic.py` — AnthropicSummarizer
- `app/tests/test_logging.py` — verify JSON output format + request_id injection
- `app/tests/test_metrics.py` — business counters increment correctly
- `app/tests/test_health.py` — extend existing health tests with Redis branch

Modified:
- `app/config.py` — `LOG_LEVEL`, `ANTHROPIC_API_KEY`
- `app/main.py` — call `setup_logging()` in lifespan; mount instrumentator;
  add CorrelationIdMiddleware; update `/health` to probe Redis
- `app/services/ticket.py` — increment business counters
- `app/worker/main.py` — call `setup_logging()` in `on_startup`
- `app/worker/tasks.py` — add `elapsed_seconds` to complete/failure log lines
- `app/worker/registry.py` — register `anthropic` backend
- `requirements.txt` — add `python-json-logger`, `asgi-correlation-id`,
  `prometheus-client`, `prometheus-fastapi-instrumentator`, `anthropic` (optional)
- `README.md` — document `LOG_LEVEL`, `/metrics`, correlation ID header

No new Alembic migrations (no schema changes).

---

## DX notes for implementation

- **README: add an "Observability" section.** Three one-liners: tail logs
  (`docker compose logs -f api`), scrape metrics (`curl http://localhost:8000/metrics`),
  and test correlation IDs (`curl -H "X-Request-ID: my-trace-id" http://localhost:8000/health`).
  List the three custom counter names and their label dimensions.
- **`LOG_LEVEL` in compose.yaml.** Add to both `api` and `worker` environment
  blocks: `LOG_LEVEL: INFO  # DEBUG|INFO|WARNING|ERROR|CRITICAL`.
- **`LOG_LEVEL` in README env table.** Document default (`INFO`) and note it
  requires container restart to change (env var, not signal-driven).
- **`anthropic` dep.** Add to a new `requirements-optional.txt` (mirrors the
  `requirements-ml.txt` pattern for ML deps). Do NOT add to the main
  `requirements.txt`. Worker `Containerfile` doesn't install it by default;
  developers opt in explicitly.
- **`LOG_LEVEL` validation in `setup_logging()`.** Guard before `setLevel`:
  ```python
  numeric = logging.getLevelName(level.upper())
  if not isinstance(numeric, int):
      raise ValueError(f"Invalid LOG_LEVEL: {level!r}. Valid values: DEBUG, INFO, WARNING, ERROR, CRITICAL.")
  ```
  This surfaces misconfiguration immediately at startup rather than silently
  falling back to WARNING.

## Test plan

- **Logging format (unit):** call `setup_logging("DEBUG")`; emit a log record;
  capture stdout; parse JSON; assert `timestamp`, `level`, `logger`, `message`
  keys present. Assert `extra={"ticket_id": 42}` produces `"ticket_id": 42`
  in the JSON object.
- **Correlation ID (integration):** `GET /health` via TestClient; assert
  `X-Request-ID` in response headers; log capture shows the same ID in the
  `request_id` field on every line emitted during that request.
- **Business counters (unit, delta assertions):** capture pre-call counter
  values, call `TicketService.create_ticket` with a mock repo + session, assert
  delta = 1. Do NOT assert absolute values (counters are process-global singletons;
  test isolation requires deltas). Exercise all 3 summarization outcome branches
  at enqueue site: `enqueued`, `deduped`, `skipped_no_pool`, `enqueue_failed`.
  The `skipped_short` outcome (description too short) is NOT a counter — it is a
  worker-task log event only. Call `update_status` and assert
  `ticket_status_transitions_total.labels(from_status=..., to_status=...)` delta = 1.
- **`/metrics` smoke test (integration):** `GET /metrics` via TestClient; assert
  200; assert response body contains `"tickets_created_total"`.
- **Health Redis branch (integration):** `GET /health` with `arq_pool=None`
  (simulated via `stub_arq_pool` fixture); assert body contains
  `"redis": "unavailable"`, status 503. With mock pool that `ping()` succeeds:
  assert `"redis": "ok"`, status 200.
- **AnthropicSummarizer unit:** stub `Anthropic` client; call `summarize("text")`;
  assert returns `msg.content[0].text`.
- **Worker elapsed_seconds:** patch `time.monotonic`; run `summarize_ticket` via
  task fixture; assert log record contains `elapsed_seconds`.

---

## Open decisions for review

- **D1 — Logging library.** `python-json-logger` (adds 1 dep, import path
  changed between 2.x and 3.x — pin explicitly) vs stdlib `logging.Formatter`
  subclass (no dep, ~30 lines, teaches `LogRecord` internals directly). For a
  learning project the stdlib approach teaches more. Draft uses `python-json-logger`
  for brevity; switch to stdlib if the teaching value matters more than code volume.
- **D2 — Correlation ID library.** `asgi-correlation-id` (small dep) vs custom
  ASGI middleware (~20 lines). Draft uses the library.
- **D3 — Worker metrics.** Logs-only (draft). Note: the goal statement says
  "every worker job should be observable" — this is met via structured JSON logs
  but NOT via Prometheus. Worker-side Prometheus (multiprocess or dedicated port)
  is deferred. The goal statement reflects this constraint.
- **D4 — AnthropicSummarizer in Phase 4?** CEO review recommends cutting: it
  introduces live external API call, new dep, conditional import guards, and
  `run_in_executor` — none of which are observability concepts. Defer to a
  standalone Phase 4b or "AI backends" phase.
- **D5 — `/metrics` on same port.** Same port (`:8000/metrics`) is correct at
  this scale. Resolved.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | CEO | Add version pins to plan | Mech | P1 | Prevents import-path failures on install |
| 2 | CEO | Add uvicorn handler conflict note | Mech | P5 | Real correctness trap, explicit fix needed |
| 3 | CEO | Narrow goal statement to match D3 | Mech | P5 | Broken promise: "every job observable" vs logs-only |
| 4 | CEO | Add D1 rationale (dep vs stdlib) | Mech | P5 | Learner needs to see the tradeoff |
| 5 | Eng | Delta assertions in metric tests (not absolute values) | Mech | P1 | Counters are global singletons; absolute values cause test-order flakiness |
| 6 | Eng | Pass correlation_id as explicit arq job arg | Mech | P1 | contextvars are not serialized to Redis; worker otherwise logs request_id: null |
| 7 | Eng | setup_logging() at module level, not inside lifespan | Mech | P5 | Uvicorn installs its own handlers after app object creation but before lifespan runs |
| 8 | Eng | Remove skipped_short from counter labels | Mech | P3 | Check happens in worker task, not service; counter can't be incremented at enqueue site |
| 9 | Eng | Specify middleware order (CorrelationIdMiddleware outermost, then Instrumentator) | Mech | P5 | LIFO ordering: wrong order means Prometheus records before request_id is set |
| 10 | Eng | Add validator=is_valid_uuid to CorrelationIdMiddleware | Mech | P1 | Prevents ID poisoning from unbounded client-supplied X-Request-ID headers |
| 11 | Eng | Keep process metrics on /metrics (no unregister) | Taste→auto | P6 | Learning project — seeing process_ metrics is educational, not harmful |
| 12 | DX | Add observability quickstart section to README | Mech | P1 | Developer can't discover JSON logs or /metrics without a doc pointer |
| 13 | DX | Add LOG_LEVEL to compose.yaml (api + worker) and README env table | Mech | P1 | Env var invisible without being listed; can't be discovered by scanning compose |
| 14 | DX | Move anthropic dep to requirements-optional.txt | Mech | P4 | Follows requirements-ml.txt pattern; anthropic is live-API dep, not observability |
| 15 | DX | Add LOG_LEVEL validation in setup_logging() | Mech | P1 | Bad value silently falls back to WARNING; raises ValueError immediately instead |
| 16 | DX | Document /metrics endpoint and custom counter names in README | Mech | P1 | Counter names only discoverable by reading source without docs |
| 17 | DX | Fix LOG_LEVEL literal in compose.yaml → ${LOG_LEVEL:-INFO} | Mech | P1 | Documented escape hatch `LOG_LEVEL=DEBUG docker compose up` silently broken — literal value ignores host env (post-impl review 2026-06-02) |
| 18 | Eng | Add explicit validator=is_valid_uuid4 to CorrelationIdMiddleware | Mech | P5 | Decision 10 approved this; implementation uses bare add_middleware() — default validator is active but intent invisible (post-impl review 2026-06-02) |
| 19 | Eng | Redact Redis URL password in startup log lines (main.py:48,54) | Mech | P1 | Credential logging security gap — redis_url may contain password (post-impl review 2026-06-02) |
| 20 | Eng | Fix test_setup_logging_idempotent root logger isolation | Mech | P1 | Test mutates root logger without restoring prior handlers — bleeds into pytest caplog (post-impl review 2026-06-02) |
| 21 | Eng | Fix test_health_returns_503_when_redis_unavailable cleanup | Mech | P1 | Stale dependency override on exception — use pop() not re-assignment (post-impl review 2026-06-02) |
| 22 | Eng | Add test for invalid X-Request-ID rejected and replaced | Mech | P1 | Missing coverage for correlation ID middleware security surface (post-impl review 2026-06-02) |
| 23 | DX | Add LOG_LEVEL to .env.example | Mech | P1 | Config template incomplete; after A7 fix env var effective but invisible in .env.example (post-impl review 2026-06-02) |
| 24 | DX | Populate CHANGELOG for Phase 4 | Mech | P1 | Changelog empty despite shipped features (post-impl review 2026-06-02) |

## Notes for implementation

- **Uvicorn logging conflict.** Uvicorn installs its own logging handlers at
  startup. Calling `root.handlers = [handler]` after uvicorn starts will fight
  with uvicorn's formatters. Fix: call `setup_logging()` before `uvicorn.run()`
  for local dev, or configure via `--log-config` / `log_config=None` to suppress
  uvicorn's default setup, then set your own. In the compose stack, uvicorn runs
  as a subprocess — pass `--no-access-log` and set `propagate=False` on the
  `uvicorn.access` logger to keep access logs from duplicating in JSON form.
- **Pin library versions.** Actual pinned versions in requirements.txt:
  `python-json-logger==4.1.0`, `asgi-correlation-id==5.0.0`,
  `prometheus-client==0.25.0`, `prometheus-fastapi-instrumentator==8.0.0`.
  The import path `pythonjsonlogger.json` works for both 3.x and 4.x.
  Note: plan originally referenced 3.x/4.x/7.x; shipped versions are higher — all
  API-compatible. [Updated 2026-06-02 post-implementation autoplan review]

---

# GSTACK /autoplan REVIEW REPORT

Voices: **subagent-only** (Codex binary not installed — dual-voice degraded).
Base branch: `main`. Branch: `develop`.

## Phase 1 — CEO / Strategy

### CEO Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Premises valid?                    YES     [unavailable]  YES (1 voice)
  2. Right problem to solve?            YES     [unavailable]  YES — right for learning project
  3. Scope calibration correct?        MOSTLY  [unavailable]  AnthropicSummarizer is scope creep
  4. Alternatives explored?            PARTIAL [unavailable]  D1 stdlib tradeoff understated
  5. Learning-per-effort sound?        YES     [unavailable]  YES (1 voice)
  6. 3-month trajectory sound?         YES     [unavailable]  YES — clean observability foundation
```

### CEO findings
- **HIGH — AnthropicSummarizer scope creep.** Introduces live external API, new dep, conditional import guards, run_in_executor — none of these are observability concepts. Cut to Phase 4b. → TASTE DECISION at gate.
- **HIGH — Library versions not pinned.** `python-json-logger` import path changed in 3.x. Without a pin, `pip install` may pick the wrong major version. → auto-decided: add version pins.
- **HIGH — Uvicorn logging handler conflict.** `setup_logging()` inside lifespan is overwritten by uvicorn's own `logging.config.dictConfig`. → auto-decided: call at module level before `app = FastAPI(...)`.
- **MEDIUM — Goal statement vs D3 deferral.** "Every worker job observable" is not met by Prometheus if worker metrics are deferred. → auto-decided: narrow goal statement.
- **MEDIUM — Missing learning narrative.** No worked scenario ("how you'd debug ticket 42"). → TASTE DECISION at gate.
- **LOW — D1 dep rationale not stated.** Plan takes `python-json-logger` without explaining why over stdlib. → auto-decided: add rationale.

## Phase 3 — Engineering

### Eng Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Architecture sound?               MOSTLY  [unavailable]  Middleware order + logging placement wrong
  2. Test coverage sufficient?         NO      [unavailable]  Counter delta issue; missing coverage
  3. Concurrency handled?              YES     [unavailable]  prometheus_client uses locks internally
  4. Correlation ID propagation correct? NO    [unavailable]  CRITICAL: lost at job queue boundary
  5. Counter label design correct?     MOSTLY  [unavailable]  skipped_short can't be incremented at service
  6. Security surface acceptable?      MOSTLY  [unavailable]  process metrics exposed; X-Request-ID unbounded
```

### Eng findings
- **CRITICAL — Prometheus test state leak.** Counters are process-global; absolute value assertions cause test-order flakiness. → auto-decided: delta assertions.
- **HIGH — Correlation ID lost at arq queue boundary.** `contextvars` not serialized to Redis; worker logs `request_id: null`. → auto-decided: pass as explicit job arg.
- **HIGH — `setup_logging()` inside lifespan overwritten by uvicorn.** → auto-decided: module-level call.
- **HIGH — Absolute counter assertions in planned tests.** → auto-decided: delta assertions.
- **MEDIUM — `skipped_short` label can't be incremented from service.** Check happens in worker task. → auto-decided: remove label, document as worker-log-only.
- **MEDIUM — Middleware ordering not specified.** CorrelationIdMiddleware must be outermost (added first in LIFO). → auto-decided: specify in plan.
- **MEDIUM — Process metrics exposed on `/metrics`.** Intentional for learning project. → auto-decided: keep (educational).
- **LOW — `X-Request-ID` not validated.** Add `validator=is_valid_uuid`. → auto-decided: in.
- **LOW — `elapsed_seconds` mock returns 0.** Acceptable for unit test; noted. → auto-decided: accept.

## Phase 3.5 — Developer Experience

### DX Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Getting started < 5 min?          NO      [unavailable]  README has no observability quickstart
  2. LOG_LEVEL discoverable?           NO      [unavailable]  Missing from compose.yaml and env table
  3. Error messages actionable?        NO      [unavailable]  Bad LOG_LEVEL silently falls back
  4. /metrics documented?              NO      [unavailable]  Counter names not in README
  5. Dep separation correct?           NO      [unavailable]  anthropic mixed with mandatory deps
  6. Compose friction-free?            MOSTLY  [unavailable]  LOG_LEVEL missing from environment blocks
```

DX score: **6/10** as specified → ~8.5/10 after fixes. TTHW: undefined (no quickstart) → ~3–5 min after README update.

### DX findings
- **HIGH — README has no observability quickstart.** → auto-decided: add section.
- **HIGH — `LOG_LEVEL` missing from compose.yaml and README env table.** → auto-decided: add to both.
- **MEDIUM — `anthropic` dep mixed with mandatory deps.** → auto-decided: move to `requirements-optional.txt`.
- **MEDIUM — Bad `LOG_LEVEL` silently falls back to WARNING.** → auto-decided: add validation.
- **MEDIUM — `/metrics` and custom counters undocumented.** → auto-decided: add to README.

## Cross-Phase Themes

- **Missing concrete docs content** — CEO flagged missing learning narrative; DX flagged README has no observability quickstart. Same gap, two angles. High-confidence signal: the plan specifies *what* to add to README but not *what content*, and that gap will bite the implementer.
- **AnthropicSummarizer scope** — CEO said cut; DX said separate the dep. Both flag it as out-of-place in an observability phase. Surfaced at gate.
- **Logging setup ordering** — CEO noted uvicorn conflict; Eng pinpointed the exact mechanism. Both agree: `setup_logging()` must run at module level, not inside lifespan.

## Surfaced for your decision (taste / scope)
- **S1 (scope) — AnthropicSummarizer in Phase 4 or defer?** CEO + DX both recommend defer/separate. Recommend: defer to Phase 4b or a dedicated "AI backends" standalone PR.
- **S2 (taste) — Add a learning narrative / worked debugging scenario?** CEO recommends; DX is silent. Recommend: add a 3-line callout ("here's how you'd debug ticket 42 end-to-end using logs + /metrics + X-Request-ID").

## Implementation Tasks (aggregated)
- [ ] **P0 — Call `setup_logging()` at module level** in `app/main.py` and `app/worker/main.py` (before `FastAPI()` / `WorkerSettings`). (eng)
- [ ] **P0 — Pass correlation ID as explicit arq job arg.** `enqueue_job(..., _correlation_id=correlation_id.get())` in `create_ticket`; restore in `summarize_ticket` via `set_correlation_id()`. (eng)
- [ ] **P1 — `app/logging_config.py`.** `setup_logging()` + `RequestIdFilter` + `LOG_LEVEL` validation. (eng)
- [ ] **P1 — `app/metrics.py`.** Three Prometheus counters (module-level). (eng)
- [ ] **P1 — CorrelationIdMiddleware first, Instrumentator second** in `app/main.py`. (eng)
- [ ] **P1 — Business counter increments** in `TicketService.create_ticket` and `update_status`. (eng)
- [ ] **P1 — Redis health check** in `GET /health`. (eng)
- [ ] **P1 — `elapsed_seconds` in worker complete/failure logs.** (eng)
- [ ] **P1 — Pin library versions** in `requirements.txt`. (eng)
- [ ] **P1 — `LOG_LEVEL` in `Settings`, compose.yaml (api + worker), README env table.** (dx)
- [ ] **P1 — README observability quickstart section + `/metrics` counter docs.** (dx)
- [ ] **P2 — `requirements-optional.txt`** for `anthropic` dep (if D4 included). (dx)
- [ ] **P2 — `setup_logging()` idempotency test.** Assert calling twice doesn't accumulate handlers. (eng)
- [ ] **P2 — Test X-Request-ID passthrough** (client-supplied header accepted + echoed). (eng)
- [ ] *S1 dependent* — `app/worker/backends/anthropic.py` + registry registration (defer to Phase 4b if S1 accepted).

---

## RESOLVED DECISIONS (post-autoplan, 2026-06-02)

- **S1 — AnthropicSummarizer: DEFERRED to Phase 4b.** Scope is clean (1 file,
  plugs into Phase 3 BackendRegistry), but it's not an observability concept.
  Phase 4 stays focused on logging + metrics. Phase 4b ships as a standalone PR.
  Drop `app/worker/backends/anthropic.py`, `ANTHROPIC_API_KEY`, and related
  tests from Phase 4 scope. Add a note to TODOS.md under "Phase 4b".

- **S2 — Learning narrative: ADDED.** Short debugging scenario callout added to
  the Goal section — explains how correlation IDs, structured logs, and `/metrics`
  connect in a real debugging flow (ticket 42 summarization failure). No code
  change.

### Net effect on scope (final)
- IN: `app/logging_config.py` (setup_logging + RequestIdFilter + LOG_LEVEL validation),
  `app/metrics.py` (3 counters, module-level), CorrelationIdMiddleware (outermost,
  with UUID validator), prometheus-fastapi-instrumentator (HTTP metrics + /metrics),
  business counter increments in TicketService, correlation ID as explicit arq job arg,
  Redis health check in /health, elapsed_seconds in worker logs, LOG_LEVEL in Settings
  + compose.yaml + README, observability quickstart in README, library version pins.
- OUT (deferred to Phase 4b): AnthropicSummarizer, ANTHROPIC_API_KEY, requirements-optional.txt
  for anthropic dep.
- OUT (deferred to later): worker-side Prometheus endpoint, multiprocess registry,
  Loki/Grafana, OpenTelemetry, /assign endpoint.

---

# GSTACK /autoplan REVIEW REPORT — Session 2 (2026-06-02, branch: feat/observability-4b)

**Context:** Post-implementation review. Code is fully shipped on main. This review
checks plan vs. implementation fidelity and surfaces remaining gaps.
Voices: **subagent-only** (Codex binary not installed — dual-voice degraded).
Base branch: `main`.

## Phase 1 — CEO / Strategy

### CEO Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Premises valid?                    YES     [unavailable]  YES — learning project, right problem
  2. Right problem to solve?            YES     [unavailable]  YES — observability needed at this stage
  3. Scope calibration correct?        MOSTLY  [unavailable]  AnthropicSummarizer correctly deferred
  4. Alternatives explored?            MOSTLY  [unavailable]  D1 (stdlib vs library) now resolved
  5. Learning-per-effort sound?        YES     [unavailable]  YES — good curriculum progression
  6. 3-month trajectory sound?         MOSTLY  [unavailable]  Worker metrics gap needs caveat
```

### CEO findings (Session 2 — post-implementation)
- **MEDIUM — Worker observability hole not caveated.** The debugging scenario says
  "check `ticket_summarization_outcomes_total{outcome="enqueued"}` is 1" but this
  counter is incremented at enqueue time in the API, not by the worker. A post-dequeue
  failure (summarizer crash, DB commit fail) shows `outcome="enqueued"` = 1 while the
  job silently failed. This is visible only in worker structured logs, not metrics.
  → auto-decided: add caveat to debugging scenario. (D5, Mech, P1)
- **MEDIUM — Plan "Notes for implementation" has stale version pins.** Says 3.x/4.x/7.x;
  actual requirements.txt ships 4.1.0/5.0.0/8.0.0. → auto-decided: update plan notes.
  (D6 updated, Mech, P5)
- **LOW — `correlation_id=` vs `_correlation_id=` inconsistency.** Plan code snippets
  use `_correlation_id=correlation_id.get()` at enqueue (arq internal-arg convention).
  Implementation correctly uses `correlation_id=correlation_id.get(None)` as an explicit
  task parameter. Plan snippets are wrong. → auto-decided: update plan snippets. (Mech, P5)
- **LOW — D1 (stdlib vs python-json-logger) never formally resolved.** Implementation
  chose python-json-logger. → auto-decided: add resolved decision entry. (Mech, P5)
- **INFO — Worker always sets correlation_id even when None (not in original plan).**
  Fix commit c803eab correctly calls `_correlation_id_var.set(correlation_id)` unconditionally
  to prevent stale ID leakage across arq jobs. The plan only specified the "if truthy" case.
  Implementation improved on plan. No action needed.

## Phase 3 — Engineering

### Eng Dual Voices — Consensus Table (Session 2)
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Architecture sound?               YES     [unavailable]  YES — middleware order, logging placement correct
  2. Test coverage sufficient?         MOSTLY  [unavailable]  Missing: invalid X-Request-ID rejection test
  3. Concurrency handled?              YES     [unavailable]  prometheus_client uses locks; arq ContextVar isolation correct
  4. Correlation ID propagation?       YES     [unavailable]  Always set (even None) — stale ID cleared correctly
  5. Security surface acceptable?      MOSTLY  [unavailable]  Redis URL may log credentials; validator not explicit
  6. Test isolation?                   NO      [unavailable]  Two test isolation issues (root logger + health override)
```

### Eng findings (Session 2 — post-implementation)
- **MEDIUM — Redis URL with credentials logged verbatim.** `app/main.py:48,54` logs
  `settings.redis_url` directly. If `REDIS_URL=redis://:password@host:6379`, the password
  appears in structured logs. → auto-decided: redact password before logging. (D19, Mech, P1)
- **MEDIUM — `CorrelationIdMiddleware` missing explicit `validator=` arg.** Plan Decision 10
  approved `validator=is_valid_uuid` but implementation uses bare `add_middleware(CorrelationIdMiddleware)`.
  The default `is_valid_uuid4` IS active (so no functional gap), but intent is invisible in
  code. Also: generator emits 32-char hex (no hyphens) while validator accepts hyphenated
  UUIDs — inconsistent log formats depending on client vs server-generated IDs.
  → auto-decided: add explicit `validator=is_valid_uuid4`. (D18, Mech, P5)
- **MEDIUM — test_setup_logging_idempotent mutates root logger without cleanup.**
  `test_logging.py:107-114` replaces `root.handlers` then leaves them; bleeds into pytest
  `caplog`. → auto-decided: add try/finally restore. (D20, Mech, P1)
- **MEDIUM — test_health_returns_503_when_redis_unavailable cleanup leaves stale mock.**
  `test_health.py` re-assigns the override in finally instead of using `pop()`. On test body
  exception, subsequent tests see wrong mock. → auto-decided: use `pop()`. (D21, Mech, P1)
- **MEDIUM — Missing test: invalid X-Request-ID rejected and replaced.**
  No test sends `"not-a-uuid"` and asserts the middleware generates a fresh ID.
  → auto-decided: add test. (D22, Mech, P1)
- **LOW — `/metrics` in OpenAPI schema.** `Instrumentator().expose(app)` uses
  `include_in_schema=True` by default. Exposed publicly and in Swagger UI. Intentional for
  learning project (discoverable). → auto-decided: keep. (Taste→auto, P6)
- **LOW — `skipped_short` not observable via counter.** Worker task logs it but no counter.
  → TASTE DECISION at gate. (T1)
- **LOW — Prometheus label pairs not pre-seeded.** Fresh process won't emit label series
  until first increment. → TASTE DECISION at gate. (T2)

## Phase 3.5 — Developer Experience

### DX Dual Voices — Consensus Table (Session 2)
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Getting started < 5 min?          MOSTLY  [unavailable]  4-5 min on happy path; LOG_LEVEL broken
  2. LOG_LEVEL escape hatch working?   NO      [unavailable]  compose.yaml uses literal — host env ignored
  3. Error messages actionable?        YES     [unavailable]  ValueError message is clear
  4. /metrics documented?              YES     [unavailable]  README has counter names + curl example
  5. Dep separation correct?           YES     [unavailable]  anthropic correctly deferred
  6. Compose friction-free?            NO      [unavailable]  LOG_LEVEL: INFO literal prevents env override
```

DX score: **6.5/10** (vs predicted 8.5/10 — LOG_LEVEL broken escape hatch is the gap).

### DX findings (Session 2 — post-implementation)
- **HIGH — `LOG_LEVEL=DEBUG docker compose up` is documented but silently broken.**
  `compose.yaml` uses `LOG_LEVEL: INFO` (literal) in both `api` and `worker` environment
  blocks. Docker Compose variable substitution requires `${LOG_LEVEL:-INFO}` syntax. Host
  env vars are ignored entirely with literals. README's documented escape hatch doesn't work.
  → auto-decided: fix to `${LOG_LEVEL:-INFO}`. (D17, Mech, P1)
- **MEDIUM — `.env.example` missing `LOG_LEVEL`.** Every other env var is in the template.
  → auto-decided: add commented entry. (D23, Mech, P1)
- **MEDIUM — `X-Request-ID` not discoverable from Swagger UI.** Header not in OpenAPI schema.
  A developer starting at `/docs` (the default landing page) won't know correlation IDs exist.
  → TASTE DECISION at gate. (T3)
- **LOW — `X-Request-ID` rejection not documented.** README says "must be a valid UUID4" but
  not what happens on rejection (silent replacement + warning log). → note at gate (minor doc).
- **LOW — CHANGELOG empty for Phase 4.** Shipped structured logging, Prometheus metrics,
  correlation IDs — none in CHANGELOG. → auto-decided: populate. (D24, Mech, P1)

## Cross-Phase Themes (Session 2)
- **LOG_LEVEL escape hatch: broken promise.** DX subagent found it broken in compose.yaml.
  This is the highest-impact single fix in this session.
- **Credential exposure in logs.** CEO flagged it as a 6-month regret. Eng confirmed the
  exact mechanism (redis_url logged verbatim). High-confidence signal.
- **Test isolation gaps.** Eng subagent found two independent test isolation issues
  (root logger mutation, health test stale override). Both mechanical fixes, no taste.

## Taste decisions for gate (Session 2)
- **T1** — Add `skipped_short` counter label to `tasks.py:71` (LOW) — changes metric contract
  previously decided not to include.
- **T2** — Pre-seed Prometheus label combinations at startup (LOW) — cosmetic but changes
  startup behavior.
- **T3** — Make `X-Request-ID` discoverable via Swagger UI (MEDIUM) — extra code complexity
  for learning project; may not be worth it.

## Implementation Tasks (Session 2 — auto-decided, must fix before Phase 4b)
- [ ] **CRITICAL — Fix `LOG_LEVEL: ${LOG_LEVEL:-INFO}` in compose.yaml** (api + worker). `compose.yaml:12,46`
- [ ] **HIGH — Redact Redis URL password in startup log.** `app/main.py:48,54`
- [ ] **MEDIUM — Add `validator=is_valid_uuid4` to `CorrelationIdMiddleware`.** `app/main.py:78`
- [ ] **MEDIUM — Fix `test_setup_logging_idempotent` root logger isolation.** `app/tests/test_logging.py:107`
- [ ] **MEDIUM — Fix `test_health` stale override cleanup (`pop()` in finally).** `app/tests/test_health.py`
- [ ] **MEDIUM — Add invalid X-Request-ID rejection test.** `app/tests/test_health.py`
- [ ] **LOW — Add `LOG_LEVEL` to `.env.example`.**
- [ ] **LOW — Populate CHANGELOG with Phase 4 additions.**
