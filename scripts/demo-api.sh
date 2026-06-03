#!/usr/bin/env bash
# Runnable API walkthrough — exercises the ticket API and async worker for graders.
#
# Usage:
#   make run          # or: API already on localhost:8000
#   make demo-api     # or: ./scripts/demo-api.sh
#
# Options (environment):
#   BASE_URL=http://localhost:8000   API base (no trailing slash)
#   RUN_SEED=auto|yes|no             Seed agents + sample tickets (default: auto)
#   ASSIGN_AGENT_ID=1                Agent id for PATCH .../assign
#   DEMO_WORKER=auto|1|0             Async worker checks: auto=if Redis ok (default)
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

# True when `compose exec api` works (podman-compose lacks `ps --status running`).
compose_api_running() {
  local compose
  compose=$(compose_cmd)
  [[ -n "$compose" ]] || return 1
  $compose exec -T api true >/dev/null 2>&1
}

redis_ready() {
  request GET "$BASE_URL/ready"
  [[ "$RESPONSE_CODE" == "200" ]] &&
    [[ "$(echo "$RESPONSE_BODY" | jq -r '.redis // empty')" == "ok" ]]
}

should_run_worker_demo() {
  case "$DEMO_WORKER" in
    1) return 0 ;;
    0) return 1 ;;
    auto) redis_ready ;;
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
    fail "expected event_type $event_type in GET /tickets/{id} events"
}

check_api_up() {
  info "Checking API at $BASE_URL"
  request GET "$BASE_URL/health"
  assert_status 200
  assert_jq '.status' ok
  pass "GET /health"
}

demo_openapi_smoke() {
  info "OpenAPI surface"
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

  local compose
  compose=$(compose_cmd)

  if compose_api_running; then
    info "Seeding sample data (idempotent) via: $compose exec api"
    $compose exec -T api python -m scripts.seed
    pass "seed via compose (agents + sample tickets)"
    return 0
  fi
  if [[ -n "${DATABASE_URL:-}" ]] && [[ -x .venv/bin/python ]]; then
    info "Seeding sample data via local .venv and DATABASE_URL"
    .venv/bin/python -m scripts.seed
    pass "seed via local python"
    return 0
  fi
  if [[ "$RUN_SEED" == "yes" ]]; then
    fail "RUN_SEED=yes but could not seed (no running api container or DATABASE_URL + .venv)"
  fi
  info "Skipping seed (no compose api container; assign uses ASSIGN_AGENT_ID=$ASSIGN_AGENT_ID)"
}

