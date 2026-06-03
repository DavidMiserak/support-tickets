"""Per-IP rate limiter (slowapi + in-memory storage).

The limiter is a module-level singleton shared across the app lifetime.
app/main.py attaches it to app.state and registers the 429 handler;
app/api/tickets.py decorates individual routes with @limiter.limit(...).

In-memory storage means limits are per-process. For multi-process or
multi-replica deployments, swap MemoryStorage for a Redis storage backend
(limits.storage.RedisStorage) pointing at the same Redis the worker uses.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
