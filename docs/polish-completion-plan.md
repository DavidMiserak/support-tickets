# Polish Completion Plan — support-tickets (feat/polish)

Branch: feat/polish | Target: main | Date: 2026-06-03

## Status: 92–95% Complete

The codebase fully satisfies the assignment spec for a runnable, production-style
learning project. All core deliverables are implemented. The remaining work is
portfolio-polish gaps, not missing fundamentals.

## Premise

The project is a Python backend assignment demonstrating:
- Ticket management REST API (FastAPI, PostgreSQL, async SQLAlchemy)
- Background async processing (arq + Redis: summary, priority, spam, routing)
- Observability (structured JSON logs, Prometheus metrics, correlation IDs)
- Production containerization (Docker Compose, liveness/readiness probes)
- Test coverage (~90+ async tests)

## Spec Checklist

| Area | Spec | Status |
|---|---|---|
| Stack | Python 3.12+, FastAPI, PostgreSQL, Docker Compose | Done |
| Create ticket | name, email, subject, description, priority, category | Done (POST /tickets) |
| Retrieve | by ID | Done (GET /tickets/{id}) |
| List | pagination + filter by status/priority/category | Done |
| Update status | OPEN → IN_PROGRESS → RESOLVED → CLOSED | Done (state machine + 409) |
| Engineering | schemas, types, enums, layers, config, logging, errors | Done |
| Database | tickets, events, optional agents, migrations, seed | Done (5 migrations) |
| Async queue | 4 tasks on create; persist results | Done (arq + Redis) |
| API design | REST, status codes, validation, error envelope, OpenAPI | Done |
| Docker | API + Postgres + worker + Redis, one command | Done (compose.yaml, make run) |
| README | overview, architecture, setup, env, API, tradeoffs | Done |

## Gaps to Close (proposal)

### Gap 1 — Agent assign endpoint (or document clearly out of scope)

**What:** The spec says "Agents to update ticket status." The `agents` table and
seed exist, and anyone can call `PATCH /tickets/{id}/status`, but there is no
`POST /tickets/{id}/assign` endpoint exposed over REST.

**Options:**
- A) Implement `PATCH /tickets/{id}/assign` — sets `assigned_agent_id`, validates
  agent exists, writes `ASSIGNED` event, returns updated ticket.
- B) Document explicitly in README that agent assignment is deferred (agents table
  exists for data model completeness; the spec's "agents" requirement is satisfied
  by the worker tasks that run on ticket creation).

### Gap 2 — Background results visibility in GET /tickets/{id}

**What:** Summaries, priority changes, spam flags, and routing decisions are stored
in `ticket_events`, but the `GET /tickets/{id}` response does not include them.

**Options:**
- A) Add `GET /tickets/{id}/events` endpoint — returns paginated audit trail.
- B) Add `latest_summary: str | None` and `events: list[TicketEventResponse]` to
  `TicketResponse` (requires `selectinload(Ticket.events)` in repo).
- C) Document current behavior clearly in README — results are internal-only and
  visible via the audit trail in the database.

### Gap 3 — README / TODOS.md sync

**What:** TODOS.md marks Phase 6 done; README roadmap still lists "Assign-agent
endpoint" as open while also marking worker tasks done.

**Fix:** Align README Phase 6 checkbox and roadmap section with TODOS.md state.
One-line change.

## Deferred (explicitly out of scope)

- Phase 4b — AnthropicSummarizer backend (not an assignment requirement)
- Phase 5 leftovers — SQLAlchemy pool under cancel storms, parallel /ready probes
  (documented, low risk for single-instance)
- Auth/authz — explicitly out of scope for learning project
- Multi-tenant operation, rate limiting, SLA enforcement

## Success Criteria

After this work, the repo should:
1. Satisfy every functional requirement in the assignment with no ambiguous gaps
2. Have README accurately reflect the implemented feature set
3. Either expose agent assignment or document clearly why it is deferred
4. Return enough ticket data to demonstrate "retrieve processing results"

---

## GSTACK REVIEW REPORT

### Phase 1 — CEO Review

