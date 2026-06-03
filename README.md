# Support Ticket Management System

A backend service for managing customer support tickets with asynchronous
processing. Built with FastAPI, PostgreSQL, and SQLAlchemy (async).

> **Status:** complete. Ticket CRUD API, async background worker (summary,
> priority, spam detection, department routing), structured JSON logging,
> Prometheus metrics, liveness/readiness probes — all running in a single
> `make run`. Worker results are visible in `GET /tickets/{id}` under `events`.

## Overview

The system lets customers create support tickets, agents update ticket status,
and background workers process tickets asynchronously (summary generation,
priority assignment, routing). It is structured in clean layers:

```text
API (routes)  ->  Service (business logic)  ->  Repository (data access)  ->  ORM
```

## Architecture Decisions

- **FastAPI** — native async, automatic OpenAPI docs, Pydantic validation.
- **asyncpg + SQLAlchemy 2.0 (async)** — true async database access with
  connection pooling.
- **Alembic** — version-controlled schema migrations.
- **Layered design** — routes, services, and repositories are separated so each
  layer is testable in isolation.
- **Enums for domain constants** — ticket status, priority, and category are
  enforced at both the schema and database level.

## Tech Stack

- Python 3.12+
- FastAPI
- PostgreSQL 16
- SQLAlchemy 2.0 (async) + asyncpg
- Alembic
- Docker / Podman + Compose

## Setup

Requires Docker (or Podman) and Compose. No local Python install needed to run
the containerized stack.

```bash
cp .env.example .env     # adjust if needed
make run                 # build and start all services (migrations run automatically)
make health              # verify the API is up
make seed                # load sample agents and tickets (idempotent)
```

The API is served at `http://localhost:8000`. The root path redirects to the
interactive docs.

### Local development (without containers)

```bash
make install-dev         # create .venv and install deps
make test-db             # start PostgreSQL in a container
make local-run           # run uvicorn with autoreload
```

To run the worker locally with DistilBART summarization:

```bash
make install-ml          # adds torch + transformers (see requirements-ml.txt)
SUMMARIZER_BACKEND=transformer python -m arq app.worker.main.WorkerSettings
```

## Environment Variables

| Variable              | Description                                  | Default (Compose)                                                      |
|-----------------------|----------------------------------------------|------------------------------------------------------------------------|
| `DATABASE_URL`        | Async SQLAlchemy connection URL (`+asyncpg`) | `postgresql+asyncpg://ticketsupport:ticketsupport@db:5432/ticketsupport` |
| `REDIS_URL`           | Redis URL for the worker queue               | `redis://redis:6379`                                                   |
| `SUMMARIZER_BACKEND`  | Worker summarizer: `noop` or `transformer`   | `noop` (worker service only)                                           |
| `LOG_LEVEL`           | Log verbosity: `DEBUG` `INFO` `WARNING` `ERROR` `CRITICAL` | `INFO`                                                  |
| `DEBUG`               | Enable debug behavior                        | `false`                                                                |
| `DB_POOL_SIZE`        | SQLAlchemy connection pool size per process  | `20`                                                                   |
| `DB_MAX_OVERFLOW`     | Max connections above pool size per process  | `10`                                                                   |

## API Documentation

Once running, interactive docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI schema: `http://localhost:8000/openapi.json`

## API Usage

Enum values (`status`, `priority`, `category`) are accepted case-insensitively
and echoed back in canonical upper case.

**Source of truth:** [`scripts/demo-api.sh`](scripts/demo-api.sh) runs the full
happy path plus error-envelope checks against a live API. It captures ticket
ids from responses (no hardcoded `1`), seeds agents when a compose `api`
container is running, and exits non-zero on the first failure.

```bash
make run          # start API + Postgres + Redis + worker
make demo-api     # or: ./scripts/demo-api.sh
```

