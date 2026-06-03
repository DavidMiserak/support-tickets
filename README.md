# Support Ticket Management System

A backend service for managing customer support tickets with asynchronous
processing. Built with FastAPI, PostgreSQL, and SQLAlchemy (async).

> **Status:** active development. The ticket CRUD API is in place — create,
> retrieve, list (filter + paginate), and validated status transitions with a
> transactional audit trail and optimistic locking. Background summarization
> runs summarize-at-create, best-effort once via arq when Redis and the worker
> are up (see [Background summarization](#background-summarization)).

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
make run                 # build and start API + PostgreSQL
make health              # verify the API is up
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

## API Documentation

Once running, interactive docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI schema: `http://localhost:8000/openapi.json`

## API Usage

Enum values (`status`, `priority`, `category`) are accepted case-insensitively
and echoed back in canonical upper case.

```bash
# Create a ticket (201). The smallest valid body needs name, email, subject,
# description, and category; priority defaults to MEDIUM.
curl -s -X POST http://localhost:8000/tickets \
  -H 'Content-Type: application/json' \
  -d '{
        "customer_name": "Ada Lovelace",
        "customer_email": "ada@example.com",
        "subject": "Cannot reset my password",
        "description": "The reset link 404s after I click it.",
        "category": "technical"
      }'

# Retrieve one ticket (200, or 404 if missing)
curl -s http://localhost:8000/tickets/1

# List with filters + pagination (200). Returns {items, total, skip, limit}.
curl -s 'http://localhost:8000/tickets?status=OPEN&skip=0&limit=20'

# Transition status (200). Illegal moves return 409; CLOSED is terminal.
curl -s -X PATCH http://localhost:8000/tickets/1/status \
  -H 'Content-Type: application/json' \
  -d '{"status": "in_progress"}'
```

### Errors

Every error returns a uniform envelope, `{"detail": ..., "error_type": ...}`
(request-validation failures add an `errors` list with per-field detail):

| `error_type`                 | HTTP | When                                             |
|------------------------------|------|--------------------------------------------------|
| `validation_error`           | 422  | Request body/params failed validation            |
| `ticket_not_found`           | 404  | No ticket with that id                            |
| `invalid_status_transition`  | 409  | Status move not allowed (e.g. out of `CLOSED`)    |
| `concurrent_update`          | 409  | Another request modified the ticket first (retry) |
| `internal_server_error`      | 500  | Unexpected server failure (details not exposed)     |

### Background summarization

When Redis and the worker are running, creating a ticket enqueues a
**summarize-at-create, best-effort once** job:

- One job per ticket, deduped by id at enqueue time (`_job_id`)
- Ticket creation succeeds even if Redis is down or enqueue fails
- The worker runs the job at most once (`max_tries=1`); failures are logged
  and not retried automatically
- With `SUMMARIZER_BACKEND=noop` (the compose default), the stored summary
  equals the ticket description unchanged
- Summaries are **internal only** for now: they are written as `SUMMARIZED`
  rows in `ticket_events`, not exposed on the ticket API
- Duplicate `SUMMARIZED` events are allowed (e.g. manual re-enqueue); there is
  no DB uniqueness constraint yet

There is no re-summarize endpoint yet; a failed or skipped job is not
backfilled unless you add that explicitly later.

### Connection budget

Each Python process (API **and** worker) creates its own SQLAlchemy pool
(`app/database.py`: `pool_size=20`, `max_overflow=10` → up to **30**
connections per process). The default Compose stack runs **two** processes, so
plan for roughly **60** concurrent Postgres connections under burst load.

PostgreSQL’s default `max_connections` is **100**, which leaves modest headroom
for admin sessions and migration tooling. Before scaling out — multiple uvicorn
workers, several worker replicas, or other services on the same database —
either lower per-process pool settings or raise `max_connections` in Postgres.

The worker holds at most `max_jobs=10` concurrent arq tasks; each
`summarize_ticket` job uses two short DB sessions (read ticket, then write
event), so jobs do not hold a connection open during CPU-bound summarization.

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

HTTP 200 when both components are healthy; 503 when either is degraded.

## Testing

```bash
make test         # run the test suite in a container (falls back to local)
make coverage     # run tests with a coverage report
make test-ml      # print a sample DistilBART summary + run ML integration test
```

Tests run against an isolated `ticketsupport_test` database.

## Common Commands

```bash
make help         # list all available targets
make run          # build and start the stack
make health       # curl the /health liveness probe
make migrate      # apply database migrations in the running container
make install-ml   # optional: torch + transformers for local worker ML runs
make clean        # remove caches and build artifacts
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
- [ ] Assign-agent endpoint + seed data script
- [ ] Additional worker tasks (priority, routing)

## License

Released under the [MIT License](LICENSE).
