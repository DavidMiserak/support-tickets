"""Prometheus business counters.

Module-level globals: importing this module anywhere registers the counters
with the default prometheus_client registry. prometheus-fastapi-instrumentator
also uses the default registry, so GET /metrics serves both automatically.

Test note: counters are process-global singletons. Use delta assertions
(capture value before, assert increment after) rather than absolute values.
"""

from prometheus_client import Counter

tickets_created_total = Counter(
    "tickets_created_total",
    "Number of support tickets created.",
)

ticket_status_transitions_total = Counter(
    "ticket_status_transitions_total",
    "Number of ticket status transitions.",
    ["from_status", "to_status"],
)

ticket_summarization_outcomes_total = Counter(
    "ticket_summarization_outcomes_total",
    "Outcomes of ticket summarization enqueue attempts at create time.",
    ["outcome"],
    # outcome labels:
    #   enqueued       — job was accepted by arq
    #   deduped        — job already existed (arq returned None)
    #   skipped_no_pool — arq pool was None at request time (Redis down at startup)
    #   enqueue_failed  — arq pool raised an exception during enqueue
    #
    # Note: "skipped_short" (description too short) is a worker-task event,
    # not observable at enqueue time, so it is not a label here.
)

ticket_worker_enqueue_outcomes_total = Counter(
    "ticket_worker_enqueue_outcomes_total",
    "Outcomes of analysis worker task enqueue attempts at ticket creation time.",
    ["task", "outcome"],
    # task labels: assign_priority, detect_spam, route_ticket
    # outcome labels: enqueued, enqueue_failed
)