By default the script also exercises async worker events when Redis is healthy
(`DEMO_WORKER=auto`). Set `DEMO_WORKER=0` to skip that block, or `VERBOSE=1` to
print JSON bodies. See `./scripts/demo-api.sh --help`.

The curls below mirror what the script exercises (replace `{id}` with a real
ticket id from `POST /tickets`):

```bash
# Create a ticket (201). Priority defaults to MEDIUM; category is case-insensitive.
curl -s -X POST http://localhost:8000/tickets \
  -H 'Content-Type: application/json' \
  -d '{
        "customer_name": "Ada Lovelace",
        "customer_email": "ada@example.com",
        "subject": "Cannot reset my password",
        "description": "The reset link 404s after I click it.",
        "category": "technical"
      }'

# Retrieve one ticket with event history (200, or 404 if missing)
curl -s http://localhost:8000/tickets/{id}

# List with filters + pagination (200). Returns {items, total, skip, limit}.
curl -s 'http://localhost:8000/tickets?status=OPEN&priority=MEDIUM&category=TECHNICAL&skip=0&limit=20'

# Transition status (200). Illegal moves return 409; CLOSED is terminal.
curl -s -X PATCH http://localhost:8000/tickets/{id}/status \
  -H 'Content-Type: application/json' \
  -d '{"status": "in_progress"}'

# Assign to an agent (200). Run `make seed` first so agent ids exist.
curl -s -X PATCH http://localhost:8000/tickets/{id}/assign \
  -H 'Content-Type: application/json' \
  -d '{"agent_id": 1}'
```

### Errors

Every error returns a uniform envelope, `{"detail": ..., "error_type": ...}`
(request-validation failures add an `errors` list with per-field detail):

| `error_type`                 | HTTP | When                                             |
|------------------------------|------|--------------------------------------------------|
| `validation_error`           | 422  | Request body/params failed validation            |
| `ticket_not_found`           | 404  | No ticket with that id                            |
| `agent_not_found`            | 404  | No support agent with that id                     |
| `invalid_status_transition`  | 409  | Status move not allowed (e.g. out of `CLOSED`)    |
| `concurrent_update`          | 409  | Another request modified the ticket first (retry) |
| `internal_server_error`      | 500  | Unexpected server failure (details not exposed)     |

### Background processing

When Redis and the worker are running, creating a ticket enqueues four
background jobs that run asynchronously:

- One job per ticket per task, deduped by ticket id at enqueue time
- Ticket creation succeeds even if Redis is down or enqueue fails
- Each job runs at most once (`max_tries=1`); failures are logged, not retried

Worker results are written to `ticket_events` and returned in
`GET /tickets/{id}` under the `events` array (at most 100 events; when
truncated, `events_truncated` is true and `events_total` is the full count).

**Worker-emitted event types:**

| `event_type` | `field_changed` | `new_value` |
|---|---|---|
| `SUMMARIZED` | `summary` | Generated summary text (or original description with `noop` backend) |
| `PRIORITY_CHANGED` | `priority` | Upgraded priority (e.g. `"CRITICAL"`) — only if keywords detected; never downgrades |
| `SPAM_FLAGGED` | `spam` | `"true"` — ticket flagged for human review |
| `ROUTED` | `department` | Routing department (e.g. `"engineering"`, `"billing"`) |

**Demo — see the async loop end-to-end:**

```bash
make demo-api   # includes worker checks when Redis is up (DEMO_WORKER=auto)
```

The script polls until it sees `CREATED`, `SUMMARIZED`, `PRIORITY_CHANGED`,
`SPAM_FLAGGED`, and `ROUTED`, and asserts priority was upgraded to `CRITICAL`.

> **Note:** `POST /tickets` and `PATCH .../status` or `.../assign` return a flat
> ticket object (no `events`). Call `GET /tickets/{id}` to see the audit event
> history (bounded; see `events_truncated` when older rows are omitted).

**Summarizer backend:**

