#!/usr/bin/env bash
# krateo-slo-rollup end-to-end on a disposable kind cluster: the chart's real apply.sh, run by its
# real CronJob as the user the platform ships, against a real ClickHouse holding the exporter's
# tables. Jobs are triggered by hand from the CronJob.
#
#   usage: KUBECONFIG=<disposable kind cluster> tests/e2e-kind.sh [chart-dir]
#          (the current context must be kind-*; nothing is written otherwise)
#
# Proves:
#   A. source tables missing: the Job fails with a clear message and creates nothing
#   B. source tables empty: objects created, zero rows, a refresh succeeds, nothing fails
#   C. delta rows for two days and two routes: counts, error ratio and a p95 from summed buckets
#      equal the hand-computed values; cumulative rows and other metrics are left out; two bucket
#      layouts are kept apart and give no p95
#   D. re-running is a no-op: same DDL, no mutation, same answer after more refreshes
#   E. installed over rows already in the sources: backfills the same answer
#   F. rollup table dropped: the writer's insert still succeeds; the next apply restores the rollup
#   G. stopped view: the Job fails as stale, and recovers
#   H. ttlDays changed: the shipped user cannot ALTER, the Job says so and fails; with the grant it
#      applies, and the next run does nothing
#   M. mutations of the SQL (error filter, delta filter, metric filter, bucket sum) each fail C
set -uo pipefail
CHART="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${KUBECONFIG:?point KUBECONFIG at a disposable kind cluster}"
CTX="$(kubectl config current-context 2>/dev/null)"
case "$CTX" in
  kind-*) ;;
  *) echo "refusing: current context '$CTX' is not kind-*"; exit 1 ;;
