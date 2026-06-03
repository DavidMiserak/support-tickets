# Multi-stage build: dependencies are compiled into an isolated virtualenv in
# the builder stage, then copied into a minimal runtime image that runs as an
# unprivileged user.
#
# Stages:
#   builder      base deps (requirements.txt) → /opt/venv
#   builder-ml   builder + optional ML deps (requirements-ml.txt)
#   test         builder + dev deps; runs pytest (compose `test` profile)
#   runtime-base shared OS/user/env/healthcheck — no venv or source yet
#   runtime-ml   ML-enabled image (built explicitly via `--target runtime-ml`)
#   runtime      lean default image — MUST stay last so a target-less build
#                (e.g. `make run`) produces the lean image, not the ML one.

# ---- Builder ----------------------------------------------------------------
FROM docker.io/python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Build into a self-contained virtualenv so the runtime stage only needs the
# resulting directory (no pip, no build caches).
RUN python -m venv "$VIRTUAL_ENV"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- Builder (ML) -----------------------------------------------------------
# Layers the optional ML dependencies (torch + transformers) on top of the base
# venv. Built only for the runtime-ml stage; never pulled into the lean runtime.
FROM builder AS builder-ml

COPY requirements-ml.txt .
RUN pip install -r requirements-ml.txt

# ---- Test -------------------------------------------------------------------
# Adds dev/test dependencies on top of the builder venv. Built explicitly via
# `--target test` (e.g. the compose `test` service); never shipped to runtime.
FROM builder AS test

COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

COPY . .

CMD ["pytest", "-q", "app/tests"]

# ---- Runtime base -----------------------------------------------------------
# Shared runtime scaffolding for both the lean and ML images: OS env, an
# unprivileged user, healthcheck, and default command. The venv and application
# source are added by the leaf stages so each can choose which venv to copy.
FROM docker.io/python:3.12-slim AS runtime-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Create an unprivileged user/group with fixed ids and a home for the app.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app app

WORKDIR /app

EXPOSE 8000

ARG VERSION=dev
LABEL org.opencontainers.image.title="support-ticket-api" \
      org.opencontainers.image.description="Support Ticket Management System — FastAPI + PostgreSQL + arq" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"

# Liveness probe that avoids shipping curl/wget in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

# Apply migrations, then launch uvicorn (see scripts/start.sh).
CMD ["sh", "scripts/start.sh"]

# ---- Runtime (ML) -----------------------------------------------------------
# ML-enabled image: same scaffolding as runtime but with the ML venv. Build via
# `--target runtime-ml` (see compose.ml.yaml / `make run-ml`). HuggingFace model
# weights download on first worker start; point HF_HOME at a mounted volume so
# the ~1 GB download is not repeated on every restart.
FROM runtime-base AS runtime-ml

ENV HF_HOME=/opt/hf-cache

# Pre-create the cache dir owned by the app user so a fresh named volume mounted
# here keeps app-writable ownership.
RUN mkdir -p /opt/hf-cache && chown app:app /opt/hf-cache

COPY --from=builder-ml --chown=app:app /opt/venv /opt/venv

# Copy application source last so dependency layers stay cached across changes.
COPY --chown=app:app . .

USER app

# ---- Runtime (default, lean) ------------------------------------------------
# Last stage on purpose: a build with no --target produces this image.
FROM runtime-base AS runtime

# Bring in the pre-built virtualenv from the builder stage.
COPY --from=builder --chown=app:app /opt/venv /opt/venv

# Copy application source last so dependency layers stay cached across changes.
COPY --chown=app:app . .

USER app
