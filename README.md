# Support Ticket Management System

A backend service for managing customer support tickets with asynchronous
processing. Built with FastAPI, PostgreSQL, and SQLAlchemy (async).

> **Status:** early development. This initial commit lays down the foundation —
> data models, migrations, configuration, and a working `/health` endpoint.
> The ticket CRUD API and background worker are in progress (see [Roadmap](#roadmap)).

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
- [ ] Ticket CRUD API (create, retrieve, list with pagination/filtering)
- [ ] Status transition validation
- [ ] Background worker (summary, priority, routing) via Redis queue
- [ ] Structured logging and metrics
- [ ] Seed data script

## License

Released under the [MIT License](LICENSE).
