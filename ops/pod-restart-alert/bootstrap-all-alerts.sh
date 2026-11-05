#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Bootstrap All Krateo Alerts – HyperDX API
#
# Creates all recommended alerts for the Krateo observability stack:
#   1. Pod Restart Alert (Warning events, threshold: 2, grouped by pod)
#   2. Heartbeat Canary Alert (absence — fires if canary logs stop)
#   3. Error Rate Alert (application ERROR/FATAL logs)
#   4. Agent Failure Alert (kagent MCP errors)
#
# Usage:
#   export HYPERDX_URL="http://localhost:3000"
#   export HYPERDX_API_KEY="your-api-key"
#   export WEBHOOK_ID="your-webhook-id"
#   ./bootstrap-all-alerts.sh
#
# Or use .env file:
#   cp .env.example .env && edit .env && ./bootstrap-all-alerts.sh
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/.env" ] && set -a && source "$SCRIPT_DIR/.env" && set +a

HYPERDX_URL="${HYPERDX_URL:-http://localhost:3000}"
HYPERDX_API_KEY="${HYPERDX_API_KEY:-}"
WEBHOOK_ID="${WEBHOOK_ID:-}"
ALERT_PROXY_WEBHOOK_ID="${ALERT_PROXY_WEBHOOK_ID:-$WEBHOOK_ID}"
KRATEO_BOT_MENTION="${KRATEO_BOT_MENTION:-}"

API_BASE="${HYPERDX_URL%/}/api"

die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[bootstrap] $*"; }

[ -n "$HYPERDX_API_KEY" ] || die "HYPERDX_API_KEY is required"
[ -n "$WEBHOOK_ID" ]      || die "WEBHOOK_ID is required"

# ---------------------------------------------------------------------------
# Helper: create a saved search
# ---------------------------------------------------------------------------
create_saved_search() {
  local name="$1"
  local query="$2"

  log "Creating saved search: $name"
  RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_BASE/v1/saved-searches" \
    -H "Authorization: Bearer $HYPERDX_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg name "$name" --arg query "$query" \
      '{ name: $name, query: $query }')")

  HTTP_CODE=$(echo "$RESP" | tail -n 1)
  HTTP_BODY=$(echo "$RESP" | sed '$d')

  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    echo "$HTTP_BODY" | jq -r '._id // .id // empty' 2>/dev/null
  else
    log "  Warning: saved search creation returned HTTP $HTTP_CODE (may already exist)"
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
  local threshold_type="$4"
  local interval="$5"
  local webhook_id="$6"
  local message="$7"
  local group_by="${8:-}"

  log "Creating alert: $name (threshold: $threshold_type $threshold, interval: $interval)"

  local payload
  if [ -n "$group_by" ]; then
    payload=$(jq -n \
      --arg name "$name" \
      --arg savedSearchId "$saved_search_id" \
      --argjson threshold "$threshold" \
      --arg threshold_type "$threshold_type" \
      --arg interval "$interval" \
      --arg webhookId "$webhook_id" \
      --arg message "$message" \
      --arg groupBy "$group_by" \
      '{
        name: $name,
        savedSearchId: $savedSearchId,
        threshold: $threshold,
        threshold_type: $threshold_type,
        interval: $interval,
        source: "search",
        channel: { type: "slack_webhook", webhookId: $webhookId },
        message: $message,
        groupBy: ($groupBy | split(","))
      }')
  else
    payload=$(jq -n \
      --arg name "$name" \
      --arg savedSearchId "$saved_search_id" \
      --argjson threshold "$threshold" \
      --arg threshold_type "$threshold_type" \
      --arg interval "$interval" \
      --arg webhookId "$webhook_id" \
      --arg message "$message" \
      '{
        name: $name,
        savedSearchId: $savedSearchId,
        threshold: $threshold,
        threshold_type: $threshold_type,
        interval: $interval,
        source: "search",
        channel: { type: "slack_webhook", webhookId: $webhookId },
        message: $message
      }')
  fi

  RESP=$(curl -s -w "\n%{http_code}" -X POST "$API_BASE/alerts" \
    -H "Authorization: Bearer $HYPERDX_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$payload")

  HTTP_CODE=$(echo "$RESP" | tail -n 1)
  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    log "  Created successfully."
  else
    HTTP_BODY=$(echo "$RESP" | sed '$d')
    log "  Warning: returned HTTP $HTTP_CODE"
    echo "$HTTP_BODY" | jq . 2>/dev/null || echo "$HTTP_BODY"
  fi
}

# ---------------------------------------------------------------------------
# Alert 1: Pod Restart Alert
# ---------------------------------------------------------------------------
log ""
log "=== Alert 1: Pod Restart Alert ==="
POD_RESTART_QUERY="ResourceAttributes['telemetry.source'] = 'k8s-events' AND JSONExtractString(Body, 'object', 'involvedObject', 'kind') = 'Pod' AND JSONExtractString(Body, 'object', 'type') = 'Warning'"

