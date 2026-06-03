#!/usr/bin/env bash
# Runnable API walkthrough for graders and local smoke checks.
#
# Usage:
#   make run          # api + postgres + redis + worker
#   make demo-api     # or: ./scripts/demo-api.sh
#
# Options (environment):
#   BASE_URL=http://localhost:8000   API base (no trailing slash)
#   RUN_SEED=auto|yes|no             Seed agents only (default: auto) — not sample tickets
#   ASSIGN_AGENT_ID=1                Agent id for PATCH .../assign
#   DEMO_WORKER=auto|1|0             auto=required if Redis ok; 0=skip async phase
#   WORKER_WAIT_SECS=20              Max seconds to poll for worker events
#   VERBOSE=1                        Print response bodies on success
#
# Requires: curl, jq. API must be up (script checks GET /health).

set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
RUN_SEED="${RUN_SEED:-auto}"
ASSIGN_AGENT_ID="${ASSIGN_AGENT_ID:-1}"
DEMO_WORKER="${DEMO_WORKER:-auto}"
WORKER_WAIT_SECS="${WORKER_WAIT_SECS:-20}"
VERBOSE="${VERBOSE:-0}"

RESPONSE_BODY=""
RESPONSE_CODE=""
TMP_FILE=""
trap 'rm -f "${TMP_FILE:-}"' EXIT

pass() { printf '  OK  %s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }
phase_sep() { printf '\n'; }
fail() {
  printf '  FAIL %s\n' "$*" >&2
  if [[ -n "$RESPONSE_BODY" ]]; then
    printf '       last response (HTTP %s): %s\n' "$RESPONSE_CODE" "$RESPONSE_BODY" >&2
  fi
  exit 1
}

need_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || fail "missing required command: $cmd"
  done
}

compose_cmd() {
  if command -v podman >/dev/null 2>&1; then
    echo "podman compose"
  elif command -v docker >/dev/null 2>&1; then
    echo "docker compose"
  else
    echo ""
  fi
}

compose_api_running() {
  local compose
  compose=$(compose_cmd)
  [[ -n "$compose" ]] || return 1
  $compose exec -T api true >/dev/null 2>&1
}

compose_worker_running() {
  local compose
  compose=$(compose_cmd)
  [[ -n "$compose" ]] || return 1
  $compose exec -T worker true >/dev/null 2>&1
}

redis_ready() {
  request GET "$BASE_URL/ready"
  [[ "$RESPONSE_CODE" == "200" ]] &&
    [[ "$(echo "$RESPONSE_BODY" | jq -r '.redis // empty')" == "ok" ]]
}

require_worker_demo() {
  case "$DEMO_WORKER" in
    1)
      if ! redis_ready; then
        fail "DEMO_WORKER=1 but GET /ready reports redis not ok — run: make run"
      fi
      ;;
    0) return 1 ;;
    auto)
      if redis_ready; then
        return 0
      fi
      fail "Phase C (async processing) requires Redis + worker — run: make run" \
        "or set DEMO_WORKER=0 to skip async checks (API-only)"
      ;;
    *)
      fail "DEMO_WORKER must be auto, 1, or 0 (got: $DEMO_WORKER)"
      ;;
  esac
}

# HTTP request; sets RESPONSE_BODY and RESPONSE_CODE.
request() {
  local method=$1 url=$2
  local body=${3:-}
  TMP_FILE=$(mktemp)
  if [[ -n "$body" ]]; then
    RESPONSE_CODE=$(
      curl -sS -o "$TMP_FILE" -w '%{http_code}' \
        -X "$method" \
        -H 'Content-Type: application/json' \
        -d "$body" \
        "$url"
    )
  else
    RESPONSE_CODE=$(
      curl -sS -o "$TMP_FILE" -w '%{http_code}' -X "$method" "$url"
    )
  fi
  RESPONSE_BODY=$(cat "$TMP_FILE")
  rm -f "$TMP_FILE"
  TMP_FILE=""
  if [[ "$VERBOSE" == "1" ]]; then
    printf '       HTTP %s %s %s\n' "$RESPONSE_CODE" "$method" "$url"
    printf '%s\n' "$RESPONSE_BODY" | jq . 2>/dev/null || printf '%s\n' "$RESPONSE_BODY"
  fi
}