- With `SUMMARIZER_BACKEND=noop` (compose default): stored summary equals the
  ticket description unchanged
- With `SUMMARIZER_BACKEND=transformer`: uses `sshleifer/distilbart-cnn-6-6`
- Duplicate `SUMMARIZED` events are allowed (e.g. manual re-enqueue)

There is no re-summarize endpoint; a failed or skipped job is not backfilled
unless you add that explicitly later.

### Transformer summarizer (optional)

The default stack uses `noop` so containers start without ML dependencies.
To enable DistilBART (`sshleifer/distilbart-cnn-6-6`):

1. Install optional deps: `make install-ml` or `pip install -r requirements-ml.txt`
2. Set `SUMMARIZER_BACKEND=transformer` for the worker process
3. On first run, HuggingFace downloads ~300 MB of weights to the local cache

The worker defaults to `max_jobs=10`, but the HuggingFace pipeline object is
**not thread-safe**. `TransformerSummarizer` serializes inference with a
process-wide lock so concurrent arq jobs do not corrupt shared model state.
If you need higher throughput, run multiple worker replicas (each with its own
process and model copy) rather than raising `max_jobs` alone.

The runtime `Containerfile` does not include `requirements-ml.txt`; extend the
worker image (e.g. `RUN pip install -r requirements-ml.txt` in a custom build)
if you want transformer mode in Compose.

## Observability

### Structured JSON logs

All log output is emitted as newline-delimited JSON. Every line includes
`timestamp`, `level`, `logger`, `message`, and any structured fields from the
call site (e.g. `ticket_id`, `elapsed_seconds`). API request lines also carry
`request_id`, which matches the `X-Request-ID` response header — use it to
correlate all lines for a single request, including worker jobs enqueued by that
request.

```bash
# Tail JSON logs from the running stack
docker compose logs -f api
docker compose logs -f worker

# Set DEBUG for verbose output
LOG_LEVEL=DEBUG docker compose up
```

### Correlation IDs

Every API request gets an `X-Request-ID` header in the response. Pass the same
header on the request to inject your own trace ID (must be a valid UUID4):

```bash
curl -H "X-Request-ID: 550e8400-e29b-41d4-a716-446655440000" \
     http://localhost:8000/health
```

### Prometheus metrics

```bash
# Scrape all metrics
curl http://localhost:8000/metrics
```

HTTP metrics (`http_requests_total`, `http_request_duration_seconds`,
`http_requests_inprogress`) are collected automatically per route.

Custom business counters:

| Metric | Labels | Description |
|--------|--------|-------------|
| `tickets_created_total` | — | Tickets created via `POST /tickets` |
| `ticket_status_transitions_total` | `from_status`, `to_status` | Successful status transitions |
| `ticket_summarization_outcomes_total` | `outcome` | Enqueue outcomes at create time |

`outcome` label values: `enqueued`, `deduped`, `skipped_no_pool`, `enqueue_failed`.

### Debugging a failed summarization (example flow)

1. `docker compose logs -f worker | grep '"ticket_id": 42'`
   — find all worker log lines for ticket 42.
2. From the complete/error line, copy `"request_id": "abc-123"`.
3. `docker compose logs -f api | grep '"request_id": "abc-123"'`
   — find the `POST /tickets` that created it and its latency.
4. `curl http://localhost:8000/metrics | grep ticket_summarization_outcomes`
   — check outcome counters to see how many jobs are failing vs. succeeding.
5. `curl http://localhost:8000/ready`
   — if `"redis": "unavailable"`, the worker never received the job.

### Health endpoints

The API exposes two separate probes — a standard pattern for containerised services:

**`GET /health` — liveness probe**

Returns 200 unconditionally. No network calls, no dependency checks. The Docker
`HEALTHCHECK` in `Containerfile` targets this endpoint so a Redis or Postgres
blip never restarts the container.

