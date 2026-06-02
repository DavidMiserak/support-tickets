"""FastAPI application."""

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

app = FastAPI(
    title="Support Ticket Management System",
    description="REST API for managing customer support tickets",
    version="0.1.0",
)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Redirect root to interactive API docs."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}
