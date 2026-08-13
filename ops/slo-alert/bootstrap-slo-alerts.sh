#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# SLO Alerts – HyperDX API Bootstrap
#
# Creates the SLO breach alerts on top of the telemetry already collected:
#   1. Error-budget burn  — server spans ending in error (fast-burn signal)
#   2. Latency SLO breach — server spans slower than the p95 target
#   3. Error-rate SLO     — ERROR/FATAL log lines above budget
#
# Targets are configurable via env (see .env.example); defaults:
#   SLO_API_LATENCY_P95_MS=500  SLO_UI_LATENCY_P95_MS=2000  SLO_ERROR_RATE_MAX=10
#
# Usage:
#   export HYPERDX_URL="http://localhost:3000"
#   export HYPERDX_API_KEY="your-api-key"
#   export WEBHOOK_ID="your-webhook-id"
#   ./bootstrap-slo-alerts.sh
#
# Or use .env file:
#   cp .env.example .env && edit .env && ./bootstrap-slo-alerts.sh
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source "$SCRIPT_DIR/.env" && set +a

HYPERDX_URL="${HYPERDX_URL:-http://localhost:3000}"
HYPERDX_API_KEY="${HYPERDX_API_KEY:-}"
WEBHOOK_ID="${WEBHOOK_ID:-}"
SLO_API_LATENCY_P95_MS="${SLO_API_LATENCY_P95_MS:-500}"
SLO_UI_LATENCY_P95_MS="${SLO_UI_LATENCY_P95_MS:-2000}"
SLO_ERROR_RATE_MAX="${SLO_ERROR_RATE_MAX:-10}"
SLO_ERROR_SPAN_MAX="${SLO_ERROR_SPAN_MAX:-5}"

API_BASE="${HYPERDX_URL%/}/api"

die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[bootstrap] $*"; }

# Failures are collected in a file (not a counter) because the helpers run
# inside $(...) subshells; any recorded failure makes the script exit 1 with
# a FAILED summary, so a partial bootstrap can never look like success.
FAIL_LOG="$(mktemp)"
trap 'rm -f "$FAIL_LOG"' EXIT
fail() { echo "[FAILED] $*" >&2; echo "$*" >> "$FAIL_LOG"; }

[ -n "$HYPERDX_API_KEY" ] || die "HYPERDX_API_KEY is required"
[ -n "$WEBHOOK_ID" ]      || die "WEBHOOK_ID is required"

# ---------------------------------------------------------------------------
# Helper: create a saved search
# Runs inside $(...): stdout is the returned id ONLY — log goes to stderr,
# otherwise the log line would be captured into the saved-search id.
# ---------------------------------------------------------------------------
create_saved_search() {
  local name="$1"
  local query="$2"

  log "Creating saved search: $name" >&2
  RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_BASE/v1/saved-searches" \
    -H "Authorization: Bearer $HYPERDX_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg name "$name" --arg query "$query" \
      '{ name: $name, query: $query }')")

  HTTP_CODE=$(echo "$RESP" | tail -n 1)
  HTTP_BODY=$(echo "$RESP" | sed '$d')

  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    local id
    id=$(echo "$HTTP_BODY" | jq -r '._id // .id // empty' 2>/dev/null)
    if [ -n "$id" ]; then
      echo "$id"
    else
      fail "saved search '$name': HTTP $HTTP_CODE but no id in response — $HTTP_BODY"
      echo ""
    fi
  else
    fail "saved search '$name': HTTP $HTTP_CODE — $HTTP_BODY (if it already exists, delete or update it in the HyperDX UI and re-run)"
    echo ""
  fi
}

