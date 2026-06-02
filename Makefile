# file: Makefile

PYTHON := .venv/bin/python
PIP := .venv/bin/pip
UVICORN := .venv/bin/uvicorn

# Container runtime: autodetect podman, else docker. Override with RUNTIME=...
RUNTIME ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
COMPOSE := $(RUNTIME) compose
SONAR_SCANNER ?= sonar-scanner

# Local tests use an isolated DB to avoid colliding with the app DB.
TEST_DATABASE_URL ?= postgresql+asyncpg://ticketsupport:ticketsupport@localhost:5432/ticketsupport_test
export TEST_DATABASE_URL

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Project Commands"
	@echo "================"
	@echo ""
	@echo "Environment:"
	@echo "  local-venv         Create .venv if missing"
	@echo "  install            Install runtime dependencies"
	@echo "  install-dev        Install runtime + test dependencies"
	@echo ""
	@echo "Quality:"
	@echo "  test               Run tests in container (default), fallback to local"
	@echo "  coverage           Run coverage in container (default), fallback to local"
	@echo "  local-test         Run tests locally against compose Postgres"
	@echo "  local-coverage     Run coverage locally against compose Postgres"
	@echo "  validate           Validate local FastAPI/tooling setup"
	@echo "  pre-commit-setup   Install and run pre-commit hooks"
	@echo "  sonar              Run Sonar scan (requires SONAR_ORGANIZATION + SONAR_TOKEN)"
	@echo ""
	@echo "Container:"
	@echo "  (default runtime: $(RUNTIME); override with RUNTIME=docker)"
	@echo "  container-config   Validate compose file"
	@echo "  container-up       Build and start API + DB"
	@echo "  container-down     Stop and remove compose services"
	@echo "  container-logs     Tail API logs"
	@echo "  container-test     Run tests in test container"
	@echo "  container-coverage Run coverage in test container (writes coverage.xml)"
	@echo "  migrate            Apply DB migrations in running API container"
	@echo "  seed               Seed sample customers in running API container"
	@echo ""
	@echo "Run:"
	@echo "  run                Run API in container (first-class default)"
	@echo "  local-run          Run API locally with uvicorn (reload)"
	@echo "  health             Check API /health endpoint"
	@echo ""
	@echo "Maintenance:"
	@echo "  clean              Remove caches and build artifacts"
	@echo "  clean-all          clean + remove .venv"

.PHONY: local-venv
local-venv:
	@test -d .venv || python -m venv .venv

.PHONY: local-install
local-install: local-venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

.PHONY: install
install: local-install

.PHONY: local-install-dev
local-install-dev: local-venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

.PHONY: install-dev
install-dev: local-install-dev

.PHONY: pre-commit-setup
pre-commit-setup:
	pre-commit install
	pre-commit install --install-hooks
	pre-commit run --all-files

.PHONY: local-validate
local-validate: local-install
	pre-commit validate-config
	$(PYTHON) -c "import tomllib; tomllib.load(open('pyproject.toml','rb')); print('pyproject ok')"
	$(PYTHON) -c "from app.main import app; print(app.title)"

.PHONY: test-db
test-db:
	$(COMPOSE) up -d db
	@echo "Waiting for Postgres to become ready..."
	@for i in $$(seq 1 30); do \
		$(COMPOSE) exec -T db pg_isready -U ticketsupport -d ticketsupport >/dev/null 2>&1 && exit 0; \
		sleep 1; \
	done; \
	echo "Error: Postgres did not become ready in time" >&2; exit 1
	@echo "Ensuring test database exists..."
	@$(COMPOSE) exec -T db sh -c 'psql -U ticketsupport -d ticketsupport -tAc "SELECT 1 FROM pg_database WHERE datname='\''ticketsupport_test'\''" | grep -q 1 || psql -U ticketsupport -d ticketsupport -c "CREATE DATABASE ticketsupport_test;"'

.PHONY: local-test
local-test: install-dev test-db
	$(PYTHON) -m pytest app/tests -q

.PHONY: local-coverage
local-coverage: install-dev test-db
	$(PYTHON) -m coverage erase
	$(PYTHON) -m coverage run -m pytest app/tests -q
	$(PYTHON) -m coverage report -m
	$(PYTHON) -m coverage xml -o coverage.xml

.PHONY: container-test
container-test:
	$(COMPOSE) --profile test build test
	$(COMPOSE) --profile test run --rm test
	$(COMPOSE) down

.PHONY: container-coverage
container-coverage:
	$(COMPOSE) --profile test build test
	$(COMPOSE) --profile test run --rm -v "$(PWD):/app" test \
		sh -c "coverage erase && coverage run -m pytest app/tests -q && coverage report -m && coverage xml -o coverage.xml"
	$(COMPOSE) down

.PHONY: test
test:
	@$(MAKE) container-test || $(MAKE) local-test

.PHONY: coverage
coverage:
	@$(MAKE) container-coverage || $(MAKE) local-coverage

.PHONY: sonar
sonar:
	@test -n "$(SONAR_ORGANIZATION)" || (echo "Error: set SONAR_ORGANIZATION"; exit 1)
	@test -n "$(SONAR_TOKEN)" || (echo "Error: set SONAR_TOKEN"; exit 1)
	@test -f coverage.xml || $(MAKE) coverage
	@test -f coverage.xml || (echo "Error: coverage.xml not found after coverage run"; exit 1)
	$(SONAR_SCANNER) \
		-Dsonar.organization="$(SONAR_ORGANIZATION)" \
		-Dsonar.token="$(SONAR_TOKEN)"

.PHONY: run
run: container-up

.PHONY: local-run
local-run: install
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8000 --reload

.PHONY: health
health:
	curl -fsS http://localhost:8000/health
	@echo ""

.PHONY: container-config
container-config:
	$(COMPOSE) config

.PHONY: container-up
container-up:
	$(COMPOSE) up --build -d

.PHONY: container-down
container-down:
	$(COMPOSE) down

.PHONY: container-logs
container-logs:
	$(COMPOSE) logs -f api

.PHONY: migrate
migrate:
	$(COMPOSE) exec api alembic upgrade head

.PHONY: seed
seed:
	$(COMPOSE) exec api python -m scripts.seed

.PHONY: clean
clean:
	find . \( -path ./.venv -o -path ./.git \) -prune -o \
		-type d -name '__pycache__' -exec rm -rf {} +
	find . \( -path ./.venv -o -path ./.git \) -prune -o \
		-type f -name '*.py[co]' -exec rm -f {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache .scannerwork \
		build dist *.egg-info .coverage coverage.xml

.PHONY: clean-all
clean-all: clean
	rm -rf .venv
