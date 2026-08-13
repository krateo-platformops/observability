#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Budget Alerts – HyperDX API Bootstrap
#
# Creates the budget warning + breach alerts over the log lines emitted by
# the budget-evaluator CronJob. Reuses the standard alert pipeline: the
# evaluator writes to stdout, OTel ships to ClickHouse `otel_logs`, HyperDX
# fires the alert.
#
# Delivery:
#   - in-portal: BUDGET_PORTAL_WEBHOOK_ID → autopilot-alert-proxy → portal
#     notification endpoint (same route as every other in-portal alert)
#   - email:     BUDGET_EMAIL_WEBHOOK_ID → HyperDX email integration / relay
#
# Usage:
#   export HYPERDX_URL="http://localhost:3000"
#   export HYPERDX_API_KEY="your-api-key"
#   export BUDGET_PORTAL_WEBHOOK_ID="webhook-id"
#   export BUDGET_EMAIL_WEBHOOK_ID="webhook-id"     # optional
#   ./bootstrap-budget-alerts.sh
#
# Or use .env file:
#   cp .env.example .env && edit .env && ./bootstrap-budget-alerts.sh
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source "$SCRIPT_DIR/.env" && set +a

HYPERDX_URL="${HYPERDX_URL:-http://localhost:3000}"
HYPERDX_API_KEY="${HYPERDX_API_KEY:-}"
BUDGET_PORTAL_WEBHOOK_ID="${BUDGET_PORTAL_WEBHOOK_ID:-}"
BUDGET_EMAIL_WEBHOOK_ID="${BUDGET_EMAIL_WEBHOOK_ID:-}"

API_BASE="${HYPERDX_URL%/}/api"

die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[bootstrap] $*"; }

# Failures are collected in a file (not a counter) because the helpers run
# inside $(...) subshells; any recorded failure makes the script exit 1 with
# a FAILED summary, so a partial bootstrap can never look like success.
FAIL_LOG="$(mktemp)"
trap 'rm -f "$FAIL_LOG"' EXIT
fail() { echo "[FAILED] $*" >&2; echo "$*" >> "$FAIL_LOG"; }

[ -n "$HYPERDX_API_KEY" ]          || die "HYPERDX_API_KEY is required"
[ -n "$BUDGET_PORTAL_WEBHOOK_ID" ] || die "BUDGET_PORTAL_WEBHOOK_ID is required (create webhook in HyperDX UI first)"

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
# Helper: create an alert on a saved search, one channel per call
# ---------------------------------------------------------------------------
create_alert() {
  local name="$1"
  local saved_search_id="$2"
  local threshold="$3"
  local interval="$4"
  local webhook_id="$5"
  local message="$6"

  log "Creating alert: $name (interval: $interval)"
  local payload
  payload=$(jq -n \
    --arg name "$name" \
    --arg savedSearchId "$saved_search_id" \
    --argjson threshold "$threshold" \
    --arg interval "$interval" \
    --arg webhookId "$webhook_id" \
    --arg message "$message" \
    --arg groupBy "JSONExtractString(Body, 'attributes', 'krateo.budget.id')" \
    '{
      name: $name,
      savedSearchId: $savedSearchId,
      threshold: $threshold,
      threshold_type: "above",
      interval: $interval,
      source: "search",
      channel: { type: "slack_webhook", webhookId: $webhookId },
      message: $message,
      groupBy: [$groupBy]
    }')

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
# Alert 1: Budget breached (spend >= amount)
# ---------------------------------------------------------------------------
log ""
log "=== Alert 1: Budget Breached ==="
BREACH_QUERY="ResourceAttributes['k8s.pod.labels.app'] = 'krateo-budget-evaluator' AND JSONExtractString(Body, 'attributes', 'krateo.budget.status') = 'breached'"

BREACH_SS_ID=$(create_saved_search "Budget Breached" "$BREACH_QUERY")
if [ -n "$BREACH_SS_ID" ]; then
  MESSAGE="Budget breached: spend reached the configured budget amount for this scope."
  create_alert "Budget Breached (portal)" "$BREACH_SS_ID" 0 "15m" "$BUDGET_PORTAL_WEBHOOK_ID" "$MESSAGE"
  if [ -n "$BUDGET_EMAIL_WEBHOOK_ID" ]; then
    create_alert "Budget Breached (email)" "$BREACH_SS_ID" 0 "15m" "$BUDGET_EMAIL_WEBHOOK_ID" "$MESSAGE"
  fi
else
  fail "breach alerts skipped: no saved-search id. Filter: $BREACH_QUERY"
fi

# ---------------------------------------------------------------------------
# Alert 2: Budget warning (spend >= warn_ratio * amount)
# ---------------------------------------------------------------------------
log ""
log "=== Alert 2: Budget Warning ==="
WARN_QUERY="ResourceAttributes['k8s.pod.labels.app'] = 'krateo-budget-evaluator' AND JSONExtractString(Body, 'attributes', 'krateo.budget.status') = 'warning'"

WARN_SS_ID=$(create_saved_search "Budget Warning" "$WARN_QUERY")
if [ -n "$WARN_SS_ID" ]; then
  MESSAGE="Budget warning: spend crossed the early-warning threshold for this scope."
  create_alert "Budget Warning (portal)" "$WARN_SS_ID" 0 "15m" "$BUDGET_PORTAL_WEBHOOK_ID" "$MESSAGE"
  if [ -n "$BUDGET_EMAIL_WEBHOOK_ID" ]; then
    create_alert "Budget Warning (email)" "$WARN_SS_ID" 0 "15m" "$BUDGET_EMAIL_WEBHOOK_ID" "$MESSAGE"
  fi
else
  fail "warning alerts skipped: no saved-search id. Filter: $WARN_QUERY"
fi

log ""
if [ -s "$FAIL_LOG" ]; then
  log "Bootstrap FAILED — $(wc -l < "$FAIL_LOG" | tr -d ' ') step(s) did not complete:"
  sed 's/^/[bootstrap]   - /' "$FAIL_LOG" >&2
  log "The bootstrap is PARTIAL: fix the failures above and re-run."
  exit 1
fi
log "Done."
