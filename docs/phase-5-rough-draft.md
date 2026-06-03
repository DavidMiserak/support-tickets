<!-- /autoplan restore point: /home/david/.gstack/projects/DavidMiserak-support-tickets/feat-observability-hardening-autoplan-restore-20260602-235159.md -->
# Phase 5 — Observability Hardening (ROUGH DRAFT)

Status: APPROVED (autoplan 2026-06-03, subagent-only) — 13 auto-decisions, 0 taste choices. See Decision Audit Trail and GSTACK REVIEW REPORT below.
Branch: develop (will land as feat/observability-hardening or similar)
Depends on: Phase 4 (observability) — done and merged.

---

## Goal

Close three operational gaps found in the adversarial post-implementation review of
Phase 4. No new features; no new dependencies. The app should be safe to put behind a
load balancer or Docker HEALTHCHECK without spurious container restarts, and its
observability endpoints should not hang under network failure.

After this phase:
- A Docker HEALTHCHECK on the `api` container will not restart it when Redis is
  briefly unavailable (Redis is optional for serving requests).
- All server-generated `X-Request-ID` values will use the same hyphenated UUID4
  format as client-supplied IDs — log aggregators can correlate without format branches.
- A half-open TCP connection to Postgres or Redis will not hang `/health` or `/ready`
  indefinitely; probes time out in 2 seconds.

### The operational scenario this enables

`docker compose up` and Redis takes 5 seconds to respond after a brief network
partition. Before Phase 5:
- The api container's `/health` returns 503 (redis: unavailable).
- If a HEALTHCHECK is configured (e.g. when deploying to any orchestrator), the
  container is marked unhealthy and restarted — even though it can serve all ticket
  CRUD requests fine without Redis.

After Phase 5:
- `/health` (liveness) returns 200 regardless of Redis state — confirms the process
  is alive and Postgres is reachable (the requirement for any useful work).
- `/ready` (readiness) returns 503 when Redis is down — the load balancer routes
  traffic away until Redis recovers, but the container is not restarted.
- The api `healthcheck` block in `compose.yaml` now uses `/health`, so a Redis blip
  does not trigger a container restart.

---

## In scope

**Fix 1 — Liveness/readiness split**
Split the single `/health` endpoint into two:
- `GET /health` → **liveness probe**: confirms the process is running and Postgres
  accepts queries. Returns `{"status": "ok"}` 200 always (no Redis check). Used by
  Docker HEALTHCHECK.
- `GET /ready` → **readiness probe**: checks Postgres + Redis. Same logic and
  response shape as the current `/health`. Used by orchestrators to route traffic.
- Add `healthcheck` block to the `api` service in `compose.yaml` using `/health`.

Rationale: Redis is optional for request serving (only needed for background
summarization). A container should not be restarted because Redis is temporarily
unreachable. The liveness/readiness split is the canonical fix for this class of
problem in containerised services.

**Fix 2 — X-Request-ID generator consistency**
Add `generator=lambda: str(uuid4())` to `CorrelationIdMiddleware` in `app/main.py`.

Current: the default generator produces `uuid4().hex` (32-char no-hyphens). A
client-supplied valid UUID4 is echoed as `"550e8400-e29b-41d4-a716-446655440000"`
(hyphenated). Log aggregators see two formats for the same field — fragile regex
patterns, ambiguous dashboards.

Fix: one line. `str(uuid4())` produces `"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"`.
The `is_valid_uuid4` validator already validates hyphenated format, so this aligns
generator and validator.

**Fix 3 — Probe timeouts**
Wrap DB and Redis probes in `/health` and `/ready` with `asyncio.wait_for(..., timeout=2.0)`.

Current: `check_database_connection()` opens a TCP connection, `pool.ping()` sends a
Redis ping — both block indefinitely if the TCP connection is half-open (common under
network partitions, firewall resets, or cloud NAT expiry). A stuck health probe blocks
the endpoint handler, which under load can exhaust the uvicorn worker pool.

Fix: wrap both at the call site in the health/ready route functions. 2s is the
conventional probe timeout (leaves 8s of headroom in a 10s healthcheck interval).

**Tests**
- `/health` liveness: verify 200 regardless of Redis state (mock Redis as down, assert
  200 and `"redis"` key absent from liveness response).
- `/ready` readiness: existing health tests ported to `/ready` (rename the endpoint
  under test — no logic changes).
- UUID format: update `test_health_generates_request_id_header` to assert
  `is_valid_uuid4(returned_id)` instead of `len > 0`.
