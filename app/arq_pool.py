"""arq connection pool — shared state and FastAPI dependency.

Kept in its own module to avoid circular imports: main.py manages the
lifespan, tickets.py references the dependency, both import from here.
"""

import logging

from arq.connections import ArqRedis

logger = logging.getLogger(__name__)

_arq_pool: ArqRedis | None = None


def set_arq_pool(pool: ArqRedis | None) -> None:
    """Set the module-level pool (called from the lifespan in main.py)."""
    global _arq_pool
    _arq_pool = pool


def get_arq_pool() -> ArqRedis | None:
    """FastAPI dependency: returns the arq connection pool (or None)."""
    return _arq_pool
