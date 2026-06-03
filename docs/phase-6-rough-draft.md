<!-- /autoplan restore point: /home/david/.gstack/projects/DavidMiserak-support-tickets/feat-polish-autoplan-restore-20260603-072216.md -->
# Phase 6 — Docker/Deploy Polish (ROUGH DRAFT)

Status: APPROVED (autoplan 2026-06-03, subagent-only) — 11 auto-decisions, 0 taste choices. See Decision Audit Trail and GSTACK REVIEW REPORT below.
Branch: feat/polish
Depends on: Phase 5 (observability hardening) — done and merged.

---

## Goal

Close four Docker/deploy gaps identified as the project approaches its final
planned phase. No new features. The app should be deployable to any OCI-capable
platform with clear migration ownership, no risk of credentials leaking into
images, and visible container metadata.

After this phase:
- Only one service (`api`) runs `alembic upgrade head` at startup; the `worker`
  waits for the api to be healthy before starting, eliminating the migration race.
- `.env` and related files are excluded from image builds so local secrets can
  never be baked into a container image by accident.
- The `api` service has an explicit healthcheck in `compose.yaml`, making
  `condition: service_healthy` usable by any dependent service.
- The runtime image carries OCI standard `LABEL` annotations
  (`org.opencontainers.image.*`) so images are discoverable and self-describing
  in a registry.

---

## Background: the migration race

Both `api` (via `scripts/start.sh`) and `worker` (via inline command in
`compose.yaml`) currently run `alembic upgrade head` at startup. Both depend on
`db: condition: service_healthy`, so they start in parallel:

```
db → api  (alembic + uvicorn)
   ↘ worker (alembic + arq)     ← second alembic run is redundant
```

Alembic uses a Postgres advisory lock, so concurrent runs do not corrupt
the migration state. However:
- Migrations are applied twice on every `compose up`.
- Ownership is ambiguous — which service is the source of truth?
- Any future migration that is not idempotent (data backfill, type change)
  is more fragile when two processes claim the right to run it.

Fix: the `api` owns migrations. The `worker` depends on the api being
healthy (via `condition: service_healthy`). Worker startup command becomes
`python -m arq app.worker.main.WorkerSettings` (no `alembic` prefix).

---

## In scope

**Fix 1 — Migration ownership: api runs migrations, worker waits**
- Add explicit `healthcheck` block to `api` service in `compose.yaml`:
  ```yaml
  healthcheck:
    test: ["CMD", "python", "-c",
           "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
    interval: 10s
    timeout: 5s
    start_period: 30s
    retries: 5
  ```
- Change `worker` `depends_on` to add `api: condition: service_healthy`.
- Remove `alembic upgrade head &&` from the worker `command` in `compose.yaml`.

Rationale: api is the service whose startup process (`scripts/start.sh`)
already runs `alembic upgrade head` before uvicorn starts. Making the worker
wait for api-healthy is a one-line change; it guarantees migrations are applied
before the worker processes any jobs.

**Fix 2 — `.containerignore` excludes `.env` files**
Add to `.containerignore`:
```
.env
.env.*
.env.local
.env.development
.env.production
```

Rationale: if a developer has a `.env` file with real credentials (e.g.
`DATABASE_URL` with a production password), it is currently copied verbatim
into any image built with `make run`. Excluding `.env*` from the build context
is a single-line fix that prevents credential leakage.

**Fix 3 — OCI image labels in Containerfile**
Add to the `runtime` stage of `Containerfile`:
```dockerfile
ARG VERSION=dev
LABEL org.opencontainers.image.title="support-ticket-api" \
      org.opencontainers.image.description="Support Ticket Management System — FastAPI + PostgreSQL + arq" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
```

Pass `--build-arg VERSION=$(cat VERSION)` when building if you want the actual
version stamped. The `dev` default is safe for local builds.

Rationale: OCI labels are the standard way to annotate images in registries
(Docker Hub, GHCR, Quay). Without them, `docker inspect` shows no description
or version. Labels cost nothing at runtime.

**Tests**
No automated tests for Docker/compose changes. Manual test protocol:
- `make run` — verify stack starts, api becomes healthy, worker starts after.
- `docker compose ps` — confirm api `(healthy)` before worker shows running.
- Build with a `.env` file present — verify `docker inspect` shows env vars
  sourced only from compose.yaml, not the `.env` file.
- `docker inspect <image> | jq '.[0].Config.Labels'` — verify OCI labels.

## Out of scope