assert_status() {
  local expected=$1
  [[ "$RESPONSE_CODE" == "$expected" ]] ||
    fail "expected HTTP $expected, got $RESPONSE_CODE"
}

assert_error_type() {
  local expected=$1
  local got
  got=$(echo "$RESPONSE_BODY" | jq -r '.error_type // empty')
  [[ "$got" == "$expected" ]] ||
    fail "expected error_type=$expected, got '$got'"
}

assert_jq() {
  local filter=$1 expected=$2
  local got
  got=$(echo "$RESPONSE_BODY" | jq -r "$filter")
  [[ "$got" == "$expected" ]] ||
    fail "jq -r '$filter' expected '$expected', got '$got'"
}

assert_has_event() {
  local event_type=$1
  echo "$RESPONSE_BODY" | jq -e --arg t "$event_type" \
    '.events | map(.event_type) | index($t)' >/dev/null ||
    fail "expected event_type $event_type in response events"
}

assert_lacks_events() {
  local event_type
  for event_type in "$@"; do
    if echo "$RESPONSE_BODY" | jq -e --arg t "$event_type" \
      '.events | map(.event_type) | index($t)' >/dev/null; then
      fail "expected no $event_type on ticket yet (worker ran too early?)"
    fi
  done
}

worker_missing_events() {
  local ticket_id=$1
  local required event missing=()
  required=(SUMMARIZED PRIORITY_CHANGED SPAM_FLAGGED ROUTED)
  request GET "$BASE_URL/tickets/$ticket_id"
  [[ "$RESPONSE_CODE" == "200" ]] || return 0
  for event in "${required[@]}"; do
    if ! echo "$RESPONSE_BODY" | jq -e --arg t "$event" \
      '.events | map(.event_type) | index($t)' >/dev/null; then
      missing+=("$event")
    fi
  done
  if [[ "${#missing[@]}" -gt 0 ]]; then
    local types
    types=$(echo "$RESPONSE_BODY" | jq -r '[.events[].event_type] | join(", ")')
    fail "worker timeout after ${WORKER_WAIT_SECS}s on ticket $ticket_id — missing: ${missing[*]}; have: ${types:-none}"
  fi
}

check_api_up() {
  info "Preflight: API health"
  request GET "$BASE_URL/health"
  assert_status 200
  assert_jq '.status' ok
  pass "GET /health"
}

demo_openapi_smoke() {
  phase_sep
  info "Phase B — API design: OpenAPI / Swagger"
  request GET "$BASE_URL/openapi.json"
  assert_status 200
  echo "$RESPONSE_BODY" | jq -e '.paths["/tickets"].post' >/dev/null ||
    fail "OpenAPI missing POST /tickets"
  echo "$RESPONSE_BODY" | jq -e '.paths["/tickets/{ticket_id}"].get' >/dev/null ||
    fail "OpenAPI missing GET /tickets/{ticket_id}"
  echo "$RESPONSE_BODY" | jq -e '.paths["/tickets/{ticket_id}/status"].patch' >/dev/null ||
    fail "OpenAPI missing PATCH /tickets/{ticket_id}/status"
  echo "$RESPONSE_BODY" | jq -e '.paths["/tickets/{ticket_id}/assign"].patch' >/dev/null ||
    fail "OpenAPI missing PATCH /tickets/{ticket_id}/assign"
  pass "OpenAPI lists ticket CRUD + status + assign routes"
}

