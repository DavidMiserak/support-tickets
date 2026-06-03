# TODOs
<!-- markdownlint-disable MD013 -->

Tracked work for the support ticket service. Derived from the /autoplan reviews
on 2026-06-02 (design doc: `~/.gstack/projects/support-tickets/david-main-design-20260602.md`;
Phase 2 plan: `docs/phase-2-rough-draft.md`).

Scope decision: this is a learning project. The async queue / DI / metrics arm
is kept deliberately as curriculum, even though a keyword-heuristic workload
would not justify a queue in production. Fix the real traps; keep the learning.

Phase 2 decisions (autoplan 2026-06-02): optimistic locking is **in** Phase 2;
`event_type` uses **VARCHAR+CHECK** (the teachable migration); **CLOSED is
terminal** (reopen via RESOLVED→IN_PROGRESS); **`/assign` is deferred** (keep
`actor_id` nullable now). DI = native FastAPI `Depends()` (no dishka).

## Phase 2a — Pre-req fixes + migration (land as its own commit first)

- [x] **Rename the colliding exception.** `app/errors.py:22` `ValidationError` shadows Pydantic's `ValidationError`. Rename to `TicketValidationError`.
- [x] **Bound `description`.** Add `max_length` (~20_000) to `description` in `CreateTicketRequest` (`schemas.py:16`, currently unbounded `Text` — a client can POST a multi-MB body).
- [x] **`lazy="raise"` on `Ticket.events`** (`models.py:45`) + `selectinload(Ticket.events)` where needed. Async SQLAlchemy raises `MissingGreenlet` on implicit lazy access. Note: do NOT add `events` to `TicketResponse` without a `selectinload`.
- [x] **`TicketEvent` reshape.** Add `actor_id: int | None` (FK agents, nullable) and `field_changed: str | None`. Store `event_type` as **VARCHAR + CHECK** (Python `EventType` enum in app code: `CREATED`, `STATUS_CHANGED`, `PRIORITY_CHANGED`; `ASSIGNED` lands with `/assign` later). Existing native enums stay as documented known-debt.
- [x] **`ON DELETE SET NULL` on BOTH agent FKs.** `assigned_agent_id` (`models.py:31`) *and* the new `actor_id` — keep tickets/audit history when an agent is deleted.
- [x] **Migration must be exercised.** conftest currently builds schema via `Base.metadata.create_all` (`conftest.py:28`), so migrations are never tested. Add an `alembic upgrade head` + `downgrade` test against a scratch DB. The `String(50)`→CHECK change needs a **defensive data backfill** (existing lowercase `"created"` won't match the constraint); update `test_models.py:48` to the canonical value.

## Phase 2b — Layers + endpoints (do after 2a is green)

- [x] **Layers.** `TicketRepository` (queries, `add`/`flush`, no commit) + `TicketService` (business logic, owns the single `commit` = unit-of-work boundary). Wire with native FastAPI `Depends()`.
- [x] **`get_session` rollback.** `database.py:33` has no commit/rollback in the dependency. Add try/except → `session.rollback()` on exception, re-raise.
- [x] **Create-path `refresh` (must-fix — currently a guaranteed 500).** `created_at`/`updated_at` are `server_default=func.now()`; after `commit()` they're unpopulated on the returned object → `TicketResponse` validation fails. `await session.refresh(ticket)` after commit; test the timestamps are present in the 201 body.
- [x] **Status-transition state machine.** `ALLOWED_TRANSITIONS` with **CLOSED terminal** (`set()`), reopen via RESOLVED→IN_PROGRESS. Use `ALLOWED_TRANSITIONS.get(status, set())` (no `KeyError` 500). Same-state PATCH (e.g. OPEN→OPEN) = idempotent **200** no-op. Illegal → `InvalidStatusTransitionError` → 409; detail names from/to/allowed.
- [x] **Optimistic locking (concurrency).** `version_id_col` (or `WHERE updated_at = :seen` guard) on the status update; **409** on mismatch + a concurrent-transition test (two race; one wins, one 409s). Makes the audit trail provably correct, not just atomic.
- [x] **Transactional audit trail.** Write the `TicketEvent` in the SAME transaction as the ticket update. Atomicity test must verify **both-or-neither from a separate connection** (fail the event insert after the ticket flush; assert the ticket status also rolled back). Needs a second independent-session fixture in conftest.
- [x] **List/filter/pagination contract.** `list(status, priority, category, skip, limit) -> (items, total)` with a real post-filter `count()`. `limit` `Field(ge=1, le=100)` default 20; `skip` `Field(ge=0)`. Order `created_at DESC` (matches `idx_status_created`).
- [x] **Errors + envelope + OpenAPI.** `{detail, error_type}` handlers in `main.py`; **normalize** the Pydantic 422 via a `RequestValidationError` handler (keep per-field errors under a stable key). `error_type` = closed-set **constants** (`ticket_not_found`, `invalid_status_transition`, `validation_error`; `agent_not_found` with `/assign`), NOT `__class__.__name__`. Register a base `TicketError`/catch-all so the envelope is guaranteed. Declare `status_code=`, a shared `ErrorEnvelope` response_model on 404/409/422, and request `examples` so `/docs` is self-documenting.
- [x] **Case-insensitive enum input.** Accept `"open"`/`"OPEN"` on input, echo canonical casing in responses; 422 lists valid values. (Cuts the most common first-call failure.)
- [x] **Don't over-mock service tests.** Test logic the service owns (transitions, event emission), not Pydantic field validation through a mock.

## Phase 3 — Background processing (arq + DistilBART summarizer)

Design doc: `~/.gstack/projects/DavidMiserak-support-tickets/david-feat-bg-processing-design-20260602-165317.md`
Test plan: `~/.gstack/projects/DavidMiserak-support-tickets/david-feat-bg-processing-test-plan-20260602-171151.md`

- [x] **arq + Redis + compose** — NoopSummarizer first; transformer in same PR (Approach C chosen).
- [x] **SUMMARIZED event** — `EventType.SUMMARIZED` + migration 0004 (CHECK constraint update; include ASSIGNED in constraint even though endpoint is deferred).
- [x] **BackendRegistry singleton** — `initialize_backend()` called in `on_startup`; `get_initialized_backend()` called in task. Fallthrough skips already-attempted backend.
- [x] **TransformerSummarizer** — `sshleifer/distilbart-cnn-6-6`; `load_model()` in `on_startup`; `run_in_executor` for sync pipeline; `truncation=True` on pipeline call; try/except around `load_model()` with NoopSummarizer fallback.
- [x] **Session isolation** — `ctx["session_factory"]` injected in `on_startup`; tasks use `ctx["session_factory"]`, not module-level import directly; enables test injection.
- [x] **Structured logging** — log start, complete, not-found, summarizer error, backend selected.
- [x] **Session acquired AFTER summarization** — `summary = await summarizer.summarize(text)` first, THEN `async with ctx["session_factory"]() as session`. Prevents connection pool exhaustion under load.
- [x] **`stub_arq_pool` fixture** — `autouse=False`, no `app` param (use module-level import). `conftest.py` teardown uses `.pop(get_arq_pool, None)` not `.clear()`.
- [x] **`settings.redis_url`** — add to `app/config.py` before introducing worker code.
- [x] **Docker model pre-download** — default `SUMMARIZER_BACKEND=noop` in compose.yaml worker service.
- [x] **`_job_id` dedup** — summarize-at-create, best-effort once; log when `enqueue_job` returns None (already deduped).
- [x] **max_tries=1** — no automatic retry on failure; pairs with the one-shot enqueue policy above.
- [x] **Test gaps to add** — `load_model()` failure, pipeline ValueError, enqueue failure → 201, dedup logging, migration 0004 downgrade.
- [x] **assign_priority task** — heuristic keyword upgrade (never downgrades); writes PRIORITY_CHANGED; migration 0005.
- [x] **detect_spam task** — phrase + URL heuristics; writes SPAM_FLAGGED event; human reviews flagged tickets.
- [x] **route_ticket task** — category → department mapping; writes ROUTED event.

## Deferred from Phase 3 review (autoplan 2026-06-02)

- **AnthropicSummarizer** — Phase 4. Plugs into `_BACKENDS` dict + `SUMMARIZER_BACKEND=anthropic`.
- **Summary inline in GET /tickets/{id}** — Open question; requires join on ticket_events.
- **Connection math documentation** — ~~pool_size=20+10 × 2 processes = 60 connections; document ceiling before multi-worker deploy.~~ Done: README “Connection budget”.
- **`async_session` → `async_session_factory` rename** — standalone PR, not bundled with Phase 3 worker. Do it separately with grep to catch all call sites.

## Data model / scale (note now, fix when relevant)

- [x] ~~**Native PG ENUM vs VARCHAR+CHECK.**~~ Resolved (autoplan): new `event_type` uses VARCHAR+CHECK (Phase 2a). Existing native enums (`TicketStatus`/`Priority`/`Category`) stay as documented known-debt — revisit only if an `ALTER TYPE` becomes painful.
- [x] ~~**`assigned_agent_id` has no `ON DELETE` rule.**~~ Resolved: `SET NULL` on both agent FKs (Phase 2a).
- [x] ~~**Concurrent updates / optimistic locking.**~~ Pulled into Phase 2b (version/`updated_at` guard + concurrent test).
- [x] **Connection math.** ~~`pool_size=20 + max_overflow=10` = 30/process vs Postgres default `max_connections=100`. Document the ceiling before running API + worker + multiple uvicorn workers.~~ Documented in README “Connection budget”.

## DX / tooling fixes

- [x] **`make run` defaults to podman.** Auto-detect (`command -v podman || command -v docker`) or default to docker — docker-only evaluators currently hit "podman: command not found." (Fold into Phase 2 — it gates clone→first-call.)
- [x] **`.env.example` default uses `db:5432`.** Footgun for local (non-container) runs. Ship `localhost` as default, comment the container override. (Fold into Phase 2.)
- [x] **README endpoint docs.** Add copy-paste curl for `POST /tickets`, `GET /tickets/{id}`, `GET /tickets?status=OPEN`, `PATCH /tickets/{id}/status`, plus the error envelope + `error_type` table; tick the roadmap. (Phase 2 — first real API surface.)
- [x] **`make test` fallback masks errors.** `container-test || local-test` hides the real container failure behind a confusing local one. Make the fallback explicit.
- [x] **`make seed` references a missing script.** ~~`python -m scripts.seed` doesn't exist.~~ Done: `scripts/seed.py` seeds agents idempotently by email until `/assign` lands.

## Phase 4 — Observability (structured logging, Prometheus metrics)

Plan: `docs/phase-4-rough-draft.md` (approved autoplan 2026-06-02)

- [x] `app/logging_config.py` — `setup_logging()` + `RequestIdFilter` + `LOG_LEVEL` validation; call at module level in `main.py` and `worker/main.py`
- [x] `app/metrics.py` — `tickets_created_total`, `ticket_status_transitions_total`, `ticket_summarization_outcomes_total` (delta-assertion tests)
- [x] `CorrelationIdMiddleware` (outermost) + `prometheus-fastapi-instrumentator` (inner) in `app/main.py`; pass `validator=is_valid_uuid`
- [x] Business counter increments in `TicketService.create_ticket` and `update_status`
- [x] Pass correlation ID as explicit arq job arg; restore in `summarize_ticket`
- [x] Redis health probe in `GET /health`; report `{"status":..., "database":..., "redis":...}`
- [x] `elapsed_seconds` in worker complete/failure log records
- [x] `LOG_LEVEL` in `Settings`, compose.yaml (api + worker), README env table
- [x] README observability quickstart section + `/metrics` counter names documented
- [x] Pin new dep versions in `requirements.txt`

## Phase 4b — AnthropicSummarizer backend

Deferred from Phase 4 (not an observability concept — ships as standalone PR).

- [ ] `app/worker/backends/anthropic.py` — `AnthropicSummarizer` using `anthropic` SDK
- [ ] Register `"anthropic"` in `BackendRegistry._BACKENDS`
- [ ] `ANTHROPIC_API_KEY: str | None` in `Settings`
- [ ] `requirements-optional.txt` for `anthropic` dep (mirrors `requirements-ml.txt`)

## Phase 5 — Observability hardening (deferred from Phase 4 review)

Plan: `docs/phase-5-rough-draft.md` (APPROVED autoplan 2026-06-03)

- [x] **`/health` Redis-degraded-to-503 causes container restart loop.** Fixed:
  `/health` is now a pure liveness probe (200 unconditionally); Redis and DB
  probes moved to new `/ready` readiness endpoint. Docker HEALTHCHECK targets
  `/health` so Redis blips no longer trigger container restarts.
- [x] **X-Request-ID format inconsistency.** Fixed: added
  `generator=lambda: str(uuid4())` to `CorrelationIdMiddleware`; server-generated
  IDs are now hyphenated UUID4 matching client-supplied IDs.
- [x] **`/health` and `/ready` have no timeouts on DB/Redis probes.** Fixed:
  wrapped both probes in `/ready` with `asyncio.wait_for(..., timeout=2.0)`.
- [ ] **SQLAlchemy pool state under `asyncio.wait_for` cancellation storms.**
  When `/ready` times out, `asyncio.wait_for` cancels the `check_database_connection()`
  coroutine mid-flight. If the coroutine had acquired a pool connection before
  the cancel, that connection may not be returned cleanly, leaking connections
  under sustained timeouts. Client disconnects (which send `CancelledError` to
  the request task) are an additional trigger for the same pool-drain path.
  Fix: add `connect_args={"timeout": 1.5}` to the `create_async_engine` call
  in `database.py` so asyncpg enforces its own connection timeout before
  `wait_for` cancels it. Deferred — low risk for current single-instance deployment.
- [ ] **`/ready` probes run sequentially (2+2=4s worst case).** Under dual-degraded
  conditions (both DB and Redis have half-open TCP), each `await asyncio.wait_for(...)`
  yields to the loop but the handler may not finish for up to 4 seconds, delaying
  that readiness response (other requests can still run). Running both probes
  concurrently with `asyncio.gather` would reduce worst-case latency to 2 seconds.
  Deferred — only matters under simultaneous dual-failure which is rare in practice.

## Deferred from Phase 7 (polish) review

- [ ] **`PATCH /tickets/{id}/status` returns flat `TicketResponse` (no events).**
  `GET /tickets/{id}` returns `TicketDetailResponse` with `events`. The asymmetry
  is intentional to avoid a second DB query (selectinload) on every PATCH. To
  fix: reload ticket with `selectinload(Ticket.events)` after commit in
  `TicketService.update_status`, or add a `get_with_events` helper to the repo.

- [x] **Assign-agent REST endpoint.** `PATCH /tickets/{id}/assign` sets
  `assigned_agent_id`, validates agent exists (404 `agent_not_found`), writes
  `ASSIGNED` event, returns `TicketResponse`.

- [ ] **Makefile `container-up` exits silently on build failure.** If the build
  fails or a port is in use, `compose up --build -d` exits 0 with no visible
  error. Fix: append `|| (echo "Run 'make worker-logs' or 'make container-logs'
  to debug" && exit 1)` to the `container-up` target.

- [ ] **Events endpoint as an alternative.** If strict REST sub-resource design
  is preferred over embedding events in `TicketDetailResponse`, add
  `GET /tickets/{id}/events` returning `list[TicketEventResponse]` with
  pagination. The repo method and schema already exist.

## Assessment gaps (deferred)

- [ ] **Authentication.** All endpoints are unauthenticated. In production: JWT
  bearer tokens verified via a FastAPI `Depends` guard, with the resolved agent
  identity threaded through as `actor_id` on audit events. API-key middleware is
  a simpler alternative for server-to-server callers. Documented in README.

- [ ] **Rate limiting.** `POST /tickets` is unbounded. Add per-IP or per-key
  limits via `slowapi` (wraps `limits`/`redis`; integrates with FastAPI
  middleware) or an upstream proxy (nginx, Traefik). Without Redis for state the
  simplest option is a fixed-window in-process limiter.

- [ ] **Full-text search on `GET /tickets`.** The list endpoint filters by
  status/priority/category but not subject or description. Options: PostgreSQL
  `tsvector`/`tsquery` (add a generated column + GIN index in a migration) or
  `ILIKE` as a quick approximation. Neither requires a new service.

- [ ] **Migration locking.** Migrations 0004 and 0005 use `DROP CONSTRAINT` +
  `ADD CONSTRAINT CHECK` on `ticket_events`, which takes `ACCESS EXCLUSIVE` for
  the full duration and stalls writes. For future event-type additions, split
  into `ADD CONSTRAINT ... NOT VALID` (no lock) followed by `VALIDATE CONSTRAINT`
  (only `SHARE UPDATE EXCLUSIVE`) in a separate transaction.

- [ ] **Worker task boilerplate.** The four tasks (`summarize_ticket`,
  `assign_priority`, `detect_spam`, `route_ticket`) repeat ~15 lines of identical
  scaffold (set correlation ID, start timer, fetch ticket, guard on None, log
  complete). Extract a shared async context manager or decorator that handles
  the scaffold and yields the ticket, reducing each task to its domain logic.

## Roadmap (from design doc)

- [x] Phase 1 — foundation: models, migrations, config, `/health`
- [x] Phase 2 — ticket CRUD API
- [x] Phase 3 — background worker (arq)
- [x] Phase 4 — observability (structured logging, metrics)
- [ ] Phase 4b — AnthropicSummarizer backend (standalone PR)
- [x] Phase 5 — observability hardening (liveness/readiness split, UUID4 fix, probe timeouts)
- [x] Phase 6 — Docker/deploy polish
- [x] Phase 7 — worker results visible in API response (`TicketDetailResponse`)