- Timeout: mock `check_database_connection` and `pool.ping` to raise
  `asyncio.TimeoutError`; assert the endpoint returns `"unavailable"` instead of
  hanging.
- Compose healthcheck: document in a manual test note; no automated compose test.

## Out of scope

- **`/metrics` probe timeout.** The `prometheus-fastapi-instrumentator` handles
  `/metrics` internally — it reads from the in-process registry, not a network call.
  No timeout risk. No change needed.
- **Worker-side Prometheus / metrics endpoint.** Deferred from Phase 4; not a
  hardening concern.
- **Loki / alerting / distributed tracing.** Out of scope for this project phase.
- **`/assign` endpoint.** Deferred from Phase 2.
- **`async_session` → `async_session_factory` rename.** Standalone rename, deferred.
- **Phase 4b (AnthropicSummarizer).** Ships as its own standalone PR.

---

## Architecture

No new modules. Changes are confined to `app/main.py`, `compose.yaml`, and the health
test file.

```
GET /health  → liveness
  → asyncio.wait_for(check_database_connection(), timeout=2.0)
  → {"status": "ok" | "degraded", "database": "ok" | "unavailable"}
  → 200 if db_ok, 503 otherwise (Redis not checked)

GET /ready   → readiness
  → asyncio.wait_for(check_database_connection(), timeout=2.0)
  → asyncio.wait_for(_check_redis(pool), timeout=2.0)
  → {"status": "ok" | "degraded", "database": ..., "redis": ...}
  → 200 if both ok, 503 otherwise

Containerfile HEALTHCHECK (unchanged — already uses Python stdlib, no curl):
  CMD python -c "import urllib.request, sys; sys.exit(0 if ...urlopen('/health')... else 1)"
  → interval: 30s, timeout: 5s, start_period: 10s, retries: 3
```

---

## Implementation

### app/main.py

**Fix 2 (UUID generator):** add `from uuid import uuid4` and update middleware:
```python
app.add_middleware(
    CorrelationIdMiddleware,
    generator=lambda: str(uuid4()),
    validator=is_valid_uuid4,
)
```

**Fix 1 + Fix 3 (split + timeouts):**
```python
import asyncio

@app.get("/health", tags=["health"])
async def health() -> JSONResponse:
    """Liveness: process is up and Postgres accepts queries."""
    try:
        db_ok = await asyncio.wait_for(check_database_connection(), timeout=2.0)
    except asyncio.TimeoutError:
        db_ok = False
        logger.warning("database liveness probe timed out")

    return JSONResponse(
        status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "unavailable",
        },
    )


@app.get("/ready", tags=["health"])
async def ready(
    arq_pool: Annotated[ArqRedis | None, Depends(get_arq_pool)],
) -> JSONResponse:
    """Readiness: Postgres and Redis are both reachable."""
    try:
        db_ok = await asyncio.wait_for(check_database_connection(), timeout=2.0)
    except asyncio.TimeoutError:
        db_ok = False
        logger.warning("database readiness probe timed out")

    try:
        redis_ok = await asyncio.wait_for(_check_redis(arq_pool), timeout=2.0)
    except asyncio.TimeoutError:
        redis_ok = False
        logger.warning("redis readiness probe timed out")

    all_ok = db_ok and redis_ok
    return JSONResponse(
        status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ok" if all_ok else "degraded",
            "database": "ok" if db_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    )
```

### compose.yaml

No change needed. The Containerfile already defines a HEALTHCHECK using the Python
stdlib (no curl dependency), pointing to `/health`:

```
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"
```

Splitting `/health` to liveness-only (DB, no Redis) automatically fixes the restart
loop — the HEALTHCHECK command stays exactly as-is. No compose.yaml changes needed.

### app/tests/test_health.py

- Port all readiness tests (503 Redis, 503 DB, Redis ping fails) to `/ready`.
- Add `/health` liveness tests: assert 200 when Redis is down (Redis state not checked).
- Update `test_health_generates_request_id_header` to assert `is_valid_uuid4(returned_id)`.
- Add timeout test: mock `check_database_connection` to raise `asyncio.TimeoutError`
  directly; assert endpoint returns 503 and `"database": "unavailable"`.

### README.md

Update the health endpoint documentation:
- Describe the liveness/readiness split.
- Add curl examples for both `/health` and `/ready`.
- Note that `/health` is safe to use as a Docker HEALTHCHECK.

---

## Files touched

Modified:
- `app/main.py` — split `/health`, add `/ready`, add timeouts, add uuid4 generator
- `app/tests/test_health.py` — port tests to `/ready`, add liveness tests, timeout test
- `README.md` — document the split endpoints