**Mode:** SELECTIVE EXPANSION | **Branch:** feat/polish → main | **Voices:** Claude subagent [codex-unavailable]

#### 0A. Premise Challenge
Premises confirmed by user. Key shift: the plan was optimizing spec coverage; correct framing is grader-differentiation. A grader will `make run`, call a few endpoints, read the code — they won't score on spec checkbox count. The async queue is the project's strongest technical signal, and it was invisible from the GET response.

Premises accepted:
1. Take-home reviewed once by a technical grader.
2. Grader runs code first, README second.
3. "Agents to update ticket status" satisfied by worker tasks (no REST assign required).
4. Biggest visible gap: GET /tickets/{id} showed zero evidence of background processing.

#### 0B. Existing Code Leverage Map

| Sub-problem | Existing code to reuse |
|---|---|
| TicketEvent schema | `app/models.py:TicketEvent` — all fields present |
| EventType enum | `app/enums.py:EventType` — SUMMARIZED, PRIORITY_CHANGED, SPAM_FLAGGED, ROUTED all exist |
| Repo query infra | `app/repositories/ticket.py:TicketRepository` — add selectinload to `get()` |
| Response schema | `app/schemas.py:TicketResponse` — subclass to `TicketDetailResponse` |
| Route wiring | `app/api/tickets.py:get_ticket` — update response_model only |

No new infrastructure. Zero new tables or migrations.

#### 0C. Dream State Delta
```
CURRENT STATE                   THIS PLAN                     12-MONTH IDEAL
Events in DB only           →   Events in GET /tickets/{id}  →  Webhook notifications
Worker results invisible    →   SUMMARIZED/ROUTED visible    →  Real-time SSE feed
README inconsistent         →   README accurate              →  Full API reference
assign_agent_id nullable    →   Defer with documentation     →  RBAC assign + agent UI
```

#### 0C-bis. Approaches (auto-decided)

Auto-decision D-A1: Approach selected = **B (Minimal + visible)**: `TicketDetailResponse` subclass + selectinload on `get()` only + README fix + assign explicitly deferred.
- Rejected Approach A (events endpoint): grader needs to know to call it — less visible.
- Rejected Approach C (full assign): new untested code before submission, higher risk.
- Principle applied: P5 (explicit over clever) + P6 (bias toward action).

#### Error & Rescue Registry

| Error | Trigger | User sees | Test? |
|---|---|---|---|
| GET /tickets/{id} — no events yet | New ticket, workers haven't run | `events: []` | Add test |
| GET /tickets/{id} — ticket not found | id=999 | 404 `ticket_not_found` | Already tested |
| GET /tickets list — no events field | List route (no selectinload) | items without events | Document; add test |
| selectinload missing on get() | Code omits selectinload | 500 MissingGreenlet | Must test |

#### Failure Modes Registry

| Mode | Probability | Impact | Gap |
|---|---|---|---|
| selectinload omitted from `get()` | Medium (easy to forget) | Critical — 500 on every GET | Must have test |
| list() tries to access events | Low — separate schema | 500 MissingGreenlet on list | Fix: separate schemas |
| events list unbounded | Low for demo | Large payload | Acceptable for demo |

#### NOT In Scope

- Assign endpoint (Gap 1): Explicitly deferred. Document in README with rationale.
- AnthropicSummarizer backend (Phase 4b): Already tracked in TODOS.md.
- Auth/authz, rate limiting, multi-tenant: Out of scope for learning project.
- make demo script: Cherry-pick — surfaced at final gate.

#### What Already Exists

All infrastructure for events visibility is already in place:
- `TicketEvent` model with `event_type`, `field_changed`, `previous_value`, `new_value`, `actor_id`
- `EventType` enum: CREATED, STATUS_CHANGED, PRIORITY_CHANGED, ASSIGNED, SUMMARIZED, SPAM_FLAGGED, ROUTED
- `Ticket.events` relationship (lazy="raise" — requires explicit selectinload)
- `TicketRepository.add_event()` already used by workers
- `TicketRepository.get()` already exists — add selectinload

#### CEO Completion Summary

