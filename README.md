# Support Ticket Management System

A backend service for managing customer support tickets with asynchronous
processing. Built with FastAPI, PostgreSQL, and SQLAlchemy (async).

> **Status:** active development. The ticket CRUD API is in place — create,
> retrieve, list (filter + paginate), and validated status transitions with a
> transactional audit trail and optimistic locking. The background worker is
> next (see [Roadmap](#roadmap)).

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

## Environment Variables

| Variable       | Description                                  | Default (Compose)                                                      |
|----------------|----------------------------------------------|------------------------------------------------------------------------|
| `DATABASE_URL` | Async SQLAlchemy connection URL (`+asyncpg`) | `postgresql+asyncpg://ticketsupport:ticketsupport@db:5432/ticketsupport` |
| `REDIS_URL`    | Redis URL for the worker queue (Phase 3)     | `redis://redis:6379`                                                   |
| `DEBUG`        | Enable debug behavior                        | `false`                                                                |

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

## Testing

```bash
make test         # run the test suite in a container (falls back to local)
make coverage     # run tests with a coverage report
```

Tests run against an isolated `ticketsupport_test` database.

## Common Commands

```bash
make help         # list all available targets
make run          # build and start the stack
make health       # curl the /health endpoint
make migrate      # apply database migrations in the running container
make clean        # remove caches and build artifacts
```

## Roadmap

- [x] Project foundation: models, migrations, config, `/health`
- [x] Ticket CRUD API (create, retrieve, list with pagination/filtering)
- [x] Status transition validation (state machine + optimistic locking)
- [ ] Assign-agent endpoint + seed data script
- [ ] Background worker (summary, priority, routing) via Redis queue
- [ ] Structured logging and metrics

## License

Released under the [MIT License](LICENSE).
