"""Prometheus metrics for the arq worker process.

Separate from ``app.metrics`` (API business counters): the worker runs in its own
process and exposes these on its own port via ``prometheus_client.start_http_server``
(see ``app.worker.main``). Importing this module registers the metrics on the
default registry; the worker's metrics server then serves them.

Counters/histograms are process-global singletons — tests use delta assertions
(capture before, assert increment after).
"""

import time

from prometheus_client import Counter, Histogram, Info

# Task-execution outcomes (complements the API-side ticket_summarization_outcomes_total,
# which only tracks enqueue outcomes at create time).
#   task    — summarize_ticket | assign_priority | detect_spam | route_ticket
#   outcome — completed | skipped | not_found | failed
worker_jobs_total = Counter(
    "worker_jobs_total",
    "Background worker task executions by task and terminal outcome.",
    ["task", "outcome"],
)

worker_job_duration_seconds = Histogram(
    "worker_job_duration_seconds",
    "Wall-clock duration of a worker task execution in seconds.",
    ["task"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

# Which summarizer backend this worker process initialized (noop/transformer/
# anthropic, after any registry fallback). Set once at startup.
worker_summarizer_backend = Info(
    "worker_summarizer_backend",
    "Summarizer backend active in this worker process.",
)


def record_job(task: str, outcome: str, start: float | None = None) -> None:
    """Record one task execution: count the outcome and (if timed) its duration.

    ``start`` is a ``time.monotonic()`` timestamp; pass it for terminal outcomes
    that did real work so the duration histogram reflects them. Outcomes without
    a meaningful duration (e.g. a missing ticket) omit it.
    """
    worker_jobs_total.labels(task=task, outcome=outcome).inc()
    if start is not None:
        worker_job_duration_seconds.labels(task=task).observe(time.monotonic() - start)