maybe_seed_agents() {
  case "$RUN_SEED" in
    no) return 0 ;;
    yes) ;;
    auto) ;;
    *) fail "RUN_SEED must be auto, yes, or no (got: $RUN_SEED)" ;;
  esac

  local compose seed_args
  seed_args=(python -m scripts.seed --agents-only)
  compose=$(compose_cmd)

  if compose_api_running; then
    info "Preflight: seed agents only (tickets created by demo POSTs)"
    $compose exec -T api "${seed_args[@]}"
    pass "seed agents only via compose"
    return 0
  fi
  if [[ -n "${DATABASE_URL:-}" ]] && [[ -x .venv/bin/python ]]; then
    info "Preflight: seed agents only via local .venv"
    .venv/bin/python -m scripts.seed --agents-only
    pass "seed agents only via local python"
    return 0
  fi
  if [[ "$RUN_SEED" == "yes" ]]; then
    fail "RUN_SEED=yes but could not seed (no running api container or DATABASE_URL + .venv)"
  fi
  info "Skipping seed (no compose api container; assign uses ASSIGN_AGENT_ID=$ASSIGN_AGENT_ID)"
}

demo_happy_path() {
  phase_sep
  info "Phase A — Ticket management API"

  info "[demo: create] POST /tickets (all fields)"
  local create_body
  create_body='{
    "customer_name": "Ada Lovelace",
    "customer_email": "demo-'$(date +%s)'@example.com",
    "subject": "Cannot reset my password",
    "description": "The reset link 404s after I click it.",
    "priority": "HIGH",
    "category": "technical"
  }'
  request POST "$BASE_URL/tickets" "$create_body"
  assert_status 201
  assert_jq '.status' OPEN
  assert_jq '.category' TECHNICAL
  assert_jq '.priority' HIGH
  local ticket_id
  ticket_id=$(echo "$RESPONSE_BODY" | jq -r '.id')
  [[ "$ticket_id" =~ ^[0-9]+$ ]] || fail "create response missing numeric id"
  pass "POST /tickets -> id=$ticket_id"

  info "[demo: retrieve] GET /tickets/{id}"
  request GET "$BASE_URL/tickets/$ticket_id"
  assert_status 200
  assert_jq '.id' "$ticket_id"
  assert_has_event CREATED
  assert_lacks_events SUMMARIZED PRIORITY_CHANGED SPAM_FLAGGED ROUTED
  pass "GET /tickets/$ticket_id (CREATED only — worker events come in Phase C)"

  info "[demo: list] GET /tickets with filters + pagination"
  request GET "$BASE_URL/tickets?status=OPEN&priority=HIGH&category=TECHNICAL&skip=0&limit=20"
  assert_status 200
  local total
  total=$(echo "$RESPONSE_BODY" | jq -r '.total')
  [[ "$total" =~ ^[0-9]+$ ]] && [[ "$total" -ge 1 ]] ||
    fail "list total should be >= 1 after create"
  echo "$RESPONSE_BODY" | jq -e '.items | type == "array"' >/dev/null ||
    fail "list response missing items array"
  pass "GET /tickets?status&priority&category&skip&limit (total=$total)"

  info "[demo: update status] Agent PATCH OPEN -> IN_PROGRESS"
  request PATCH "$BASE_URL/tickets/$ticket_id/status" '{"status": "in_progress"}'
  assert_status 200
  assert_jq '.status' IN_PROGRESS
  assert_has_event STATUS_CHANGED
  pass "PATCH /tickets/$ticket_id/status -> IN_PROGRESS"

  info "[extra: assign] PATCH /tickets/{id}/assign (optional; needs seeded agent)"
  request PATCH "$BASE_URL/tickets/$ticket_id/assign" "{\"agent_id\": $ASSIGN_AGENT_ID}"
  if [[ "$RESPONSE_CODE" == "404" ]]; then
    local err
    err=$(echo "$RESPONSE_BODY" | jq -r '.error_type // .detail // empty')
    if [[ "$err" == "agent_not_found" ]]; then
      fail "agent $ASSIGN_AGENT_ID not found — run: make demo-api (RUN_SEED=auto) or make seed"
    fi
    if [[ "$err" == "Not Found" ]]; then
      fail "PATCH /assign not available — rebuild API: podman compose build api && podman compose up -d api"
    fi
  fi
  assert_status 200
  assert_jq '.assigned_agent_id' "$ASSIGN_AGENT_ID"
  assert_has_event ASSIGNED
  pass "PATCH /tickets/$ticket_id/assign -> agent $ASSIGN_AGENT_ID"

  info "[demo: update status] IN_PROGRESS -> RESOLVED -> CLOSED"
  request PATCH "$BASE_URL/tickets/$ticket_id/status" '{"status": "resolved"}'
  assert_status 200
  assert_jq '.status' RESOLVED
  pass "PATCH /tickets/$ticket_id/status -> RESOLVED"

  request PATCH "$BASE_URL/tickets/$ticket_id/status" '{"status": "closed"}'
  assert_status 200
  assert_jq '.status' CLOSED
  pass "PATCH /tickets/$ticket_id/status -> CLOSED"

  request GET "$BASE_URL/tickets/$ticket_id"
  assert_status 200
  assert_has_event ASSIGNED
  assert_has_event STATUS_CHANGED
  pass "GET /tickets/$ticket_id includes agent/status audit events"

  info "[demo: update status] CLOSED is terminal (409)"
  request PATCH "$BASE_URL/tickets/$ticket_id/status" '{"status": "open"}'
  assert_status 409
  assert_error_type invalid_status_transition
  pass "409 invalid_status_transition (CLOSED -> OPEN)"

  DEMO_TICKET_ID=$ticket_id
}