demo_happy_path() {
  info "Create ticket — all fields"
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
  pass "POST /tickets -> id=$ticket_id (priority HIGH, category TECHNICAL)"

  info "Retrieve ticket (GET /tickets/{id})"
  request GET "$BASE_URL/tickets/$ticket_id"
  assert_status 200
  assert_jq '.id' "$ticket_id"
  assert_has_event CREATED
  pass "GET /tickets/$ticket_id (includes CREATED event)"

  info "List with filters + pagination"
  request GET "$BASE_URL/tickets?status=OPEN&priority=HIGH&category=TECHNICAL&skip=0&limit=20"
  assert_status 200
  local total
  total=$(echo "$RESPONSE_BODY" | jq -r '.total')
  [[ "$total" =~ ^[0-9]+$ ]] && [[ "$total" -ge 1 ]] ||
    fail "list total should be >= 1 after create"
  echo "$RESPONSE_BODY" | jq -e '.items | type == "array"' >/dev/null ||
    fail "list response missing items array"
  pass "GET /tickets?status=OPEN&priority=HIGH&category=TECHNICAL (total=$total)"

  info "Agent updates status OPEN -> IN_PROGRESS"
  request PATCH "$BASE_URL/tickets/$ticket_id/status" '{"status": "in_progress"}'
  assert_status 200
  assert_jq '.status' IN_PROGRESS
  [[ "$(echo "$RESPONSE_BODY" | jq -r 'has("events")')" == "false" ]] ||
    fail "PATCH /status must return TicketResponse without events"
  pass "PATCH /tickets/$ticket_id/status -> IN_PROGRESS"

  info "Assign agent (PATCH /tickets/{id}/assign)"
  request PATCH "$BASE_URL/tickets/$ticket_id/assign" "{\"agent_id\": $ASSIGN_AGENT_ID}"
  if [[ "$RESPONSE_CODE" == "404" ]]; then
    local err
    err=$(echo "$RESPONSE_BODY" | jq -r '.error_type // .detail // empty')
    if [[ "$err" == "agent_not_found" ]]; then
      fail "agent $ASSIGN_AGENT_ID not found — run: make seed (or RUN_SEED=yes ./scripts/demo-api.sh)"
    fi
    if [[ "$err" == "Not Found" ]]; then
      fail "PATCH /assign not available — rebuild API: podman compose build api && podman compose up -d api"
    fi
  fi
  assert_status 200
  assert_jq '.assigned_agent_id' "$ASSIGN_AGENT_ID"
  pass "PATCH /tickets/$ticket_id/assign -> agent $ASSIGN_AGENT_ID"

  info "Idempotent assign (same agent again)"
  request PATCH "$BASE_URL/tickets/$ticket_id/assign" "{\"agent_id\": $ASSIGN_AGENT_ID}"
  assert_status 200
  pass "PATCH /tickets/$ticket_id/assign (idempotent)"

  info "Status lifecycle IN_PROGRESS -> RESOLVED -> CLOSED"
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
  pass "GET /tickets/$ticket_id includes ASSIGNED and STATUS_CHANGED events"

  info "CLOSED is terminal"
  request PATCH "$BASE_URL/tickets/$ticket_id/status" '{"status": "open"}'
  assert_status 409
  assert_error_type invalid_status_transition
  pass "409 invalid_status_transition (CLOSED -> OPEN)"

  DEMO_TICKET_ID=$ticket_id
}

demo_error_envelopes() {
  info "Error envelopes"

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

  info "Illegal status transition on fresh OPEN ticket (OPEN -> RESOLVED)"
  local open_id
  request POST "$BASE_URL/tickets" \
    '{"customer_name":"Err","customer_email":"err-'$(date +%s)'@example.com","subject":"S","description":"D","category":"OTHER"}'
  assert_status 201
  open_id=$(echo "$RESPONSE_BODY" | jq -r '.id')
  request PATCH "$BASE_URL/tickets/$open_id/status" '{"status": "RESOLVED"}'
  assert_status 409
  assert_error_type invalid_status_transition
  pass "409 invalid_status_transition (OPEN -> RESOLVED)"

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

demo_async_processing() {
  should_run_worker_demo || {
    info "Skipping async worker demo (DEMO_WORKER=$DEMO_WORKER; Redis/worker not available)"
    return 0
  }

  info "Async processing (queue + background tasks)"
  request GET "$BASE_URL/ready"
  assert_status 200
  pass "GET /ready (redis + database)"

  info "Create ticket tuned for worker heuristics"
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
  pass "POST /tickets -> id=$worker_id (worker pipeline)"

  info "Polling for worker events (up to ${WORKER_WAIT_SECS}s)"
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
  assert_has_event CREATED
  assert_has_event SUMMARIZED
  assert_has_event PRIORITY_CHANGED
  assert_has_event SPAM_FLAGGED
  assert_has_event ROUTED
  local types priority
  types=$(echo "$RESPONSE_BODY" | jq -r '[.events[].event_type] | join(", ")')
  priority=$(echo "$RESPONSE_BODY" | jq -r '.priority')
  [[ "$priority" == "CRITICAL" ]] ||
    fail "expected worker priority upgrade to CRITICAL, got $priority"
  pass "worker events on ticket $worker_id: $types (priority=$priority)"
}

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  need_cmd curl jq
  DEMO_TICKET_ID=""

  check_api_up
  demo_openapi_smoke
  maybe_seed_agents
  demo_happy_path
  demo_error_envelopes
  demo_async_processing

  info "All demo-api checks passed."
}

main "$@"