| Section | Finding | Action |
|---|---|---|
| Problem framing | Plan optimized spec coverage, not grader differentiation | CORRECTED via premises gate |
| Scope | Gap 1 = defer; Gap 2 = embed; Gap 3 = fix | CONFIRMED |
| Alternatives | 3 approaches evaluated | B selected |
| Failure modes | selectinload omission = critical 500 | Must test |
| What exists | Full event infra already in DB | Reuse everything |
| Deferred | assign endpoint, make demo (cherry-pick) | Documented |

**Phase 1 complete.** Claude subagent: 6 concerns (premises, scope, approach, demo path, assign risk, events invisible). Consensus: 4/6 confirmed, 2 surfaced at gate.
Passing to Phase 2 (Design — SKIPPED, no UI scope). Passing to Phase 3 (Eng).

---

### Phase 2 — Design Review: SKIPPED (no UI scope)

---

### Phase 3 — Eng Review

**Voices:** Claude subagent [codex-unavailable]

#### Architecture ASCII Diagram

```
GET /tickets/{id}
    │
    └── get_ticket_service (Depends)
            │
            └── TicketService.get_ticket(id)
                    │
                    └── TicketRepository.get(id)
                            │
                            └── select(Ticket)
                                .options(selectinload(Ticket.events)
                                         .order_by(TicketEvent.created_at))
                                .where(Ticket.id == ticket_id)
                                    │
                                    └── ticket.events: list[TicketEvent]
                                            │
                                            ▼
                        TicketDetailResponse.model_validate(ticket)
                                ▼
                        {"id":1, ..., "events":[{"event_type":"CREATED",...},
                                                {"event_type":"SUMMARIZED",...}]}

GET /tickets (list) — unchanged
    │
    └── TicketRepository.list()
            │
            └── select(Ticket).where(...).offset().limit()
                (no selectinload — events NOT loaded, NOT in response)
                    ▼
            ListTicketsResponse { items: list[TicketResponse] }
```

#### Scope Challenge

Files touched by this plan:
- `app/schemas.py` — add `TicketEventResponse`, `TicketDetailResponse`
- `app/repositories/ticket.py` — add selectinload + order_by to `get()`
- `app/api/tickets.py` — update `get_ticket` response_model; add `Path(ge=1)` annotation
- `README.md` — fix roadmap, add events table, add demo sequence, update banner
- `TODOS.md` — mark assign explicitly deferred
- `app/tests/test_api/test_tickets.py` — add 5 test cases

Total: 6 files, no new migrations, no new infrastructure. Complexity: LOW.

#### ENG DUAL VOICES — CONSENSUS TABLE [subagent-only]

```
ENG DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Architecture sound?               Yes     N/A   CONFIRMED: subclass pattern clean
  2. Test coverage sufficient?         No      N/A   CONCERN: 5 test gaps identified
  3. Performance risks addressed?      Yes     N/A   CONFIRMED: list stays lean (no N+1)
  4. Security threats covered?         Yes     N/A   CONFIRMED: read-only, no new surface
  5. Error paths handled?              Partial N/A   CONCERN: PATCH asymmetry to document
  6. Deployment risk manageable?       Yes     N/A   CONFIRMED: zero migrations, rollback trivial
═══════════════════════════════════════════════════════════════
[subagent-only — codex unavailable]
```

#### Section 1 — Architecture

`TicketDetailResponse(TicketResponse)` subclass is the right pattern. Pydantic V2 propagates `from_attributes=True` to subclasses automatically. The schema split keeps the list endpoint lean.

**Critical requirement:** `TicketEventResponse` must define its own `model_config = ConfigDict(from_attributes=True)` — Pydantic validates nested ORM objects independently.

PATCH /tickets/{id}/status asymmetry: after the plan lands, PATCH returns `TicketResponse` (no events), GET returns `TicketDetailResponse` (with events). This is intentional and acceptable for a demo. Document in README: "Call `GET /tickets/{id}` after creating or updating to see the full event history including worker results."

Auto-decision D-A2: Document PATCH asymmetry, do NOT add reload after commit (P5: no added complexity). Log to TODOS.md as a future improvement.