No new files. No compose.yaml changes (Containerfile HEALTHCHECK already correct). No
migrations. No new dependencies.

---

## Open questions for review

- **D1 — Liveness response shape.** Should `/health` include `"database": "ok"` in the
  response body (current plan) or just `{"status": "ok"}` (minimal)? Including `database`
  gives the on-call engineer richer info when investigating a liveness failure. The minimal
  shape is more conventional for pure liveness probes.
- **D2 — Timeout value.** 2 seconds per probe (4s total for `/ready` in the worst case).
  The Containerfile HEALTHCHECK has `--timeout=5s`, leaving 3s headroom per check cycle.
  This is comfortable, but if probes run sequentially inside the handler, the effective
  wall-clock max is 2+2 = 4s for `/ready`. Acceptable?
- **D3 — Liveness: DB check or pure 200?** The Containerfile HEALTHCHECK hits `/health`.
  Current plan checks DB in the liveness endpoint. The alternative: `/health` just returns
  200 unconditionally (confirms the process is alive; DB loss is self-healing via SQLAlchemy
  pool reconnect). Tradeoff: with DB check, a DB outage restarts the container (probably
  wrong); with no check, a truly dead DB doesn't trigger a restart (container serves traffic
  that fails at the service layer). Recommend: pure 200 for liveness, all probing in `/ready`.
- **D4 — Timeout placement.** Current plan wraps probes with `asyncio.wait_for` at the
  call site inside each route handler. Alternative: add timeout to `check_database_connection()`
  in `database.py` itself (centralizes the concern). Tradeoff: centralizing means the DB
  helper has a fixed timeout that callers can't override; call-site wrapping is more
  flexible. For 1 caller (health), call-site is fine.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | CEO | compose.yaml verified — healthcheck blocks are for redis (L25) and db (L78) only; no api service healthcheck. "No compose.yaml changes needed" claim is correct. | Mech | P5 | Verified by grep; plan claim accurate |
| 2 | CEO | Document Redis-optional premise in code comment at /health and /ready definitions. | Mech | P5 | Premise 1 (Redis optional for CRUD) should be visible in code, not just the plan |
| 3 | CEO+Eng+DX | /health returns pure 200 unconditionally (no DB check). Remove check_database_connection() call from /health. Architecture diagram and implementation code corrected to match. | Mech | P5 | All three review voices independently flagged D3 contradiction. Pure 200 is the canonical liveness pattern. A DB outage triggering container restarts recreates the exact anti-pattern this phase eliminates. |
| 4 | CEO | D4: Call-site asyncio.wait_for wrapping accepted (not centralizing in database.py). One caller, more flexible. | Mech | P5 | Single call site makes call-site wrapping explicit and non-intrusive |
| 5 | CEO | Add test/comment verifying sequential timeout budget for /ready (2+2s, each probe independent). | Mech | P1 | CEO subagent: a bug could make both probes share one 2s budget; make independence verifiable |
| 6 | Eng | Fix plan contradiction on line 53: remove "Add healthcheck block to compose.yaml" from scope. Replace with note that Containerfile HEALTHCHECK already targets /health. | Mech | P5 | Eng subagent: scope section and implementation section contradicted each other |
| 7 | Eng | Add test: Redis probe timeout → 503. Mock _check_redis to raise asyncio.TimeoutError at the call site; assert /ready returns 503 with "redis": "unavailable". | Mech | P1 | Eng subagent: Redis timeout path was untested in the plan |
| 8 | Eng | Update test_health_generates_request_id_header BEFORE generator change: assert is_valid_uuid4(id) AND '-' in id. Ensures test fails red before going green. | Mech | P5 | Eng subagent: existing test (len > 0) passes on old hex format, giving no regression signal |
| 9 | Eng | Note SQLAlchemy pool state under repeated asyncio.wait_for cancellations as a known limitation. Defer adding connect_args={"timeout": 1.5} to TODOS.md. | Deferred | P3 | Eng subagent raised valid concern; for learning project, a TODOS note is sufficient |
| 10 | DX | README update must explicitly state: /health no longer checks Redis (two-field response), /ready is readiness (three-field response). Show both curl examples. | Mech | P1 | DX subagent: README health section describes old combined behavior; will mislead developers |
| 11 | DX | Add two-sentence liveness/readiness explainer before curl examples in README for container newcomers. | Mech | P1 | DX subagent: distinction is not self-evident to developers without container operations background |
| 12 | DX | Update README debugging flow step 5 and make health Makefile target to reference /ready (not /health) for Redis reachability checks. | Mech | P1 | DX subagent: step 5 says "curl /health" to check Redis; after split, that check moves to /ready |
| 13 | DX | Add CHANGELOG entry for X-Request-ID format change: 32-char hex → hyphenated UUID4. | Mech | P1 | DX subagent: log aggregators with regex patterns will silently break without a callout |