- **SQLAlchemy pool state under `asyncio.wait_for` cancellation.** Deferred in
  Phase 5; still deferred. Low risk for single-instance deployment.
- **`/ready` probes run sequentially.** Deferred in Phase 5; still deferred.
- **`async_session` → `async_session_factory` rename.** Standalone PR, not bundled.
- **Phase 4b (AnthropicSummarizer).** Ships as its own standalone PR.
- **Python base image version upgrade.** `python:3.12-slim` is current LTS.
  Local dev uses 3.14; aligning would require verifying all deps. Out of scope
  for polish; revisit when 3.12 reaches EOL.
- **Multi-arch / `platform` directive.** Not needed for a learning project
  targeting a single deployment architecture.

---

## Architecture

```
compose up:
  db (postgres) ──────────────────────── healthy
  redis ──────────────────────────────── healthy
                  ↓                         ↓
                  api (start.sh: alembic + uvicorn) ── healthy
                            ↓
                            worker (arq, no alembic)
```

Worker startup order is now deterministic: migrations are applied by the api
(which is also the service that performs DB writes in the request path), and
the worker starts only after the api confirms it's alive.

---

## Files touched

Modified:
- `compose.yaml` — add api healthcheck; update worker depends_on; remove alembic from worker command; add `restart: unless-stopped` comment; add start_period rationale comment
- `.containerignore` — add `.env`, `.env.*`, `*.env` exclusions
- `Containerfile` — add `ARG VERSION` + `LABEL` to runtime stage
- `Makefile` — add `make ready` target (hits `/ready`; fixes `make health` post-Phase-5-split mismatch)

No new files. No migrations. No new dependencies. No Python code changes.

### `make ready` target (added in DX review)

```makefile
.PHONY: ready
ready:
	curl -fsS http://localhost:8000/ready
	@echo ""
```

Also update help text for `health` target:
- Before: `"Check API /health endpoint"`
- After: `"Check API liveness (pure liveness, no DB/Redis check — use 'make ready' for full readiness)"`

And add `ready` to help output: `"  ready              Check API /ready endpoint (Postgres + Redis reachability)"`

---

## Open questions for review

