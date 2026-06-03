<!-- /autoplan restore point: ~/.gstack/projects/DavidMiserak-support-tickets/develop-autoplan-phase2-restore-20260602-124118.md -->
# Phase 2 — Ticket CRUD API (ROUGH DRAFT)

Status: REVIEWED (autoplan 2026-06-02, subagent-only) — APPROVED with 4 decisions
settled. See "RESOLVED DECISIONS" at the bottom; review report above the audit trail.
Branch: develop
Depends on: Phase 1 (foundation) — done.
Source: design doc `~/.gstack/projects/support-tickets/david-main-design-20260602.md`
+ the Phase 2 traps already in `TODOS.md`.

---

## Goal

Ship a working, well-tested REST CRUD API for tickets: create, read, list (with
filtering + pagination), and status updates — with a transactional audit trail
and a real status-transition state machine. No background worker yet (Phase 3),
no auth (deferred), no metrics (Phase 4).

A developer should be able to `POST /tickets`, `GET /tickets/{id}`,
`GET /tickets?status=OPEN`, and `PATCH /tickets/{id}` against a running container
and get correct, validated, well-shaped JSON back.

---

## In scope

- `TicketRepository` (data access) + `TicketService` (business logic) layers.
- Endpoints: create, get-by-id, list (filter + paginate), update status,
  (optional) assign agent.
- Status-transition state machine with explicit allowed moves.
- Transactional `TicketEvent` audit trail (written in the same transaction as
  the ticket mutation).
- Consistent error envelope + exception handlers.
- Pre-req cleanups to Phase-1 code (see "Pre-req fixes" — these are bugs/traps
  that will bite Phase 2 if left).
- Tests: service unit tests (mocked repo), repository tests (real DB),
  API integration tests.

## Out of scope (defer)

- Background processing / task queue / worker — **Phase 3**.
- Structured logging, correlation IDs, metrics — **Phase 4**.
- Auth (agent or customer) — deferred per design doc §"Open Questions".
- `Customer` table — staying with the denormalized snapshot on `Ticket`
  (deliberate; revisit when a customer-level feature lands).
- `dishka` DI container — see Open Decision D2; rough draft assumes native
  FastAPI `Depends()`.

---

## Architecture for this phase

Layering, matching the design doc:

```
POST/GET/PATCH /tickets   (api/tickets.py — route handlers, HTTP <-> DTO)
        │
        ▼
   TicketService          (services/ticket.py — business rules, transitions,
        │                  audit events, transaction boundary)
        ▼
  TicketRepository         (repositories/ticket.py — all SQLAlchemy queries)
        │
        ▼
   Ticket / TicketEvent    (models.py — ORM, already exists)
```

Wiring: FastAPI `Depends()`. `get_session` (exists in `database.py:33`) →
repo `Depends` → service `Depends`. Transaction boundary owned by the service:
service does the work, commits once at the end; the route does not commit.

---

## Pre-req fixes (do first — these are real Phase-1 traps)

Referenced against current code:

1. **Rename colliding exception.** `app/errors.py:22` `ValidationError` shadows
   Pydantic's `ValidationError`. Rename → `TicketValidationError`. Update the
   design-doc snippets that raise it.
2. **Bound `description`.** `app/schemas.py:16` `description` is unbounded
   (`min_length=1` only) and maps to `Text`. Add `max_length` (propose 20_000)
   so a client can't POST a multi-MB body.
3. **`lazy="raise"` on `Ticket.events`.** `app/models.py:45`. Async SQLAlchemy
   throws `MissingGreenlet` on implicit lazy load — make it explicit and load
   via `selectinload(Ticket.events)` only where needed. Surfaces the footgun at
   dev time instead of in prod.
4. **`assigned_agent_id` `ON DELETE` rule.** `app/models.py:31-33` has no
   ondelete. Deleting an `Agent` with assigned tickets errors. Decide
   `SET NULL` (recommended — unassign, keep the ticket) and add it in a new
   Alembic migration.