# ---------------------------------------------------------------------------
# Helper: create an alert
# ---------------------------------------------------------------------------
create_alert() {
  local name="$1"
  local saved_search_id="$2"
  local threshold="$3"
  local interval="$4"
  local message="$5"
  local group_by="${6:-}"

  log "Creating alert: $name (threshold: above $threshold, interval: $interval)"
  local payload
  payload=$(jq -n \
    --arg name "$name" \
    --arg savedSearchId "$saved_search_id" \
    --argjson threshold "$threshold" \
    --arg interval "$interval" \
    --arg webhookId "$WEBHOOK_ID" \
    --arg message "$message" \
    --arg groupBy "$group_by" \
    '{
      name: $name,
      savedSearchId: $savedSearchId,
      threshold: $threshold,
      threshold_type: "above",
      interval: $interval,
      source: "search",
      channel: { type: "slack_webhook", webhookId: $webhookId },
      message: $message
    } + (if $groupBy != "" then { groupBy: ($groupBy | split(",")) } else {} end)')

  RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_BASE/alerts" \
    -H "Authorization: Bearer $HYPERDX_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$payload")

  HTTP_CODE=$(echo "$RESP" | tail -n 1)
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    log "  Created successfully."
  else
    HTTP_BODY=$(echo "$RESP" | sed '$d')
    fail "alert '$name': HTTP $HTTP_CODE — $(echo "$HTTP_BODY" | jq -c . 2>/dev/null || echo "$HTTP_BODY")"
  fi
}

# ---------------------------------------------------------------------------
# Alert 1: Error-budget burn (server spans in error)
# ---------------------------------------------------------------------------
log ""
log "=== Alert 1: Error-Budget Burn ==="
BURN_QUERY="SpanKind = 'Server' AND StatusCode = 'Error'"

BURN_SS_ID=$(create_saved_search "SLO Error-Budget Burn" "$BURN_QUERY")
if [ -n "$BURN_SS_ID" ]; then
  MESSAGE="Availability SLO at risk: server spans are failing — error budget is burning fast."
  create_alert "SLO Error-Budget Burn" "$BURN_SS_ID" "$SLO_ERROR_SPAN_MAX" "5m" "$MESSAGE" "service.name"
else
  fail "error-budget-burn alert skipped: no saved-search id. Filter: $BURN_QUERY"
fi

# ---------------------------------------------------------------------------
# Alert 2: API latency SLO breach (p95 target)
# ---------------------------------------------------------------------------
log ""
log "=== Alert 2: API Latency SLO ==="
API_LATENCY_QUERY="SpanKind = 'Server' AND Duration > ${SLO_API_LATENCY_P95_MS}000000"

API_LAT_SS_ID=$(create_saved_search "SLO API Latency Breaches" "$API_LATENCY_QUERY")
if [ -n "$API_LAT_SS_ID" ]; then
  MESSAGE="Latency SLO at risk: API requests slower than ${SLO_API_LATENCY_P95_MS}ms are accumulating."
  create_alert "SLO API Latency" "$API_LAT_SS_ID" 20 "5m" "$MESSAGE" "service.name"
else
  fail "api-latency alert skipped: no saved-search id. Filter: $API_LATENCY_QUERY"
fi

# ---------------------------------------------------------------------------
# Alert 3: Error-rate SLO (application logs)
# ---------------------------------------------------------------------------
log ""
log "=== Alert 3: Error-Rate SLO ==="
ERROR_QUERY="SeverityText IN ('ERROR', 'FATAL') AND ResourceAttributes['telemetry.source'] != 'k8s-events'"

ERROR_SS_ID=$(create_saved_search "SLO Error Rate" "$ERROR_QUERY")
if [ -n "$ERROR_SS_ID" ]; then
  MESSAGE="Error-rate SLO at risk: ERROR/FATAL log volume above budget."
  create_alert "SLO Error Rate" "$ERROR_SS_ID" "$SLO_ERROR_RATE_MAX" "5m" "$MESSAGE" "k8s.namespace.name"
else
  fail "error-rate alert skipped: no saved-search id. Filter: $ERROR_QUERY"
fi

log ""
if [ -s "$FAIL_LOG" ]; then
  log "Bootstrap FAILED — $(wc -l < "$FAIL_LOG" | tr -d ' ') step(s) did not complete:"
  sed 's/^/[bootstrap]   - /' "$FAIL_LOG" >&2
  log "The bootstrap is PARTIAL: fix the failures above and re-run."
  exit 1
fi
log "Done. Build the 'Platform SLOs' dashboard from sli-queries.sql (see README)."
