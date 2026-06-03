"""FastAPI application."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from asgi_correlation_id import CorrelationIdMiddleware
from asgi_correlation_id.middleware import is_valid_uuid4
from fastapi import Depends, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api import tickets
from app.arq_pool import get_arq_pool, set_arq_pool
from app.config import settings
from app.database import check_database_connection
from app.errors import (
    INTERNAL_SERVER_ERROR_DETAIL,
    INTERNAL_SERVER_ERROR_TYPE,
    TicketError,
)
from app.logging_config import setup_logging

# Configure JSON logging before anything else (including FastAPI app creation).
# Uvicorn installs its own handlers after the app object is created; calling
# setup_logging() here — at module level — ensures our formatter is in place
# before uvicorn can overwrite the root logger.
setup_logging(settings.log_level)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage the arq Redis connection pool for the lifetime of the process.

    Redis is optional at startup: if unavailable, the API still serves requests
    and enqueue is skipped (same best-effort behavior as per-request enqueue).
    """
    set_arq_pool(None)
    _parsed = urlparse(settings.redis_url)
    # Strip credentials from the URL before logging — netloc may contain a password.
    _safe_netloc = (
        f"{_parsed.hostname}:{_parsed.port}"
        if _parsed.port
        else (_parsed.hostname or "")
    )
    _redis_host = urlunparse(
        (
            _parsed.scheme,
            _safe_netloc,
            _parsed.path,
            _parsed.params,
            _parsed.query,
            _parsed.fragment,
        )
    )
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        set_arq_pool(pool)
        logger.info("arq pool connected to %s", _redis_host)
    except Exception as exc:
        logger.error(
            "Could not connect to Redis at %s: %s. "
            "API will start without background enqueue; "
            "set REDIS_URL or start Redis with `docker compose up redis -d`.",
            _redis_host,
            exc,
        )
    try:
        yield
    finally:
        pool = get_arq_pool()
        if pool is not None:
            await pool.close()
            set_arq_pool(None)
            logger.info("arq pool closed")


app = FastAPI(
    title="Support Ticket Management System",
    description="REST API for managing customer support tickets",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware order matters: FastAPI adds middleware in LIFO order, so the
# middleware added FIRST here is the OUTERMOST (first to handle the request).
# CorrelationIdMiddleware must be outermost so the request ID is set before
# the Prometheus instrumentator records the request.
app.add_middleware(
    CorrelationIdMiddleware,
    generator=lambda: str(uuid4()),
    validator=is_valid_uuid4,
)

# HTTP metrics: request count, latency histogram, in-flight gauge.
# expose() registers GET /metrics on the default prometheus_client registry,
# which also includes the business counters from app.metrics.
Instrumentator().instrument(app).expose(app)

app.include_router(tickets.router)


@app.exception_handler(TicketError)
async def ticket_error_handler(request: Request, exc: TicketError) -> JSONResponse:
    """Render every domain error as the {detail, error_type} envelope.

    Registered on the base class, so every TicketError subclass is covered and
    a new one can't silently leak FastAPI's default error shape.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc), "error_type": exc.error_type},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Normalize Pydantic request-validation errors into the same envelope.

    Keeps the per-field detail under a stable ``errors`` key so the 422 shape
    matches the rest of the API instead of FastAPI's bare ``detail`` list.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": "request validation failed",
            "error_type": "validation_error",
            "errors": jsonable_encoder(exc.errors()),
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(
    request: Request, _exc: Exception
) -> JSONResponse:
    """Return a safe uniform envelope for unexpected errors.

    Domain and validation handlers take precedence via the exception MRO; this
    covers everything else (500) without leaking stack traces or internals.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": INTERNAL_SERVER_ERROR_DETAIL,
            "error_type": INTERNAL_SERVER_ERROR_TYPE,
        },
    )


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to interactive API docs."""
    return RedirectResponse(url="/docs")


async def _check_redis(pool: ArqRedis | None) -> bool:
    """Return True if Redis accepts a ping, False otherwise.

    Redis is optional for ticket CRUD — only needed to enqueue background
    summarization. Callers should treat False as a soft degraded signal, not
    a reason to restart the container.

    asyncio.CancelledError is NOT caught here (it's BaseException, not
    Exception) and propagates up to the caller, which is the correct behavior
    when the request task is cancelled mid-flight.
    """
    if pool is None:
        return False
    try:
        await pool.ping()
        return True
    except Exception:
        logger.warning("redis health check failed", exc_info=True)
        return False


@app.get("/health", tags=["health"])
async def health() -> JSONResponse:
    """Liveness probe: the process is alive. Used by Docker HEALTHCHECK.

    Returns 200 unconditionally — no I/O, no network calls. A container should
    only be restarted when the process itself is stuck, not when a dependency
    is briefly unavailable.
    """
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})


_READINESS_PROBE_TIMEOUT: float = 2.0


@app.get("/ready", tags=["health"])
async def ready(
    arq_pool: Annotated[ArqRedis | None, Depends(get_arq_pool)],
) -> JSONResponse:
    """Readiness probe: Postgres and Redis are both reachable.

    Returns 503 when either dependency is down or slow. Load balancers should
    route traffic away on 503; the container is not restarted.
    """
    try:
        db_ok = await asyncio.wait_for(
            check_database_connection(), timeout=_READINESS_PROBE_TIMEOUT
        )
    except asyncio.TimeoutError:
        db_ok = False
        logger.warning("database readiness probe timed out")

    try:
        redis_ok = await asyncio.wait_for(
            _check_redis(arq_pool), timeout=_READINESS_PROBE_TIMEOUT
        )
    except asyncio.TimeoutError:
        redis_ok = False
        logger.warning("redis readiness probe timed out")

    all_ok = db_ok and redis_ok
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={
            "status": "ok" if all_ok else "degraded",
            "database": "ok" if db_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    )