#### Section 2 — Code Quality

`selectinload(Ticket.events).order_by(TicketEvent.created_at)` — ordering is required. Without it, event order is insertion-order by default but not guaranteed by SQL. Adding `order_by` is one chained call.

`ticket_id: Annotated[int, Path(ge=1)]` — add to both `get_ticket` and `update_status`. Rejects id=0 and negatives at the routing layer, not the DB.

Auto-decision D-A3: Both included (P1 — completeness is cheap, each is 1-2 lines).

#### Section 3 — Test Coverage (NEVER SKIP)

**New UX flows introduced by this plan:**

| Flow | Test type | Gap? |
|---|---|---|
| GET /tickets/{id} immediately after create → events: [CREATED] | Integration | YES |
| GET /tickets/{id} after worker runs → events includes SUMMARIZED | Integration | YES (arq mock) |
| GET /tickets list items do NOT have events field | Unit/Integration | YES |
| TicketDetailResponse.model_validate(ticket_with_events) succeeds | Unit | YES |
| GET /tickets/{id} with invalid id (id=0, id=-1) → 422 | Integration | YES |
| PATCH /tickets/{id}/status does not trigger MissingGreenlet | Integration | YES |

Test plan artifact written below. All 5 test gaps are auto-decided INCLUDE (P1).

#### Test Plan

Test file: `app/tests/test_api/test_tickets.py`

```python
# T1: GET /tickets/{id} returns CREATED event immediately after ticket creation
async def test_get_ticket_includes_created_event(client, session):
    r = await client.post("/tickets", json={...})
    ticket_id = r.json()["id"]
    r2 = await client.get(f"/tickets/{ticket_id}")
    events = r2.json()["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "CREATED"

# T2: GET /tickets/{id} includes multiple events after status change
async def test_get_ticket_includes_status_change_event(client, session):
    ticket_id = ...  # create + update status
    r = await client.get(f"/tickets/{ticket_id}")
    event_types = [e["event_type"] for e in r.json()["events"]]
    assert "STATUS_CHANGED" in event_types
    assert event_types[0] == "CREATED"  # ordered by created_at

# T3: GET /tickets list items do NOT have events field
async def test_list_tickets_items_have_no_events_field(client, session):
    await client.post("/tickets", json={...})
    r = await client.get("/tickets")
    for item in r.json()["items"]:
        assert "events" not in item

# T4: GET /tickets/{id} with id=0 returns 422
async def test_get_ticket_invalid_id_returns_422(client):
    r = await client.get("/tickets/0")
    assert r.status_code == 422

# T5: PATCH /tickets/{id}/status does not load events (no MissingGreenlet)
async def test_update_status_does_not_access_events(client, session):
    ticket_id = ...  # create
    r = await client.patch(f"/tickets/{ticket_id}/status", json={"status": "IN_PROGRESS"})
    assert r.status_code == 200
    assert "events" not in r.json()
```

#### Failure Modes Registry

| Mode | Probability | Impact | Resolution |
|---|---|---|---|
| selectinload omitted from get() | Medium | Critical — 500 MissingGreenlet | Test T1 catches this |
| TicketEventResponse missing from_attributes | Medium | Medium — 500 on model_validate | Test T1 catches this |
| list() accidentally gets selectinload | Low | Medium — N+1 perf regression | Test T3 catches this |
| PATCH route accesses events via model_validate | Low | Critical — 500 MissingGreenlet | Test T5 catches this |

#### NOT In Scope (Eng)

- Reload ticket after PATCH for symmetric response (TODOS.md)
- Pagination on events list (TODOS.md — unbounded acceptable for demo)
- Concurrent events write testing (already tested via existing worker tests)

#### What Already Exists (Eng)

All needed: `TicketEvent` model, `EventType` enum, `TicketRepository.add_event()`, `Ticket.events` relationship (lazy="raise"), `TicketResponse(ConfigDict(from_attributes=True))`. Zero new infra.

#### Eng Completion Summary

