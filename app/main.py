"""FastAPI application."""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.api import tickets
from app.errors import TicketError

app = FastAPI(
    title="Support Ticket Management System",
    description="REST API for managing customer support tickets",
    version="0.1.0",
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


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to interactive API docs."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