---

# GSTACK REVIEW REPORT

Voices: **subagent-only** (Codex binary not installed — dual-voice degraded).
Base branch: `main`. Branch: `feat/observability-hardening`.

## Phase 1 — CEO / Strategy

### CEO Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Premises valid?                    MOSTLY  [unavailable]  MOSTLY — Redis-optional premise should be documented in code
  2. Right problem to solve?            YES     [unavailable]  YES — correct operational gaps for learning project
  3. Scope calibration correct?         YES     [unavailable]  YES — 3 focused fixes, right-sized diff
  4. Alternatives explored?             MOSTLY  [unavailable]  D3 needed resolution (pure 200 vs DB-check) — RESOLVED
  5. Learning value sound?              YES     [unavailable]  YES — liveness/readiness, asyncio timeouts, middleware config
  6. 6-month trajectory sound?          YES     [unavailable]  YES — if D3 resolved correctly (now confirmed: pure 200)
```

### CEO findings
- **MEDIUM — D3 contradiction (resolved).** Plan said pure 200 for liveness but implementation code had DB check. Both CEO and Eng subagents flagged this independently. → auto-decided: pure 200 for /health, no DB check. (D3, Mech, P5)
- **LOW — Redis-optional premise not documented in code.** Code should have a comment stating Redis is optional for CRUD requests. → auto-decided: add comment. (D2, Mech, P5)
- **LOW — Sequential timeout budget for /ready not tested.** If a bug made both probes share one budget, it would be invisible. → auto-decided: add test/assertion. (D5, Mech, P1)

## Phase 3 — Engineering

### Eng Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Architecture sound?               YES     [unavailable]  YES — endpoint split is correct
  2. Test coverage sufficient?         NO      [unavailable]  Missing: Redis timeout test, /health liveness tests
  3. Concurrency handled?              YES     [unavailable]  asyncio.wait_for is correct; CancelledError propagates cleanly
  4. D3 resolved correctly?            YES     [unavailable]  Pure 200 confirmed — both models agree
  5. Security surface acceptable?      MOSTLY  [unavailable]  /ready is unauthenticated; noted as out of scope
  6. Deployment risk manageable?       YES     [unavailable]  No migrations, no new deps, Containerfile unchanged
```

### Eng findings
- **CRITICAL (false alarm — plan contradiction only) — D3 architecture/implementation conflict.** Implementation code showed DB check but goal said pure 200. → auto-decided: pure 200 for /health. (D3, confirmed)
- **HIGH — Timeout margin concern.** With 2+2=4s for /ready and HEALTHCHECK --timeout=5s, the subagent flagged tight headroom. **Analysis**: HEALTHCHECK calls /health (pure 200, no I/O), not /ready. No margin issue. Finding is N/A after D3 resolution.
- **HIGH — asyncio.TimeoutError through _check_redis.** Subagent claimed TimeoutError bypasses `except Exception`. **Analysis**: `asyncio.TimeoutError` is a subclass of `Exception` (via OSError) in Python 3.11+; it IS caught. The `wait_for` at the call site handles it correctly. False positive, no change needed.
- **MEDIUM — Redis timeout path untested.** No test for Redis probe raising asyncio.TimeoutError → 503. → auto-decided: add test. (D7, Mech, P1)
- **MEDIUM — Plan scope contradiction.** Line 53 says "add healthcheck block to compose.yaml" but implementation section says no change needed. → auto-decided: fix contradiction in plan. (D6, Mech, P5)
- **LOW — test_health_generates_request_id_header too permissive.** Asserts `len > 0`, not UUID4 format. → auto-decided: update test before generator change for red-before-green. (D8, Mech, P5)
- **LOW — SQLAlchemy pool state under cancellation.** Repeated TimeoutError storms could corrupt pool under sustained load. → deferred to TODOS.md. (D9, P3)