demo_error_envelopes() {
  phase_sep
  info "Phase B — API design: error envelopes"

  request GET "$BASE_URL/tickets/999999"
  assert_status 404
  assert_error_type ticket_not_found
  pass "404 ticket_not_found"

  local ticket_id=${DEMO_TICKET_ID:-}
  if [[ -z "$ticket_id" ]]; then
    request POST "$BASE_URL/tickets" \
      '{"customer_name":"X","customer_email":"x@example.com","subject":"S","description":"D","category":"OTHER"}'
    assert_status 201
    ticket_id=$(echo "$RESPONSE_BODY" | jq -r '.id')
  fi

  info "409 invalid_status_transition (CLOSED -> OPEN)"
  local closed_id
  request POST "$BASE_URL/tickets" \
    '{"customer_name":"Err","customer_email":"err-'$(date +%s)'@example.com","subject":"S","description":"D","category":"OTHER"}'
  assert_status 201
  closed_id=$(echo "$RESPONSE_BODY" | jq -r '.id')
  request PATCH "$BASE_URL/tickets/$closed_id/status" '{"status": "CLOSED"}'
  assert_status 200
  request PATCH "$BASE_URL/tickets/$closed_id/status" '{"status": "OPEN"}'
  assert_status 409
  assert_error_type invalid_status_transition
  pass "409 invalid_status_transition"

  request POST "$BASE_URL/tickets" \
    '{"customer_name":"X","customer_email":"not-an-email","subject":"S","description":"D","category":"OTHER"}'
  assert_status 422
  assert_error_type validation_error
  echo "$RESPONSE_BODY" | jq -e '.errors | type == "array"' >/dev/null ||
    fail "422 should include errors array"
  pass "422 validation_error"

  request PATCH "$BASE_URL/tickets/$ticket_id/assign" '{"agent_id": 999999}'
  assert_status 404
  assert_error_type agent_not_found
  pass "404 agent_not_found"

  request PATCH "$BASE_URL/tickets/$ticket_id/assign" '{"agent_id": 0}'
  assert_status 422
  assert_error_type validation_error
  pass "422 validation_error (agent_id ge=1)"
}

