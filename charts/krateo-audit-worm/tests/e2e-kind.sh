#!/usr/bin/env bash
# krateo-audit-worm end-to-end on a disposable kind cluster: the chart's real seal.sh and
# verify.sh against a real ClickHouse. Jobs are triggered by hand from the CronJobs.
#
#   usage: KUBECONFIG=<disposable kind cluster> tests/e2e-kind.sh [chart-dir]
#          (chart-dir defaults to this chart; pass an older checkout to compare versions)
#
# Proves:
#   A. fresh install: verify before the first seal is ok (nothing to protect yet)
#   B. rows are mirrored into the WORM table, sealed (row_count), and verify reports ok
#      over a non-empty chain
#   C. an edited WORM row is detected: verify reports tampered and fails the Job
#   D. seals deleted while the sealer is stopped: verify reports ok while the rows are
#      younger than verify.staleAfterSeconds (no false positive), then unsealed and fails
#      once they are older (0.1.2 reported ok here forever)
set -uo pipefail
CHART="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
: "${KUBECONFIG:?point KUBECONFIG at a disposable kind cluster}"
NS=audit-e2e; REL=krateo-audit-worm
CH_IMAGE="clickhouse/clickhouse-server:24.8-alpine"
STALE=60
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
export HELM_CONFIG_HOME="$W/helm/config" HELM_CACHE_HOME="$W/helm/cache" HELM_DATA_HOME="$W/helm/data"
k() { kubectl -n $NS "$@"; }
FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
expect() { if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (got '$1', want '$2')"; fi; }
step() { echo; echo "== $* =="; }
CH() { k exec deploy/clickhouse -- clickhouse-client --user e2e --password "$CHPW" -q "$1"; }
run() { # run <seal|verify> <job> : trigger, wait, print "<Complete|Failed> <status-from-log>"
  k delete job "$2" --ignore-not-found >/dev/null 2>&1
  k create job "$2" --from="cronjob/$REL-$1" >/dev/null
  local c=""
  for _ in $(seq 1 90); do
    c=$(k get job "$2" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{"\n"}{end}' 2>/dev/null | grep -E '^(Complete|Failed)$' | head -1)
    [ -n "$c" ] && break; sleep 2
  done
  local s; s=$(k logs "job/$2" 2>/dev/null | grep -o '"krateo.audit.status":"[a-z_]*"' | tail -1 | cut -d'"' -f4)
  echo "${c:-Timeout} ${s:-none}"
}
attr() { k logs "job/$1" 2>/dev/null | grep -o "\"krateo.audit.$2\":\"[^\"]*\"" | tail -1 | cut -d'"' -f4; }
insert() { # insert <n> rows into the collector's source table
  CH "INSERT INTO krateo_audit SELECT now64(9), lower(hex(rand64())), lower(hex(rand64())), 'INFO', 'e2e', concat('audit row ', toString(number)), map('service.name','e2e'), map('event.name','audit') FROM numbers($1)"; }

step "0. ClickHouse and the source table (as the collector creates it)"
kubectl delete ns $NS --ignore-not-found --wait=true >/dev/null 2>&1
kubectl create ns $NS >/dev/null
CHPW="$(openssl rand -hex 12)"
k create secret generic otel-clickhouse-credentials --from-literal=username=e2e --from-literal=password="$CHPW" >/dev/null
k apply -f - >/dev/null <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: {name: clickhouse}
spec:
  selector: {matchLabels: {app: clickhouse}}
  template:
    metadata: {labels: {app: clickhouse}}
    spec:
      containers:
        - name: clickhouse
          image: $CH_IMAGE
          env:
            - {name: CLICKHOUSE_USER, value: e2e}
            - {name: CLICKHOUSE_PASSWORD, valueFrom: {secretKeyRef: {name: otel-clickhouse-credentials, key: password}}}
            - {name: CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT, value: "1"}
          ports: [{containerPort: 9000}]
          readinessProbe: {tcpSocket: {port: 9000}, periodSeconds: 3}
---
apiVersion: v1
kind: Service
metadata: {name: clickhouse}
spec: {selector: {app: clickhouse}, ports: [{port: 9000, targetPort: 9000}]}
EOF
k rollout status deploy/clickhouse --timeout=300s >/dev/null && pass "clickhouse up" || { fail "clickhouse"; exit 1; }
CH "CREATE TABLE krateo_audit (Timestamp DateTime64(9), TraceId String, SpanId String, SeverityText LowCardinality(String), ServiceName LowCardinality(String), Body String, ResourceAttributes Map(LowCardinality(String), String), LogAttributes Map(LowCardinality(String), String)) ENGINE = MergeTree ORDER BY Timestamp" \
  && pass "source table krateo_audit" || { fail "source table"; exit 1; }

step "1. The chart ($(grep '^version:' "$CHART/Chart.yaml" | awk '{print $2}')), short timings"
helm upgrade --install $REL "$CHART" -n $NS --wait \
  --set clickhouse.host=clickhouse.$NS.svc --set seal.lagSeconds=5 --set verify.staleAfterSeconds=$STALE >/dev/null 2>"$W/h.err" \
  && pass "chart installed" || { fail "chart install"; cat "$W/h.err"; exit 1; }
# Every run below is triggered by hand: suspend the schedules so a CronJob firing on its
# own (*/15 seal, hourly verify) can never change the state between steps. In D this IS
# the scenario: "the sealer is stopped".
for cj in seal verify; do k patch cronjob "$REL-$cj" -p '{"spec":{"suspend":true}}' >/dev/null; done
expect "$(k get cronjob $REL-seal $REL-verify -o jsonpath='{.items[*].spec.suspend}')" "true true" "seal and verify schedules suspended"

step "A. Fresh install"
expect "$(run verify v-fresh)" "Complete ok" "verify before the first seal: ok"

step "B. Mirror, seal, verify over a non-empty chain"
expect "$(run seal s1 | cut -d' ' -f1)" "Complete" "first seal (creates the WORM schema)"
insert 3; sleep 8
expect "$(CH "SELECT count() FROM krateo_audit_worm")" "3" "3 audit rows mirrored into the WORM table"
expect "$(run seal s2 | cut -d' ' -f1)" "Complete" "second seal"
expect "$(CH "SELECT row_count FROM krateo_audit_seal ORDER BY seal_seq DESC LIMIT 1")" "3" "seal #2 covers the 3 rows"
expect "$(run verify v-ok)" "Complete ok" "verify: ok"
expect "$(attr v-ok checked_seals)" "2" "verify checked 2 seals (not a vacuous ok)"

step "C. An edited WORM row is detected"
CH "ALTER TABLE krateo_audit_worm UPDATE Body = 'edited' WHERE Body = 'audit row 1' SETTINGS mutations_sync = 2"
expect "$(run verify v-tamper)" "Failed tampered" "verify: tampered, Job failed"

step "D. Seals deleted while the sealer is stopped"
CH "DROP TABLE krateo_audit_worm_mv"; CH "DROP TABLE krateo_audit_worm"; CH "DROP TABLE krateo_audit_seal"
expect "$(run seal s3 | cut -d' ' -f1)" "Complete" "fresh WORM schema and seal #1"
insert 2; sleep 2
CH "TRUNCATE TABLE krateo_audit_seal"
expect "$(run verify v-young)" "Complete ok" "rows younger than staleAfterSeconds, no seals: ok (no false positive)"
echo "      waiting $((STALE + 5))s for the rows to pass staleAfterSeconds…"
sleep $((STALE + 5))
expect "$(run verify v-unsealed)" "Failed unsealed" "rows older than staleAfterSeconds outside every seal: unsealed, Job failed"
expect "$(attr v-unsealed unsealed_rows)" "2" "unsealed_rows = 2"

echo
[ "$FAILS" -gt 0 ] && { echo "$FAILS check(s) failed"; exit 1; }
echo "all audit-worm checks passed"