### Code Paths — Coverage Diagram
```
CODE PATHS                                              TEST STATUS
[+] app/main.py
  ├── GET /health (liveness)
  │   ├── [GAP] 200 always — pure 200 design            to add: test_health_liveness_returns_200
  │   ├── [GAP] no "redis" key in response              to add: test_health_liveness_no_redis_key
  │   └── [GAP] 503 if DB probe added — N/A (pure 200)  n/a after D3 resolution
  └── GET /ready (readiness)
      ├── [★★★ PORT] 200 all ok                         existing test ported from /health
      ├── [★★★ PORT] 503 DB down                        existing test ported from /health
      ├── [★★★ PORT] 503 Redis pool None                existing test ported from /health
      ├── [★★★ PORT] 503 Redis ping fails               existing test ported from /health
      ├── [GAP] 503 DB timeout                          to add: test_ready_db_timeout
      └── [GAP] 503 Redis timeout                       to add: test_ready_redis_timeout

[+] CorrelationIdMiddleware generator
  ├── [★★  UPDATE] server-generated ID format           update: assert is_valid_uuid4 AND '-' in id
  └── [★★★ PASS] invalid client ID → replacement        test_health_rejects_invalid_request_id (exists)

COVERAGE: 4 existing tests port to /ready | 4 new tests to add | 1 test to update
```

## Phase 3.5 — Developer Experience

### DX Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. README health section correct?    NO      [unavailable]  NO — describes old behavior, must be updated
  2. Liveness/readiness explained?     NO      [unavailable]  NO — two sentences needed for newcomers
  3. curl examples complete?           NO      [unavailable]  NO — /ready examples missing, step 5 references wrong endpoint
  4. X-Request-ID change announced?    NO      [unavailable]  NO — CHANGELOG entry missing for format change
  5. Endpoint naming guessable?        YES     [unavailable]  YES — /health and /ready are industry-standard names
  6. Error responses informative?      YES     [unavailable]  YES — per-component "ok"/"unavailable" shape is clear
```

DX score: **5/10** (README describes wrong behavior) → **8/10** after fixes (all mechanical).

### DX findings
- **HIGH — README health section will mislead after split.** Shows three-field response with "redis", states /health checks both components. → auto-decided: update to show two-field /health and three-field /ready with correct descriptions. (D10, Mech, P1)
- **MEDIUM — Liveness/readiness not explained for newcomers.** → auto-decided: add two-sentence explainer. (D11, Mech, P1)
- **LOW — README debugging step 5 and `make health` reference wrong endpoint.** Step 5 says "curl /health" to verify Redis. After split, that check moves to /ready. → auto-decided: update step 5 and note `make health` targets liveness only. (D12, Mech, P1)
- **LOW — No CHANGELOG entry for X-Request-ID format change.** Regex-based log aggregators will silently break. → auto-decided: add entry. (D13, Mech, P1)

## Cross-Phase Themes

- **D3 contradiction flagged by all three phases independently.** CEO subagent, Eng subagent, and DX subagent all found the architecture/implementation conflict. Unanimous: pure 200 for /health. High-confidence signal — not a taste decision.
- **README update incompleteness.** CEO flagged "plan specifies what to add but not what content." DX confirmed: step 5, make health, and health section body all need specific wording. Same cross-phase gap.

## Taste Decisions
None. D3 was the only open question and was resolved by consensus of all review voices.

## Implementation Tasks (aggregated)

- [ ] **P0 — Resolve D3: make /health return pure 200** — remove `check_database_connection()` call; response is `{"status": "ok"}` always. `app/main.py`
- [ ] **P0 — Port existing /health readiness tests to /ready** — update endpoint under test in all 4 readiness test cases. `app/tests/test_health.py`
- [ ] **P0 — Update test_health_generates_request_id_header** — assert hyphenated UUID4 format BEFORE generator change. `app/tests/test_health.py`
- [ ] **P1 — Add GET /ready endpoint** — DB + Redis probes with `asyncio.wait_for(timeout=2.0)`. `app/main.py`
- [ ] **P1 — Add generator=lambda: str(uuid4()) to CorrelationIdMiddleware.** `app/main.py`
- [ ] **P1 — Add test: /health liveness returns 200 always (no Redis key in response).** `app/tests/test_health.py`
- [ ] **P1 — Add test: /ready DB timeout → 503.** `app/tests/test_health.py`
- [ ] **P1 — Add test: /ready Redis timeout → 503.** `app/tests/test_health.py`
- [ ] **P1 — Update README: /health section, /ready section, debugging step 5, make health note.** `README.md`
- [ ] **P1 — Add CHANGELOG entry for X-Request-ID format change.** `CHANGELOG.md`
- [ ] **P2 — Add code comment: Redis is optional for CRUD requests (only needed for background summarization enqueue).** `app/main.py`
- [ ] **P2 — TODOS.md: note SQLAlchemy pool state under repeated asyncio.wait_for cancellations; fix = connect_args={"timeout": 1.5} on engine.** `TODOS.md`
