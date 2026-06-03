"""arq WorkerSettings — entry point for the background worker process.

Run with: python -m arq app.worker.main.WorkerSettings
"""

import asyncio
import logging
from typing import Any

import arq
from arq.connections import RedisSettings

from app.config import settings
from app.database import async_session_factory
from app.logging_config import setup_logging
from app.worker.registry import initialize_backend
from app.worker.tasks import (
    assign_priority,
    detect_spam,
    route_ticket,
    summarize_ticket,
)

# Configure JSON logging at module level before arq starts any threads.
setup_logging(settings.log_level)

logger = logging.getLogger(__name__)


async def on_startup(ctx: dict[str, Any]) -> None:
    """Initialize resources shared across all tasks in this worker process."""
    logger.info("worker: starting up")
    loop = asyncio.get_running_loop()
    ctx["summarizer"] = await loop.run_in_executor(None, initialize_backend)
    ctx["session_factory"] = async_session_factory
    logger.info("worker: ready")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Release resources on worker shutdown."""
    logger.info("worker: shutting down")


class WorkerSettings:
    """arq worker configuration."""

    functions = [
        arq.func(summarize_ticket, max_tries=1),
        arq.func(assign_priority, max_tries=1),
        arq.func(detect_spam, max_tries=1),
        arq.func(route_ticket, max_tries=1),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = 10
    job_timeout = 300  # 5 min — covers worst-case CPU inference