worker_events_ready() {
  local ticket_id=$1
  local required event found=0
  required=(CREATED SUMMARIZED PRIORITY_CHANGED SPAM_FLAGGED ROUTED)
  request GET "$BASE_URL/tickets/$ticket_id"
  [[ "$RESPONSE_CODE" == "200" ]] || return 1
  for event in "${required[@]}"; do
    if echo "$RESPONSE_BODY" | jq -e --arg t "$event" \
      '.events | map(.event_type) | index($t)' >/dev/null; then
      found=$((found + 1))
    fi
  done
  [[ "$found" -eq "${#required[@]}" ]]
}

assert_worker_results_stored() {
  local worker_id=$1

  assert_has_event CREATED
  assert_has_event SUMMARIZED
  assert_has_event PRIORITY_CHANGED
  assert_has_event SPAM_FLAGGED
  assert_has_event ROUTED

  echo "$RESPONSE_BODY" | jq -e \
    '.events[] | select(.event_type == "SUMMARIZED") | select(.field_changed == "summary") | select(.new_value != null and .new_value != "")' \
    >/dev/null ||
    fail "SUMMARIZED event missing summary text in new_value"

  echo "$RESPONSE_BODY" | jq -e \
    '.events[] | select(.event_type == "SPAM_FLAGGED") | select(.new_value == "true")' \
    >/dev/null ||
    fail "SPAM_FLAGGED event missing new_value true"

  echo "$RESPONSE_BODY" | jq -e \
    '.events[] | select(.event_type == "ROUTED") | select(.new_value == "technical-support")' \
    >/dev/null ||
    fail "ROUTED event expected department technical-support for TECHNICAL category"

  local priority types
  priority=$(echo "$RESPONSE_BODY" | jq -r '.priority')
  [[ "$priority" == "CRITICAL" ]] ||
    fail "expected worker priority upgrade to CRITICAL, got $priority"
  types=$(echo "$RESPONSE_BODY" | jq -r '[.events[].event_type] | join(", ")')
  pass "worker results stored on ticket $worker_id: $types (priority=$priority)"
}

demo_async_processing() {
  phase_sep
  if ! require_worker_demo; then
    info "Phase C — Async processing: skipped (DEMO_WORKER=0)"
    return 0
  fi

  info "Phase C — Async processing (queue + background tasks)"

  request GET "$BASE_URL/ready"
  assert_status 200
  pass "GET /ready (database + redis)"

  if compose_api_running && ! compose_worker_running; then
    info "WARN: redis is up but worker container is not reachable via compose exec"
  fi

  info "[demo: async] POST /tickets tuned for worker heuristics"
  local body worker_id
  body='{
    "customer_name": "Worker Demo",
    "customer_email": "worker-'$(date +%s)'@example.com",
    "subject": "URGENT: production outage",
    "description": "You have won a free offer! Login broken. http://a.test http://b.test http://c.test",
    "category": "TECHNICAL",
    "priority": "MEDIUM"
  }'
  request POST "$BASE_URL/tickets" "$body"
  assert_status 201
  worker_id=$(echo "$RESPONSE_BODY" | jq -r '.id')
  pass "POST /tickets -> id=$worker_id (enqueued worker jobs)"

  info "Polling GET /tickets/$worker_id for worker events (up to ${WORKER_WAIT_SECS}s)"
  local elapsed=0
  while [[ "$elapsed" -lt "$WORKER_WAIT_SECS" ]]; do
    if worker_events_ready "$worker_id"; then
      break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done

  request GET "$BASE_URL/tickets/$worker_id"
  assert_status 200
  if ! worker_events_ready "$worker_id"; then
    worker_missing_events "$worker_id"
  fi
  assert_worker_results_stored "$worker_id"
}

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  need_cmd curl jq
  DEMO_TICKET_ID=""

  check_api_up
  maybe_seed_agents
  demo_happy_path
  demo_openapi_smoke
  demo_error_envelopes
  demo_async_processing

  phase_sep
  info "All demo-api checks passed (phases A–C)."
}

main "$@"