*(All open questions resolved during autoplan — see Decision Audit Trail below.)*

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | CEO | Select Approach A (current plan: 3 files, all 4 concerns). | Mech | P1 | Verified against code; all premises correct; Approach A is completeness at minimal cost |
| 2 | CEO | D1 resolved: worker depends on `api: condition: service_healthy`. Remove alembic from worker command; specify exact command: `python -m arq app.worker.main.WorkerSettings` (no sh -c wrapper). | Mech | P5 | Subagent and own analysis agree: api-healthy is semantically correct since no jobs arrive before api is serving; 30s startup delay is acceptable |
| 3 | CEO | Add one-sentence rationale to Fix 1: compose-level healthcheck overrides Containerfile HEALTHCHECK; `/health` is correct target (pure liveness, no I/O after migrations run); explains `start_period: 30s` chosen to give migrations headroom (vs Containerfile's 10s). | Mech | P5 | Subagent finding: undocumented override is a future maintainer trap |
| 4 | CEO | Test service migration concern (subagent Finding 5): FALSE ALARM. `test_migrations.py` targets `ticketsupport_test` (separate DB) via direct alembic subprocess; conftest uses `create_all/drop_all`. No action needed for test service. | Mech | P5 | Verified by reading test_migrations.py and conftest.py |
| 5 | CEO | Fix 2 security rationale: reframe as "build hygiene/habit" not "security mitigation" since compose.yaml already contains plaintext credentials. Accurate framing matters in a learning project. | Mech | P5 | Subagent observation: stating security as the primary rationale is misleading given compose hardcoded creds |
| 6 | Eng | Document `restart: unless-stopped` bypass: worker restarts skip `depends_on` re-check; safe because migrations are already applied. Add comment to compose.yaml. | Mech | P5 | Subagent finding: subtle behavioral difference worth a comment for learners |
| 7 | Eng | `.containerignore`: keep `.env` explicitly; replace redundant `.env.local`/`.env.development`/`.env.production` with `.env.*` glob; add `*.env` to cover `production.env`-style files. | Mech | P5 | Subagent: glob covers common variants; redundant explicit entries create maintenance confusion |
| 8 | Eng | Add inline comment to compose.yaml api healthcheck block: "This compose-level definition overrides the HEALTHCHECK in the Containerfile. Using /health (pure liveness) rather than /ready intentionally — worker should wait until uvicorn is serving, not until all deps are reachable." | Mech | P5 | Both phases flagged undocumented override |
| 9 | DX | Add `make ready` target to Makefile scope (hits `/ready`). Fixes DX gap: `make health` post-Phase 5 split now tests liveness only; `make ready` gives developers a way to verify full stack connectivity. | Mech | P2+P1 | Subagent finding: `make health` gives false sense of completeness for a fully operational stack; blast radius includes Makefile; 2-line addition |
| 10 | DX | Add debugging note to manual test protocol: "If api shows (unhealthy) after `make run`, run `make container-logs` to see the migration error. Worker will hang in (starting) until api is healthy." | Mech | P5 | Subagent finding: silent failure path with no actionable developer hint |
| 11 | DX | Note `--build-arg VERSION=$(cat VERSION)` as a known limitation: compose `up --build` does not forward build-args; a separate `docker compose build --build-arg VERSION=$(cat VERSION) api` is needed to stamp the version. Deferred — `dev` default is safe for learning project. | Deferred | P3 | DX subagent raised; documented limitation is better than silent unknown |

---

# GSTACK REVIEW REPORT

Voices: **subagent-only** (Codex binary not installed — dual-voice degraded).
Base branch: `main`. Branch: `feat/polish`.

## Phase 1 — CEO / Strategy

### CEO Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Premises valid?                    YES     [unavailable]  YES — all 4 verified against code
  2. Right problem to solve?            YES     [unavailable]  YES — correct final-phase hygiene items
  3. Scope calibration correct?         YES     [unavailable]  YES — 4 focused fixes, 3-4 files
  4. Alternatives explored?             YES     [unavailable]  YES — 3 approaches evaluated; A chosen
  5. Learning value sound?              YES     [unavailable]  YES — migration ownership, .containerignore, OCI labels
  6. 6-month trajectory sound?          YES     [unavailable]  YES — all decisions are reversible two-way doors
```

### CEO Findings
- **MEDIUM (resolved) — compose healthcheck overrides Containerfile HEALTHCHECK undocumented.** Decision 3+8: add inline comment explaining override and why `/health` is correct target for `condition: service_healthy`.
- **LOW (resolved) — D1 open question closed.** Worker depends on `api: condition: service_healthy`. Migration ownership moves to api. Decision 2.
- **LOW (false alarm) — test service migration isolation.** `test_migrations.py` targets `ticketsupport_test` independently. No action. Decision 4.
- **LOW (resolved) — Fix 2 rationale.** Reframed as build hygiene/habit, not security mitigation. Decision 5.

### CEO NOT in scope
- Python version alignment (3.12 container vs 3.14 local) — deferred; 3.12 is current LTS
- Multi-arch / `platform:` directive — out of scope for learning project
- Compose secrets management — out of scope; hardcoded creds are a known learning project constraint

### What already exists
- `scripts/start.sh` — already runs migrations for api; worker duplicates this unnecessarily
- Containerfile HEALTHCHECK — already targets `/health`; compose will override it with Phase 6 addition
- `make health` — exists but is misleading after Phase 5 split; Phase 6 adds `make ready`

### Error & Rescue Registry
| Error scenario | Detected by | Developer action |
|----------------|-------------|-----------------|
| alembic fails at startup | api goes `(unhealthy)` | `make container-logs` shows error |
| uvicorn fails to bind | api goes `(unhealthy)` | `make container-logs` shows bind error |
| Worker restarts after api crash | Worker restarts without depends_on re-check | Safe — migrations already applied |
| .env file present at build time | Excluded by .containerignore | No credentials in image |

## Phase 2 — Design
**SKIPPED** — no UI scope detected.

## Phase 3 — Engineering

### Eng Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Architecture sound?               YES     [unavailable]  YES — depends_on: service_healthy is correct primitive
  2. Test coverage sufficient?         YES     [unavailable]  YES — manual protocol proportionate for infra
  3. Performance risks addressed?      YES     [unavailable]  YES — no code paths added
  4. Security threats covered?         YES     [unavailable]  YES — .containerignore hygiene
  5. Error paths handled?              MOSTLY  [unavailable]  MOSTLY — restart bypass documented
  6. Deployment risk manageable?       YES     [unavailable]  YES — reversible 3→4 file change
```

### Eng Findings
- **MEDIUM (resolved) — restart: unless-stopped bypass undocumented.** Decision 6: add comment to compose.yaml noting worker restarts skip depends_on re-check; safe because migrations already applied.
- **LOW (resolved) — .containerignore redundant entries.** Decision 7: use `.env.*` glob; add `*.env`; drop redundant explicit entries.
- **LOW (resolved) — Worker command not specified.** Decision 2: exact replacement is `python -m arq app.worker.main.WorkerSettings` (no sh -c wrapper); PID 1 = arq = cleaner signal handling.

### Code Paths — Coverage Diagram
```
CODE PATHS (infrastructure)                  TEST STATUS
[+] compose.yaml
  ├── api healthcheck block                  Manual: docker compose ps (healthy)
  ├── worker depends_on: api                 Manual: timing log comparison
  └── worker command (EXEC form)             Manual: SIGTERM reaches arq
[+] .containerignore
  └── .env/.env.*/*.env exclusions           Manual: build + inspect image
[+] Containerfile (runtime stage)
  └── ARG VERSION + LABEL block              Manual: docker inspect | jq labels
[+] Makefile
  └── make ready target                      Manual: curl /ready via make ready

No Python code changes → no unit tests required.
Manual protocol is proportionate for infra-only changes.
COVERAGE: 4/4 change areas have manual test steps
```

### Failure Modes Registry
| Mode | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| api healthcheck too strict (start_period too low) | Low | Worker never starts | 30s start_period; generous retries=5 |
| .env exclusion gap (new .env variant) | Low | Credential leakage in edge case | Glob .env.* + *.env covers common variants |
| OCI label VERSION defaults to "dev" | Medium | Image metadata inaccurate | Documented limitation; pass --build-arg to fix |

## Phase 3.5 — Developer Experience

### DX Dual Voices — Consensus Table
```
  Dimension                            Claude  Codex          Consensus
  ──────────────────────────────────── ─────── ────────────── ─────────
  1. Getting started < 5 min?          YES     [unavailable]  YES — make run still works
  2. Naming guessable?                 YES     [unavailable]  YES — healthcheck syntax is standard
  3. Error messages actionable?        NO→YES  [unavailable]  YES (after Decision 10 — debug note added)
  4. Docs findable & complete?         MOSTLY  [unavailable]  YES (after make ready added)
  5. Upgrade path safe?                YES     [unavailable]  YES — reversible changes
  6. Dev environment friction-free?    NO→YES  [unavailable]  YES (after make ready added)
```

DX score: **6/10** initial → **8/10** after Decisions 9+10.

### DX Findings
- **MEDIUM (resolved) — `make health` misleading after Phase 5 split.** Decision 9: add `make ready` target pointing at `/ready`; update `make health` help text.
- **MEDIUM (resolved) — Silent failure when api unhealthy.** Decision 10: add debugging note to manual protocol.
- **LOW (deferred) — `--build-arg VERSION` not wired into container-up.** Decision 11: documented as known limitation.

## Cross-Phase Themes

- **Undocumented override (CEO + Eng both flagged):** compose healthcheck overrides Containerfile HEALTHCHECK — resolved by Decision 3+8 (add inline comments).
- **DX gap from Phase 5 split (Eng + DX both flagged):** `make health` liveness vs. `make ready` readiness distinction — resolved by Decision 9.

## Taste Decisions
None. All decisions were mechanical with clear correct answers.

## Implementation Tasks (aggregated)

- [ ] **P0 — compose.yaml: add api healthcheck block** with `start_period: 30s`, override rationale comment. `compose.yaml`
- [ ] **P0 — compose.yaml: update worker depends_on** to add `api: condition: service_healthy`. `compose.yaml`
- [ ] **P0 — compose.yaml: update worker command** to `python -m arq app.worker.main.WorkerSettings` (remove `sh -c "alembic upgrade head && "`). `compose.yaml`
- [ ] **P1 — compose.yaml: add restart bypass comment** to worker service explaining `restart: unless-stopped` skips depends_on on restarts. `compose.yaml`
- [ ] **P1 — .containerignore: add .env exclusions** (`.env`, `.env.*`, `*.env`). `.containerignore`
- [ ] **P1 — Containerfile: add ARG VERSION + LABEL block** to runtime stage. `Containerfile`
- [ ] **P1 — Makefile: add `make ready` target** hitting `/ready`; update `health` help text. `Makefile`
- [ ] **P2 — Manual test protocol: add debugging note** for api `(unhealthy)` → `make container-logs` path. `docs/phase-6-rough-draft.md`
- [ ] **P2 — Document --build-arg VERSION limitation** in plan and README if appropriate. `docs/phase-6-rough-draft.md`