| Section | Finding | Action |
|---|---|---|
| Architecture | TicketDetailResponse subclass — sound | CONFIRMED |
| selectinload | Must add to get() with order_by | INCLUDE |
| from_attributes on TicketEventResponse | Required | INCLUDE |
| PATCH asymmetry | Document, don't fix | DOCUMENT |
| Path(ge=1) | Trivial improvement | INCLUDE |
| Test gaps | 5 gaps identified | ALL INCLUDE |

**Phase 3 complete.** Claude subagent: 6 findings (1 critical, 1 high, 2 medium, 2 low). Consensus: 4/6 confirmed, 2 concerns.
Passing to Phase 3.5 (DX Review — triggered: REST API project, developer-facing endpoints).

---

### Phase 3.5 — DX Review

**Voices:** Claude subagent [codex-unavailable]

#### Developer Journey Map

| Stage | Current state | After plan |
|---|---|---|
| 1. Discover | README + GitHub | README banner updated |
| 2. Install | `make run` (auto-migrates via start.sh) | No change — already works |
| 3. Hello World | `make health` → 200 | No change |
| 4. First call | POST /tickets → 201, flat response | No change |
| 5. See async work | (nothing — events invisible) | GET /tickets/{id} → events array |
| 6. Understand events | (no docs on event types) | README event types table |
| 7. Debug | `make worker-logs` | No change |
| 8. Iterate | status transitions | No change |
| 9. Submit | README says "active development" | Banner updated for submission |

TTHW (time to see worker events): **current: ~8 min** (no demo path). **After plan: ~5 min** (demo sequence in README).

#### DX DUAL VOICES — CONSENSUS TABLE [subagent-only]

```
DX DUAL VOICES — CONSENSUS TABLE:
═══════════════════════════════════════════════════════════════
  Dimension                           Claude  Codex  Consensus
  ──────────────────────────────────── ─────── ─────── ─────────
  1. Getting started < 5 min?          Partial N/A   CONCERN: demo sequence missing
  2. API/CLI naming guessable?         Yes     N/A   CONFIRMED: clean REST naming
  3. Error messages actionable?        Partial N/A   CONCERN: Makefile silent failure
  4. Docs findable & complete?         Partial N/A   CONCERN: event types table missing
  5. Upgrade path safe?                Yes     N/A   CONFIRMED: Alembic migrations
  6. Dev environment friction-free?    Yes     N/A   CONFIRMED: make run + auto-migrate
═══════════════════════════════════════════════════════════════
[subagent-only — codex unavailable]
```

#### DX Scorecard (8 dimensions)

| Dimension | Score | Finding |
|---|---|---|
| 1. Getting started (TTHW) | 6/10 | Demo sequence missing; auto-migrate works |
| 2. API naming | 8/10 | Clean REST, guessable paths |
| 3. Error messages | 7/10 | Error envelopes good; Makefile silent |
| 4. Documentation | 6/10 | Good README; missing event types table + demo |
| 5. Upgrade path | 9/10 | Alembic, explicit versioning |
| 6. Dev environment | 8/10 | Docker Compose, one command |
| 7. Escape hatches | 8/10 | All config via env vars |
| 8. Consistency | 9/10 | Uniform patterns throughout |
| **Overall** | **7.6/10** | |

#### DX Decisions (auto-decided)

Auto-decision D-A4: **`make migrate` false alarm** — migrations auto-run via `scripts/start.sh`. README needs a note clarifying this, not an additional step. P5.

Auto-decision D-A5: **Event types table** — INCLUDE in README Background section. One table with 4 event types + `new_value` meaning. P1.

Auto-decision D-A6: **Makefile silent failure** — DEFER to TODOS.md. Out of scope for this plan. P3.

Auto-decision D-A7: **Demo sequence (curl + sleep + GET)** — Cherry-pick from CEO. Surface at Final Gate. TASTE DECISION.

Auto-decision D-A8: **Assign checkbox** — INCLUDE in Gap 3. Change `[ ]` to checkbox with deferred note. P1.

Auto-decision D-A9: **"active development" banner** — INCLUDE in Gap 3. Change to submission-ready language. P3.

