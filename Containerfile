# Multi-stage build: dependencies are compiled into an isolated virtualenv in
# the builder stage, then copied into a minimal runtime image that runs as an
# unprivileged user.

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

# ---- Test -------------------------------------------------------------------
# Adds dev/test dependencies on top of the builder venv. Built explicitly via
# `--target test` (e.g. the compose `test` service); never shipped to runtime.
FROM builder AS test

COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

COPY . .

CMD ["pytest", "-q", "app/tests"]

# ---- Runtime ----------------------------------------------------------------
FROM docker.io/python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Create an unprivileged user/group with fixed ids and a home for the app.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app app

WORKDIR /app

# Bring in the pre-built virtualenv from the builder stage.
COPY --from=builder --chown=app:app /opt/venv /opt/venv

# Copy application source last so dependency layers stay cached across changes.
COPY --chown=app:app . .

USER app

EXPOSE 8000

# Liveness probe that avoids shipping curl/wget in the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

# Apply migrations, then launch uvicorn (see scripts/start.sh).
CMD ["sh", "scripts/start.sh"]