POD_RESTART_SS_ID=$(create_saved_search "Pod Restart Events" "$POD_RESTART_QUERY")
if [ -n "$POD_RESTART_SS_ID" ]; then
  MESSAGE="Pod restart detected. ${KRATEO_BOT_MENTION:+$KRATEO_BOT_MENTION please investigate and fix.}"
  create_alert "Pod Restart Alert" "$POD_RESTART_SS_ID" 2 "above" "5m" "$ALERT_PROXY_WEBHOOK_ID" "$MESSAGE" "k8s.namespace.name,k8s.pod.name"
else
  log "  Skipping alert creation (no saved search ID). Create the saved search manually in HyperDX UI."
fi

# ---------------------------------------------------------------------------
# Alert 2: Heartbeat Canary (absence alert)
# ---------------------------------------------------------------------------
log ""
log "=== Alert 2: Heartbeat Canary ==="
CANARY_QUERY="ResourceAttributes['k8s.pod.labels.app'] = 'krateo-heartbeat-canary'"

CANARY_SS_ID=$(create_saved_search "Heartbeat Canary" "$CANARY_QUERY")
if [ -n "$CANARY_SS_ID" ]; then
  MESSAGE="CRITICAL: Observability pipeline may be down — heartbeat canary logs missing for >3 minutes."
  create_alert "Pipeline Heartbeat" "$CANARY_SS_ID" 0 "below" "3m" "$WEBHOOK_ID" "$MESSAGE"
else
  log "  Skipping alert creation. Create the saved search manually in HyperDX UI."
  log "  Filter: ResourceAttributes['k8s.pod.labels.app'] = 'krateo-heartbeat-canary'"
fi

# ---------------------------------------------------------------------------
# Alert 3: Application Error Rate
# ---------------------------------------------------------------------------
log ""
log "=== Alert 3: Application Error Rate ==="
# The ServiceName exclusion is rule 3 in docs/alerts.md: the observability stack must not alert on
# itself. krateo-budget-evaluator is added because a breached budget is an ERROR line by design, and
# a spend threshold is not an application failure.
ERROR_QUERY="SeverityNumber >= 17 AND ResourceAttributes['telemetry.source'] != 'k8s-events' AND ServiceName NOT IN ('krateo-observability', 'krateo-alert-troubleshooter', 'krateo-alert-provider', 'incident-controller', 'incident-provider', 'krateo-clickstack', 'krateo-clickstack-clickhouse-clickhouse', 'krateo-clickstack-keeper-keeper', 'krateo-clickstack-mongodb', 'clickhouse-mcp-server', 'clickhouse-operator', 'incident-agent', 'autopilot', 'kagent', 'repo-mcp-server', 'krateo-budget-evaluator')"

ERROR_SS_ID=$(create_saved_search "Application Errors" "$ERROR_QUERY")
if [ -n "$ERROR_SS_ID" ]; then
  MESSAGE="High error rate detected. ${KRATEO_BOT_MENTION:+$KRATEO_BOT_MENTION please investigate.}"
  create_alert "Error Rate Alert" "$ERROR_SS_ID" 10 "above" "5m" "$ALERT_PROXY_WEBHOOK_ID" "$MESSAGE" "k8s.namespace.name"
else
  log "  Skipping alert creation. Create the saved search manually."
fi

# ---------------------------------------------------------------------------
# Alert 4: Agent MCP Errors
# ---------------------------------------------------------------------------
log ""
log "=== Alert 4: Agent MCP Errors ==="
AGENT_QUERY="ResourceAttributes['k8s.container.name'] LIKE '%kagent%' AND SeverityNumber >= 17 AND Body LIKE '%mcp%'"

AGENT_SS_ID=$(create_saved_search "Agent MCP Errors" "$AGENT_QUERY")
if [ -n "$AGENT_SS_ID" ]; then
  MESSAGE="Agent MCP tool errors detected — agent diagnosis may be impaired."
  create_alert "Agent MCP Error" "$AGENT_SS_ID" 3 "above" "5m" "$WEBHOOK_ID" "$MESSAGE"
else
  log "  Skipping alert creation. Create the saved search manually."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log ""
log "Alert bootstrap complete."
log ""
log "Next steps:"
log "  1. Verify alerts in HyperDX UI: ${HYPERDX_URL}/alerts"
log "  2. Point HyperDX webhooks to autopilot-alert-proxy:"
log "     http://autopilot-alert-proxy.krateo-system.svc:8090/webhook"
log "  3. Test by deploying demo/scenario1-crashloop.yaml"