5. **`TicketEvent` shape.** `app/models.py:59-61`: `event_type` is a loose
   `String(50)`. Tighten to an enum (`STATUS_CHANGED`, `ASSIGNED`, `CREATED`,
   `PRIORITY_CHANGED`). Add `actor_id: int | None` (FK agents, nullable —
   system/customer actions have no agent) and `field_changed: str | None`.
   New Alembic migration.

---

## Endpoints / API contract

| Method | Path | Body | Success | Notes |
|--------|------|------|---------|-------|
| POST | `/tickets` | `CreateTicketRequest` | 201 `TicketResponse` | status defaults OPEN; writes a `CREATED` event |
| GET | `/tickets/{id}` | — | 200 `TicketResponse` | 404 if missing |
| GET | `/tickets` | query params | 200 `ListTicketsResponse` | filter + paginate (below) |
| PATCH | `/tickets/{id}/status` | `{status}` | 200 `TicketResponse` | runs state machine; writes `STATUS_CHANGED` event |
| PATCH | `/tickets/{id}/assign` | `{agent_id}` | 200 `TicketResponse` | 404 if agent missing; writes `ASSIGNED` event |

Schemas (`schemas.py`) — mostly exist:
- `CreateTicketRequest` ✓ (add `max_length` to `description`).
- `TicketResponse` ✓.
- `ListTicketsResponse` ✓ (`items`, `total`, `skip`, `limit`).
- Add `UpdateStatusRequest { status: TicketStatus }`,
  `AssignAgentRequest { agent_id: int }`.

---

## Status-transition state machine

In the service, validate before any status write:

```python
ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.OPEN:        {TicketStatus.IN_PROGRESS, TicketStatus.CLOSED},
    TicketStatus.IN_PROGRESS: {TicketStatus.RESOLVED, TicketStatus.OPEN, TicketStatus.CLOSED},
    TicketStatus.RESOLVED:    {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},  # reopen
    TicketStatus.CLOSED:      set(),  # terminal — no transitions out
}
```

Illegal move → raise `InvalidStatusTransitionError` (`errors.py:16`) → 409.
Explicit calls to settle: **CLOSED is terminal** (no reopen); **RESOLVED→IN_PROGRESS
is the reopen path**. (These two are the decisions to confirm in review.)

---

## Audit trail (transactional)

The `TicketEvent` MUST be written in the **same transaction** as the ticket
mutation, or the log can lie. Pattern in the service:

```python
async def update_status(self, ticket_id, new_status, actor_id=None):
    ticket = await self.repo.find_by_id(ticket_id)         # selectinload events if needed
    if ticket is None:
        raise TicketNotFoundError(ticket_id)
    if new_status not in ALLOWED_TRANSITIONS[ticket.status]:
        raise InvalidStatusTransitionError(...)
    old = ticket.status
    ticket.status = new_status
    self.repo.add_event(TicketEvent(
        ticket_id=ticket.id, event_type=EventType.STATUS_CHANGED,
        field_changed="status", previous_value=old.value,
        new_value=new_status.value, actor_id=actor_id,
    ))
    await self.session.commit()   # one transaction: ticket + event
    return ticket
```

Repo `add()`/`add_event()` use `session.add` + `flush` (no commit). Service owns
the single `commit`.

---

## List / filter / pagination contract

`repo.list(status, priority, category, skip, limit) -> (items, total)`:
- Optional filters: `status`, `priority`, `category` (any combination).
- `skip` ≥ 0, `limit` capped at 100 (`Field(le=100)` on the query param,
  default 20).
- Real `count()` query for `total` (the `ListTicketsResponse` schema requires it).
  Two queries (page + count) is fine for this scale.
- Order: `created_at DESC` (matches `idx_status_created` in `models.py:47`).

---

## Error handling / envelope

Register handlers in `app/main.py` (currently bare — `main.py:1-22`):
- `TicketNotFoundError` → 404
- `InvalidStatusTransitionError` → 409
- `TicketValidationError` → 422
- envelope: `{"detail": str, "error_type": str}`
- **Document the 422 mismatch:** FastAPI/Pydantic request validation returns
  `detail` as a *list*, not a string. Either normalize via a
  `RequestValidationError` handler or document the two shapes. Pick one in review.

---

