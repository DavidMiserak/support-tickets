"""arq WorkerSettings — entry point for the background worker process.

Run with: python -m arq app.worker.main.WorkerSettings
"""

import logging
from typing import Any

from arq.connections import RedisSettings

from app.config import settings
from app.database import async_session_factory
from app.worker.registry import initialize_backend
from app.worker.tasks import summarize_ticket

logger = logging.getLogger(__name__)


async def on_startup(ctx: dict[str, Any]) -> None:
    """Initialize resources shared across all tasks in this worker process."""
    logger.info("worker: starting up")
    ctx["summarizer"] = initialize_backend()
    ctx["session_factory"] = async_session_factory
    logger.info("worker: ready")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Release resources on worker shutdown."""
    logger.info("worker: shutting down")


class WorkerSettings:
    """arq worker configuration."""

    functions = [summarize_ticket]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 300  # 5 min — covers worst-case CPU inference