esac
NS=slo-rollup-e2e; REL=krateo-slo-rollup; T=krateo_slo_daily
CH_IMAGE="clickhouse/clickhouse-server:26.8-alpine"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
export HELM_CONFIG_HOME="$W/helm/config" HELM_CACHE_HOME="$W/helm/cache" HELM_DATA_HOME="$W/helm/data"
k() { kubectl --context "$CTX" -n $NS "$@"; }
FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
expect() { if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (got '$1', want '$2')"; fi; }
check() { local m="$1"; shift; if "$@"; then pass "$m"; else fail "$m"; fi; }
logsays() { grep -q "$1" "$W/job.log" || { cat "$W/job.log"; return 1; }; }
step() { echo; echo "== $* =="; }
# ADMIN is the test's own superuser. WRITER is what the Secret holds: the grants of the shipped
# otelcollector principal, and nothing else, because the chart must work with exactly those.
ADMINPW="$(openssl rand -hex 12)"; WRITERPW="$(openssl rand -hex 12)"
CH() { k exec deploy/clickhouse -- clickhouse-client --user e2e --password "$ADMINPW" --format TSV -q "$1"; }
CHF() { k exec -i deploy/clickhouse -- clickhouse-client --user e2e --password "$ADMINPW" --format TSV --multiquery < "$1"; }
WRITER() { k exec -i deploy/clickhouse -- clickhouse-client --user writer --password "$WRITERPW" --multiquery; }
install() { # install <chart-dir> [helm args]
  local c="$1"; shift
  helm --kube-context "$CTX" upgrade --install $REL "$c" -n $NS --wait --set clickhouse.host=clickhouse.$NS.svc "$@" >/dev/null 2>"$W/h.err" \
    || { fail "helm upgrade"; cat "$W/h.err"; return 1; }
  # Every run below is triggered by hand: a schedule firing on its own would change the state between steps.
  k patch cronjob "$REL-apply" -p '{"spec":{"suspend":true}}' >/dev/null
}
run() { # run : trigger apply, wait, print Complete|Failed; the log is left in $W/job.log
  # Callers use $(run), a subshell, so a shell counter would never advance: the Job count lives in a file.
  local n; n=$(( $(cat "$W/jobn" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$W/jobn"
  local j="apply-$n" c=""
  k create job "$j" --from="cronjob/$REL-apply" >/dev/null
  for _ in $(seq 1 90); do
    c=$(k get job "$j" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{"\n"}{end}' 2>/dev/null | grep -E '^(Complete|Failed)$' | head -1)
    [ -n "$c" ] && break; sleep 2
  done
  k logs "job/$j" >"$W/job.log" 2>&1
  echo "${c:-Timeout}"
}
refresh() { CH "SYSTEM REFRESH VIEW ${T}_mv"; CH "SYSTEM WAIT VIEW ${T}_mv"; }
wipe() { CH "DROP VIEW IF EXISTS ${T}_mv"; CH "DROP TABLE IF EXISTS $T"; }
query() { CHF "$HERE/rollup-query.sql"; }

# Hand-computed from fixture.sql. Summed buckets over both days and routes:
#   counts   [0, 500, 600, 1000, 300, 0, 10, 0, 40, 50, 0, 0]  total 2500
#   running  [0, 500, 1100, 2100, 2400, 2400, 2410, 2410, 2450, 2500, 2500, 2500]
# p95 rank = 0.95 * 2500 = 2375, in bucket 5 (250, 500]: 250 + 250 * (2375 - 2100) / 300 = 479.2
# /call [0,0,200,900,300,0,10,...] total 1410, rank 1339.5, 250 + 250 * 239.5 / 300 = 449.6
# /list [0,500,400,100,0,0,0,0,40,50,...] total 1090, rank 1035.5, 1500 + 1000 * 35.5 / 40 = 2387.5
# Errors: 10 of 1410 on /call, 90 of 1090 on /list, 100 of 2500 overall.
WANT="$(printf '(all routes)\t2500\t100\t0.04\t479.2\n/call\t1410\t10\t0.00709\t449.6\n/list\t1090\t90\t0.08257\t2387.5')"

step "0. ClickHouse, and the user the platform ships"
kubectl --context "$CTX" delete ns $NS --ignore-not-found --wait=true >/dev/null 2>&1
kubectl --context "$CTX" create ns $NS >/dev/null
k create secret generic otel-clickhouse-credentials --from-literal=username=writer --from-literal=password="$WRITERPW" >/dev/null
k create secret generic e2e-admin --from-literal=password="$ADMINPW" >/dev/null
k apply -f - >/dev/null <<YAML
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
            - {name: CLICKHOUSE_PASSWORD, valueFrom: {secretKeyRef: {name: e2e-admin, key: password}}}
            - {name: CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT, value: "1"}
          ports: [{containerPort: 9000}]
          readinessProbe: {tcpSocket: {port: 9000}, periodSeconds: 3}
---
apiVersion: v1
kind: Service
metadata: {name: clickhouse}
spec: {selector: {app: clickhouse}, ports: [{port: 9000, targetPort: 9000}]}
YAML
check "clickhouse up" k rollout status deploy/clickhouse --timeout=300s
[ "$FAILS" = 0 ] || exit 1
check "writer created" CH "CREATE USER writer IDENTIFIED WITH sha256_password BY '$WRITERPW'"
check "writer has the grants of the shipped otelcollector principal" \
  CH "GRANT SHOW, SELECT, INSERT, CREATE DATABASE, CREATE TABLE, CREATE VIEW, CREATE DICTIONARY ON default.* TO writer"
[ "$FAILS" = 0 ] || exit 1

step "A. Source tables missing"
install "$CHART" || exit 1
expect "$(run)" "Failed" "apply fails while the collector has not created the sources"
check "the log says why" logsays 'does not exist yet'
expect "$(CH "SELECT count() FROM system.tables WHERE database = 'default' AND name LIKE '$T%'")" "0" "nothing was created"

step "B. Source tables empty: safe to install before data exists"
check "source tables, with the exporter's DDL" CHF "$HERE/otel-metrics-tables.sql"
[ "$FAILS" = 0 ] || exit 1
expect "$(run)" "Complete" "apply"
expect "$(CH "SELECT engine FROM system.tables WHERE database = 'default' AND name = '$T'")" "ReplacingMergeTree" "rollup table"
expect "$(CH "SELECT engine FROM system.tables WHERE database = 'default' AND name = '${T}_mv'")" "MaterializedView" "view"
refresh
expect "$(CH "SELECT exception FROM system.view_refreshes WHERE view = '${T}_mv'")" "" "a refresh over empty sources does not fail"
expect "$(CH "SELECT count() FROM $T")" "0" "and writes nothing"

step "C. Known rows, known answers"
if WRITER < "$HERE/fixture.sql"; then pass "the writer's inserts succeed"; else fail "the writer's inserts"; fi
refresh
expect "$(query)" "$WANT" "counts, error ratio and p95 from summed buckets, per route and overall"
expect "$(CH "SELECT sum(calls) FROM $T FINAL WHERE service = 'snowplow' AND day = today() - 3")" "1000" "day 1: rows summed, not last-wins"
expect "$(CH "SELECT sum(calls) FROM $T FINAL WHERE service = 'snowplow' AND day = today() - 2")" "1500" "day 2"
expect "$(CH "SELECT uniqExact(bounds) FROM $T FINAL WHERE service = 'bounds-changed'")" "2" "two bucket layouts stay two rows"
sed "s/'snowplow'/'bounds-changed'/" "$HERE/rollup-query.sql" > "$W/q.sql"
expect "$(CHF "$W/q.sql" | head -1 | cut -f5)" '\N' "two bucket layouts in scope give no p95"

step "D. Re-running is a no-op"
DDL="$(CH "SELECT create_table_query FROM system.tables WHERE database = 'default' AND name IN ('$T', '${T}_mv') ORDER BY name")"
expect "$(run)" "Complete" "apply again"
install "$CHART" || exit 1
expect "$(run)" "Complete" "helm upgrade, apply again"
expect "$(CH "SELECT create_table_query FROM system.tables WHERE database = 'default' AND name IN ('$T', '${T}_mv') ORDER BY name")" "$DDL" "DDL unchanged"
expect "$(CH "SELECT count() FROM system.mutations WHERE table = '$T'")" "0" "no ALTER ran"
refresh; sleep 1; refresh
expect "$(query)" "$WANT" "two more refreshes: nothing is counted twice"

step "E. Installed over rows already in the sources"
wipe
expect "$(run)" "Complete" "apply on a clean slate"
refresh
expect "$(query)" "$WANT" "the first refresh backfills what the sources hold"

step "F. A broken rollup cannot touch ingestion"
CH "DROP TABLE $T"
before="$(CH "SELECT count() FROM otel_metrics_sum")"
if WRITER <<'SQL'
INSERT INTO otel_metrics_sum (ServiceName, MetricName, Attributes, StartTimeUnix, TimeUnix, Value, AggregationTemporality, IsMonotonic) VALUES ('host', 'system.cpu.time', map(), now() - 60, now(), 1, 2, true);
SQL
then pass "the writer's insert succeeds with the rollup table gone"; else fail "the writer's insert with the rollup table gone"; fi
expect "$(CH "SELECT count() FROM otel_metrics_sum")" "$((before + 1))" "and its row landed"
expect "$(run)" "Complete" "apply restores the table"
refresh
expect "$(query)" "$WANT" "and the next refresh restores the answer"

step "G. A stopped view fails the Job"
CH "SYSTEM STOP VIEW ${T}_mv"
CH "TRUNCATE TABLE $T"
WRITER <<'SQL'
INSERT INTO otel_metrics_sum (ServiceName, MetricName, Attributes, StartTimeUnix, TimeUnix, Value, AggregationTemporality, IsMonotonic) VALUES ('snowplow', 'traces.span.metrics.calls', map('span.name','GET /late','http.route','/late','krateo.sli.error','false'), now() - 10860, now() - 10800, 1, 1, true);
SQL
expect "$(run)" "Failed" "apply fails: a source row older than staleAfterSeconds is newer than the last refresh"
check "the log says stale" logsays '^stale: '
CH "SYSTEM START VIEW ${T}_mv"
refresh
expect "$(run)" "Complete" "apply passes again after a refresh"

step "H. A changed ttlDays"
install "$CHART" --set rollup.ttlDays=45 || exit 1
expect "$(run)" "Failed" "the shipped user cannot ALTER: the Job fails rather than pretend"
check "the log names the grant and the statement" logsays 'needs ALTER TTL'
expect "$(CH "SELECT extract(engine_full, 'toIntervalDay[(]([0-9]+)') FROM system.tables WHERE database = 'default' AND name = '$T'")" "40" "the table keeps 40 days"
CH "GRANT ALTER TTL ON default.$T TO writer"
expect "$(run)" "Complete" "with ALTER TTL granted the apply succeeds"
expect "$(CH "SELECT extract(engine_full, 'toIntervalDay[(]([0-9]+)') FROM system.tables WHERE database = 'default' AND name = '$T'")" "45" "the table now keeps 45 days"
m="$(CH "SELECT count() FROM system.mutations WHERE table = '$T'")"
expect "$(run)" "Complete" "apply again"
expect "$(CH "SELECT count() FROM system.mutations WHERE table = '$T'")" "$m" "no second ALTER"
CH "REVOKE ALTER TTL ON default.$T FROM writer"
install "$CHART" || exit 1

step "M. Each assertion can fail: mutate the SQL, expect a wrong answer"
CH "ALTER TABLE otel_metrics_sum DELETE WHERE MetricName = 'traces.span.metrics.calls' AND Attributes['http.route'] = '/late' SETTINGS mutations_sync = 2"
mutant() { # mutant <name> <sed expression>
  rm -rf "$W/mut"; cp -R "$CHART" "$W/mut"
  sed -i.bak "$2" "$W/mut/templates/configmap-scripts.yaml"; rm -f "$W/mut/templates/configmap-scripts.yaml.bak"
  cmp -s "$CHART/templates/configmap-scripts.yaml" "$W/mut/templates/configmap-scripts.yaml" && { fail "$1: the mutation changed nothing"; return; }
  wipe
  install "$W/mut" || return
  [ "$(run)" = "Complete" ] || { fail "$1: apply"; return; }
  refresh
  if [ "$(query)" = "$WANT" ]; then fail "$1: the mutant survived"; else pass "$1: detected"; fi
}
mutant "error filter dropped" "s/Attributes\['krateo.sli.error'\] = 'true'  */1 /"
mutant "delta filter dropped" "/AND AggregationTemporality = 1/d"
mutant "metric filter dropped" "s/WHERE MetricName = 'traces.span.metrics.[a-z]*'/WHERE 1/"
mutant "buckets not summed" "s/sumForEach(BucketCounts)/any(BucketCounts)/"
# The unmutated chart comes back, so a failure below this line is the chart, not the harness.
wipe; install "$CHART" || exit 1
expect "$(run)" "Complete" "clean chart back"
refresh
expect "$(query)" "$WANT" "and the right answer with it"

echo
[ "$FAILS" -gt 0 ] && { echo "$FAILS check(s) failed"; exit 1; }
echo "all slo-rollup checks passed"