## Files touched

New:
- `app/repositories/__init__.py`, `app/repositories/ticket.py`
- `app/services/__init__.py`, `app/services/ticket.py`
- `app/api/__init__.py`, `app/api/tickets.py` (router; mount in `main.py`)
- `app/tests/test_services/test_ticket_service.py`
- `app/tests/test_repositories/test_ticket_repo.py`
- `app/tests/test_api/test_tickets.py`
- new Alembic migration (TicketEvent shape + agent ondelete)

Modified:
- `app/errors.py` (rename + add handlers' exceptions)
- `app/schemas.py` (bound description; add Update/Assign requests)
- `app/models.py` (lazy=raise; TicketEvent fields/enum; agent ondelete)
- `app/enums.py` (add `EventType`)
- `app/main.py` (mount router; register exception handlers)

---

## Test plan

- **Service (unit, mocked repo):** every legal transition passes; every illegal
  transition raises; CLOSED is terminal; create emits a `CREATED` event; audit
  event written on each mutation.
- **Repository (real DB):** create→find round-trip; list filters (each + combos);
  pagination boundaries (skip past end → empty + correct total); count accuracy.
- **API (integration):** 201 on create; 404 on missing get; 409 on illegal
  transition; 422 on bad body (+ assert the envelope shape); list query params;
  `total`/`skip`/`limit` correct.
- **Transactional audit:** force a failure after the ticket mutation but before
  commit; assert NO event row persisted (proves same-transaction guarantee).

---

## Open decisions for review

- **D1 — CLOSED terminal vs reopen.** Draft says CLOSED is terminal, reopen goes
  through RESOLVED→IN_PROGRESS. Confirm.
- **D2 — DI: native `Depends()` vs `dishka`.** Design doc names dishka; draft
  uses native `Depends()` (simpler, fewer deps, enough for 3 layers). Decide.
- **D3 — 422 envelope.** Normalize Pydantic validation errors to the
  `{detail, error_type}` shape, or document the divergence? Draft leans normalize.
- **D4 — assign endpoint in Phase 2** or defer to a later phase? Draft includes it
  (cheap, exercises the agent FK + ondelete decision).
- **D5 — agent `ON DELETE`.** Draft recommends `SET NULL`. Confirm vs `RESTRICT`.

---

<!-- AUTONOMOUS DECISION LOG -->
# GSTACK /autoplan REVIEW REPORT

Voices: **subagent-only** (Codex binary not installed — dual-voice degraded).
Base branch: `main`. Branch: `develop`.

## Phase 1 — CEO / Strategy

### CEO Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Premises valid?                    YES     [unavailable]  YES (1 voice)
  2. Right problem to solve?            YES     [unavailable]  YES (1 voice)
  3. Scope calibration correct?        MOSTLY  [unavailable]  MOSTLY — 2 adjustments
  4. Alternatives explored?            PARTIAL [unavailable]  enum choice unresolved
  5. Learning-per-effort sound?        YES     [unavailable]  YES (1 voice)
  6. 3-month trajectory sound?         AT RISK [unavailable]  audit trail not concurrency-safe
```

### CEO findings (severity)
- **HIGH — Audit trail not concurrency-safe.** Headline feature is a *trustworthy*
  audit trail, but optimistic locking is deferred (TODOS). Two concurrent
  `PATCH /status` lost-update → atomic-but-wrong log. Fix: version/`updated_at`
  guard + concurrent-transition test (~15 lines). → SCOPE DECISION at gate.
- **MEDIUM — Atomicity test gives false confidence.** "force failure before commit,
  assert no event" passes trivially (single commit). Must assert both-or-neither
  from a *separate* connection. → auto-decided: strengthen test.
- **MEDIUM — Session lifecycle premise unstated.** `get_session` (`database.py:33`)
  has no commit/rollback in `finally`. → auto-decided: rollback-on-exception in
  dependency; service owns the explicit commit.
- **MEDIUM — Native PG enum vs VARCHAR+CHECK unresolved** (TODOS line 29; design-doc
  SQL uses VARCHAR+CHECK, models use native Enum). EventType migration doubles down.
  → TASTE DECISION at gate.
- **MEDIUM — Pre-req migrations bundled with layer work.** → auto-decided: split into
  step 2a (fixes + migrations) and 2b (layers).
- **LOW — `/assign` is scope creep.** → SCOPE DECISION at gate (keep `actor_id`
  nullable regardless).
- **LOW — 422 envelope (D3):** normalize. → auto-decided.
- **LOW — Don't over-mock service tests** (test logic the service owns, not Pydantic).
  → auto-decided: accept.
- **LOW — D2 DI:** native `Depends()` already settled. → auto-decided: close D2.

## Phase 3 — Engineering

### Eng Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Architecture sound?               YES     [unavailable]  YES (1 voice)
  2. Test coverage sufficient?         NO      [unavailable]  NO — infra gaps
  3. Concurrency handled?              NO      [unavailable]  NO — lost update
  4. Async session lifecycle correct?  AT RISK [unavailable]  rollback + refresh missing
  5. Migration safe?                   NO      [unavailable]  enum cast + actor_id ondelete
  6. Create path correct?              NO      [unavailable]  CRITICAL: server-default 500
```

### Eng findings (severity)
- **CRITICAL — Create path 500s on server-default timestamps.** `created_at`/
  `updated_at` are `server_default=func.now()` (`models.py:35-43`); after
  `commit()` the returned object has them unpopulated → `TicketResponse`
  validation fails → 500. Fix: `await session.refresh(ticket)` (or RETURNING) in
  create path + test asserting timestamps in 201 body. → auto-decided: include.
- **HIGH — Lost update on status transitions.** read-modify-write window; audit
  trail records contradictory history. → SCOPE DECISION (optimistic locking).
- **HIGH — `get_session` no rollback.** → auto-decided: try/except rollback in dep.
- **HIGH — conftest bypasses Alembic (`create_all`) + no second-connection
  fixture.** Migrations never exercised; atomicity test impossible to write.
  → auto-decided: add `alembic upgrade head/downgrade` test + independent session
  fixture.
- **HIGH — `event_type` String→enum migration needs data backfill** (existing
  lowercase `"created"` won't cast; `test_models.py:48` needs update).
  → auto-decided: defensive backfill + test the migration.
- **MEDIUM — State machine `KeyError` + no same-state rule.** → auto-decided:
  `.get(status, set())`, test every enum is a key, same-state = idempotent 200.
- **MEDIUM — `actor_id` FK needs its own `ON DELETE SET NULL`.** → auto-decided.
- **MEDIUM — pagination:** `total` post-filter; `skip` `ge=0`, `limit` `ge=1`.
  → auto-decided.
- **MEDIUM — service↔session coupling:** service reaches around repo to commit.
  → auto-decided: document service owns unit-of-work; both receive the session.
- **LOW — misleading `selectinload` comment** in update_status (never reads
  events). → auto-decided: remove; add note "don't put events in TicketResponse
  without selectinload".

## Phase 3.5 — Developer Experience (REST API)

### DX Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Endpoints RESTful/guessable?      YES     [unavailable]  YES (keep /status,/assign split)
  2. Status codes correct?             MOSTLY  [unavailable]  distinct 404s needed
  3. Error envelope actionable?        NO      [unavailable]  error_type vocab undocumented
  4. Self-documenting (/docs)?         NO      [unavailable]  no examples/responses
  5. README/clone-to-first-call?       NO      [unavailable]  no curl, podman wall
  6. Request-contract friction?        SOME    [unavailable]  all-caps enums, required category
```
DX score: **6/10** as specified → ~8.5/10 after high-severity fixes. TTHW: 8-12 min
(podman) / 20-30+ min (docker-only) → ~5 min after fixes.

### DX findings (severity)
- **HIGH — `error_type` vocabulary undocumented.** → auto-decided: closed set of
  constants (`ticket_not_found`, `agent_not_found`, `invalid_status_transition`,
  `validation_error`), not `__class__.__name__`; document in README + OpenAPI.
- **HIGH — 422 divergence (D3) unresolved in body.** → auto-decided: normalize via
  `RequestValidationError` handler, keep per-field errors under a stable key.
- **HIGH — Not self-documenting.** → auto-decided: `status_code=`, shared
  `ErrorEnvelope` response_model on 404/409/422, request `examples`.
- **HIGH — README has no endpoint docs.** → auto-decided: add curl examples +
  error_type table; tick roadmap.
- **MEDIUM — distinct 404 (ticket vs agent).** → auto-decided.
- **MEDIUM — all-caps enum casing friction.** → auto-decided: accept
  case-insensitive enum input, echo canonical casing; 422 lists valid values.
- **MEDIUM — `make run` podman wall + `.env.example` `db:5432`.** → auto-decided:
  autodetect podman/docker; ship `localhost` default. (blast-radius, cheap)
- **MEDIUM — `make seed`/`scripts/seed.py` missing; `/assign` needs an agent.**
  → tied to SCOPE DECISION on `/assign`.
- **MEDIUM — catch-all `TicketError`/500 handler** to guarantee envelope.
  → auto-decided: register base handler.
- **MEDIUM — 409 detail must name from/to/allowed.** → auto-decided.

## Cross-Phase Themes
- **Trustworthy audit trail vs concurrency** — CEO + Eng independently. Highest-
  confidence signal. Drives the optimistic-locking scope decision.
- **Atomicity test proves nothing** — CEO + Eng. Fixed (auto-decided).
- **Native enum vs VARCHAR+CHECK** — CEO + Eng. → taste decision.
- **State-machine completeness (same-state / KeyError)** — Eng + DX. Fixed.

## Decision Audit Trail
| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | CEO | Split pre-req migrations (2a) from layers (2b) | Mech | P3/P5 | Cleaner diffs, isolates migration lesson |
| 2 | CEO/Eng | Strengthen atomicity test (separate connection, both-or-neither) | Mech | P1 | Test must fail for the right reason |
| 3 | Eng | `get_session` rollback-on-exception | Mech | explicit | Correctness of txn boundary |
| 4 | Eng | **Create path `session.refresh()` + timestamp test** | Mech | P1 | Fixes a guaranteed 500 |
| 5 | Eng | State machine `.get(status,set())` + enum-coverage test | Mech | P1 | Avoid 500 on future enum add |
| 6 | Eng/DX | Same-state transition = idempotent 200 | Taste→auto | P6 | Friendlier, two-way door |
| 7 | Eng | pagination `total` post-filter; skip ge=0, limit ge=1 | Mech | P1 | Correctness |
| 8 | Eng | `actor_id` FK `ON DELETE SET NULL` | Mech | P1 | Keep audit history |
| 9 | Eng | event_type migration: data backfill + alembic test | Mech | P1 | Cast fails on lowercase rows |
| 10 | Eng | conftest: add migration test + second-conn fixture | Mech | P1 | Migrations currently untested |
| 11 | DX | 422 normalize, keep per-field errors | Mech | P1 | One error shape |
| 12 | DX | error_type closed-set constants, documented | Mech | P1 | Clients need stable vocab |
| 13 | DX | OpenAPI status_code + ErrorEnvelope + examples | Mech | P1 | Self-documenting |
| 14 | DX | README curl + error_type table; tick roadmap | Mech | P1 | First API surface front door |
| 15 | DX | base TicketError/500 catch-all handler | Mech | P1 | Guarantee envelope |
| 16 | DX | distinct ticket vs agent 404 | Mech | P1 | Actionable errors |
| 17 | DX | case-insensitive enum input | Mech | P6 | Cuts first-call friction |
| 18 | DX | Makefile podman/docker autodetect + .env localhost | Mech | P2 | Blast-radius, unblocks TTHW |
| 19 | CEO | Close D2: native `Depends()` (no dishka) | Mech | P4/P5 | Already settled, no dup |
| 20 | Eng | Document service owns unit-of-work | Mech | P5 | Resolve coupling ambiguity |

## Surfaced for your decision (taste / scope)
- **S1 (scope, reverses TODOS) — Optimistic locking IN Phase 2?** CEO+Eng both
  HIGH. Recommend IN.
- **S2 (taste) — `EventType` as VARCHAR+CHECK vs native PG enum?** Recommend
  VARCHAR+CHECK for the new column (teaching-migrations goal).
- **S3 (taste) — D1: CLOSED terminal?** Recommend terminal (reopen via RESOLVED).
- **S4 (scope) — D4: `/assign` in Phase 2 or defer?** Recommend defer; keep
  `actor_id` nullable now.

## Implementation Tasks (aggregated)
- [ ] **P0 — Create-path refresh** — `session.refresh()` after commit; test timestamps in 201. (eng)
- [ ] **P0 — `get_session` rollback** — try/except rollback in dependency. (eng)
- [ ] **P1 — Pre-req step 2a** — rename `ValidationError`→`TicketValidationError`; bound `description`; `lazy="raise"`; TicketEvent fields+enum/CHECK; both agent FKs `ON DELETE SET NULL`; migration + alembic upgrade/downgrade test + data backfill. (eng)
- [ ] **P1 — Layers step 2b** — repository, service (owns commit), router; mount + handlers. (eng)
- [ ] **P1 — State machine** — `.get(status,set())`, same-state 200, enum-coverage test. (eng)
- [ ] **P1 — Audit atomicity** — strengthen test (separate connection, both-or-neither); second-conn fixture. (eng)
- [ ] **P1 — Errors/OpenAPI** — normalize 422; error_type constants; ErrorEnvelope responses; examples; catch-all. (dx)
- [ ] **P1 — pagination guards** — total post-filter, skip ge=0, limit ge=1. (eng)
- [ ] **P2 — DX/tooling** — README curl + error_type table; Makefile podman/docker autodetect; `.env.example` localhost. (dx)
- [ ] **P2 — case-insensitive enum input.** (dx)
- [ ] *S1/S4 dependent* — optimistic locking + concurrent test; `/assign` + `scripts/seed.py`.

---

## RESOLVED DECISIONS (post-autoplan, 2026-06-02)

- **S1 — Optimistic locking: IN Phase 2.** Add `version_id_col` (or
  `WHERE updated_at = :seen` guard) on the status update; return **409** on
  mismatch. Add a concurrent-transition test (two transitions race; one wins, one
  409s). This makes the audit trail provably correct, not just atomic. Reverses
  the prior TODOS deferral — update TODOS "Data model / scale" accordingly.
- **S2 — `EventType` column: VARCHAR + CHECK** (not native PG enum). The new
  column is the teachable migration; existing native enums stay as documented
  known-debt. Keep `EventType` as a Python `enum.Enum` in app code, store as
  string, enforce with a CHECK constraint in the migration.
- **S3 — CLOSED is terminal.** `ALLOWED_TRANSITIONS[CLOSED] = set()`. Reopen path
  is RESOLVED→IN_PROGRESS only. Direct CLOSED→OPEN → 409.
- **S4 — `/assign` deferred** to a later phase. Drop the endpoint, the
  `AssignAgentRequest` schema, the `ASSIGNED` event path, and `scripts/seed.py`
  from Phase 2 scope. **Keep `actor_id` nullable** on `TicketEvent` now so the
  audit trail stays forward-compatible. (D5 agent `ON DELETE SET NULL` still
  applies to `assigned_agent_id` via the migration; the column just won't have a
  write path yet.)

### Net effect on scope
- IN: pre-req step 2a (fixes + migration, VARCHAR+CHECK for event_type, both agent
  FKs SET NULL, data backfill, alembic up/down test), layers step 2b
  (repo/service/router, service owns commit, `get_session` rollback,
  create-path `refresh`), status state machine (CLOSED terminal, same-state 200,
  `.get()` guard), **optimistic locking + concurrent test**, list/filter/paginate
  with guards, normalized errors + error_type constants + OpenAPI examples +
  README curl, case-insensitive enum input, Makefile podman/docker autodetect +
  `.env` localhost.
- OUT (deferred): `/assign` endpoint + `scripts/seed.py`, worker, auth, metrics,
  Customer table.

All open decisions D1–D5 are now closed (D1→S3, D2→native Depends, D3→normalize,
D4→S4 defer, D5→SET NULL).
