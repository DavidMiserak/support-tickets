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

## Roadmap (from design doc)

- [x] Phase 1 — foundation: models, migrations, config, `/health`
- [x] Phase 2 — ticket CRUD API
- [x] Phase 3 — background worker (arq)
- [x] Phase 4 — observability (structured logging, metrics)
- [ ] Phase 4b — AnthropicSummarizer backend (standalone PR)
- [ ] Phase 5 — full test suite
- [ ] Phase 6 — Docker/deploy polish