#### DX Implementation Checklist

- [ ] Add "includes migrations automatically" note to README Setup block
- [ ] Add event types table to Background summarization section
- [ ] Update README Roadmap: mark assign as explicitly deferred (not just open)
- [ ] Change status banner from "active development" to submission-appropriate language
- [ ] Add demo sequence (cherry-pick — Final Gate)

**Phase 3.5 complete.** DX overall: 7.6/10. TTHW: ~8 min → ~5 min (with demo sequence).
Claude subagent: 5 findings (2 high, 2 medium, 1 low). 1 high corrected (false positive). Consensus: 4/6 confirmed, 2 concerns.
Passing to Phase 4 (Final Approval Gate).

---

### Final Approval

**Status: APPROVED** — 2026-06-03 | All premises confirmed | Demo sequence included.

### Implementation Tasks (approved)

- [ ] P1, ~15 min — `app/schemas.py`: add `TicketEventResponse` + `TicketDetailResponse`
- [ ] P1, ~5 min — `app/repositories/ticket.py`: `selectinload(Ticket.events).order_by(TicketEvent.created_at)` on `get()` only
- [ ] P1, ~10 min — `app/api/tickets.py`: `get_ticket` → `TicketDetailResponse`, `Path(ge=1)` on ticket_id
- [ ] P1, ~20 min — `app/tests/test_api/test_tickets.py`: 5 new test cases (T1–T5)
- [ ] P2, ~15 min — `README.md`: roadmap fix, event types table, setup note, banner update, PATCH asymmetry note, demo sequence
- [ ] P2, ~5 min — `TODOS.md`: assign defer note, PATCH asymmetry, Makefile note

### Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | CEO | Mode = SELECTIVE EXPANSION | Mechanical | P3 | Small plan, scope well-bounded, cherry-pick expansions appropriate | EXPANSION (too big), REDUCTION (already lean) |
| 2 | CEO | Gap 1 (assign) = DEFER + document | Mechanical | P5+P6 | Spec satisfied by workers; new code before submission = risk | IMPLEMENT |
| 3 | CEO | Gap 3 (README sync) = INCLUDE | Mechanical | P3 | 5-min fix, zero risk | — |
| 4 | CEO | Approach = B (TicketDetailResponse + embed) | Taste | P5 | Maximum discoverability for grader; reuses all existing infra | A (events endpoint), C (full assign) |
| 5 | CEO | Embed events only on GET /id, not list | Mechanical | P5+P3 | Avoids N+1 on list; list stays lean | Embed on list too (N+1 risk) |
| 6 | Eng | selectinload + order_by on get() | Mechanical | P1 | Required for correctness; order guaranteed | No order_by (non-deterministic) |
| 7 | Eng | TicketEventResponse needs from_attributes=True | Mechanical | P1 | Required for ORM → Pydantic validation | Omit (500 on model_validate) |
| 8 | Eng | PATCH asymmetry: document, don't fix | Mechanical | P5 | No reload complexity; PATCH returns flat ticket | Add reload after commit |
| 9 | Eng | Path(ge=1) on ticket_id | Mechanical | P1 | Rejects invalid ids at routing layer | Leave bare int |
| 10 | Eng | 5 test gaps: ALL INCLUDE | Mechanical | P1 | Completeness is cheap; each gap catches a real failure mode | Defer tests |
| 11 | DX | make migrate false alarm: clarify README | Mechanical | P5 | Migrations auto-run via start.sh; README should say so | Add make migrate to setup |
| 12 | DX | Event types table | Mechanical | P1 | Grader needs to understand what events mean | Skip table |
| 13 | DX | Makefile silent failure | Mechanical | P3 | Out of scope for this plan; TODOS.md | Include fix |
| 14 | DX | Demo sequence | Taste | P1 | Cherry-pick; surfaces at Final Gate | Skip demo |
| 15 | DX | Assign checkbox: update to deferred note | Mechanical | P1 | Open checkbox misleads grader | Leave as-is |
| 16 | DX | Active development banner: update | Mechanical | P3 | Misleading for submission context | Leave as-is |
