"""FastAPI application."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.api import tickets
from app.arq_pool import get_arq_pool, set_arq_pool  # noqa: F401 (re-exported)
from app.config import settings
from app.errors import (
    INTERNAL_SERVER_ERROR_DETAIL,
    INTERNAL_SERVER_ERROR_TYPE,
    TicketError,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage the arq Redis connection pool for the lifetime of the process."""
    try:
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        set_arq_pool(pool)
        logger.info("arq pool connected to %s", settings.redis_url)
    except Exception as exc:
        logger.error(
            "Could not connect to Redis at %s: %s. "
            "Set REDIS_URL or start Redis with `docker compose up redis -d`.",
            settings.redis_url,
            exc,
        )
        raise
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


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