```bash
curl http://localhost:8000/health
# {"status": "ok"}
make health   # same — targets liveness only
```

**`GET /ready` — readiness probe**

Probes both Postgres and Redis (each with a 2-second timeout). Returns 200 when
both are healthy; 503 when either is degraded. Use this for load-balancer health
checks so traffic is held back when a dependency is temporarily unreachable.

```bash
curl http://localhost:8000/ready
# {"status": "ok",      "database": "ok",         "redis": "ok"}
# {"status": "degraded","database": "unavailable", "redis": "ok"}
# {"status": "degraded","database": "ok",          "redis": "unavailable"}
```

## Testing

```bash
make test         # run the test suite in a container (falls back to local)
make coverage     # run tests with a coverage report
make test-ml      # print a sample DistilBART summary + run ML integration test
```

Tests run against an isolated `ticketsupport_test` database.

## Connection pool

Each Python process (API **and** worker) creates its own SQLAlchemy pool
(`DB_POOL_SIZE=20`, `DB_MAX_OVERFLOW=10` by default → up to **30**
connections per process; tune via environment variables).
The default Compose stack runs **two** processes, so plan for roughly
**60** concurrent Postgres connections under burst load.

PostgreSQL's default `max_connections` is **100**, which leaves modest headroom
for admin sessions and migration tooling. Before scaling out — multiple uvicorn
workers, several worker replicas, or other services on the same database —
either lower per-process pool settings or raise `max_connections` in Postgres.

The worker holds at most `max_jobs=10` concurrent arq tasks; each
`summarize_ticket` job uses two short DB sessions (read ticket, then write
event), so jobs do not hold a connection open during CPU-bound summarization.

## Common Commands

```bash
make help         # list all available targets
make run          # build and start the stack
make health       # curl the /health liveness probe
make migrate      # apply database migrations in the running container
make install-ml   # optional: torch + transformers for local worker ML runs
make seed         # sample agents + tickets for local testing (idempotent)
make clean        # remove caches and build artifacts
```

### Seed data

After the stack is up, run `make seed` to insert three support agents and five
sample tickets covering every status (`OPEN`, `IN_PROGRESS`, `RESOLVED`, `CLOSED`),
multiple categories, and representative audit events (including worker-style
`SUMMARIZED` / `ROUTED` / `SPAM_FLAGGED` rows on selected tickets). Re-running
seed skips rows that already exist (matched by agent email or ticket email+subject).

```bash
make seed
curl -s 'http://localhost:8000/tickets?limit=10' | jq '.total, .items[].subject'
curl -s http://localhost:8000/tickets/2 | jq '.status, .events'
```

## Roadmap

- [x] Project foundation: models, migrations, config, `/health`
- [x] Ticket CRUD API (create, retrieve, list with pagination/filtering)
- [x] Status transition validation (state machine + optimistic locking)
- [x] Background worker — summarize-at-create, best-effort once
  (arq + pluggable backends)
- [x] Structured JSON logging, Prometheus metrics, correlation IDs
- [x] Observability hardening: liveness/readiness split, UUID4 correlation
  IDs, probe timeouts
- [x] Additional worker tasks — priority upgrade, spam detection, department
  routing (heuristic stubs; all write audit events)
- [x] Worker results visible in `GET /tickets/{id}` under `events`
- [x] Assign-agent REST endpoint — `PATCH /tickets/{id}/assign` sets
  `assigned_agent_id`, validates agent exists (404 `agent_not_found`), writes
  `ASSIGNED` audit event; idempotent when already assigned to the same agent
- [ ] **Authentication — deferred.** All endpoints are unauthenticated in this
  take-home scope. In production: JWT bearer tokens issued at login, verified via
  a FastAPI `Depends` guard, with the resolved agent/user identity passed as
  `actor_id` on audit events. API key middleware is a simpler alternative for
  server-to-server callers.

## License

Released under the [MIT License](LICENSE).
