#!/usr/bin/env bash
# krateo-slo-rollup end-to-end on a disposable kind cluster: the chart's real apply.sh, run by its
# real CronJob as the user the platform ships for it (krateo-observability's slo_rollup, with exactly
# its grants), against a real ClickHouse holding the exporter's tables. The fixture is inserted by a
# second user with the collector's grants, as on the platform. Jobs are triggered by hand.
#
#   usage: KUBECONFIG=<disposable kind cluster> tests/e2e-kind.sh [chart-dir]
#          (the current context must be kind-*; nothing is written otherwise)
#
# Proves:
#   0. slo_rollup has exactly the four grants SHOW GRANTS lists on krateo-057, and no more
#   A. the CronJob reads clickhouse-slo-rollup-credentials, not the collector's Secret; source
#      tables missing: the Job fails with a clear message and creates nothing
#   B. source tables empty: objects created, zero rows, a refresh succeeds, nothing fails; every
#      DDL ran as slo_rollup and the view refreshes as its definer, slo_rollup
#   C. delta rows for two days, two API routes and three UI span names, read with THE 30-day
#      statements (30d-1..30d-3 of ops/slo-alert/sli-span-metrics.sql, cut from the file as
#      committed): availability, error budget and p50/p95 bucket intervals equal the hand-computed
#      values; whole UTC days (midnight, mid-day and now-30d..now windows); cumulative rows and
#      other metrics are left out; two bucket layouts stay two rows and are never summed
#   D. re-running is a no-op: same DDL, no mutation, same answer after more refreshes
#   E. installed over rows already in the sources: backfills the same answer
#   F. rollup table dropped: the writer's insert still succeeds; the next apply restores the rollup
#   G. stopped view: the Job fails as stale, and recovers
#   H. ttlDays changed: with the shipped grants (ALTER TTL on the rollup) it applies, and the next
#      run does nothing; with ALTER TTL revoked the Job says so and fails; re-granted, it applies
#   M. mutations of the SQL (error filter, delta filter, metric filter, bucket sum) each fail C
#
# There is no query of the chart's own: the rollup is read the one way every reader reads it, with
# the 30-day statements of ops/slo-alert/sli-span-metrics.sql (SLI_SQL overrides the path). Their
# percentiles are bucket intervals (lower, upper], upper-inclusive, never interpolated.
set -uo pipefail
CHART="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SLI_SQL="${SLI_SQL:-$HERE/../../../ops/slo-alert/sli-span-metrics.sql}"
[ -f "$SLI_SQL" ] || { echo "no $SLI_SQL: run from the repo, or set SLI_SQL"; exit 1; }
: "${KUBECONFIG:?point KUBECONFIG at a disposable kind cluster}"
CTX="$(kubectl config current-context 2>/dev/null)"
case "$CTX" in
  kind-*) ;;
  *) echo "refusing: current context '$CTX' is not kind-*"; exit 1 ;;
esac
NS=slo-rollup-e2e; REL=krateo-slo-rollup; T=krateo_slo_daily
# The server krateo-057 runs (krateo-observability pins 26.10.3.7), as tests/sli-span-metrics-docker.py.
CH_IMAGE="clickhouse/clickhouse-server:26.10.3.7"
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
# ADMIN is the test's own superuser. WRITER has the grants of the shipped otelcollector principal and
# inserts the fixture, as the collectors do. SLO is what the chart's Secret holds: the slo_rollup
# user krateo-observability ships, with its grants and nothing else, because the chart must work
# with exactly those. The writer's Secret exists too, as on the platform, so a chart that still
# read it would run (as the writer) and be caught by the checks in A and B, not by a missing Secret.
ADMINPW="$(openssl rand -hex 12)"; WRITERPW="$(openssl rand -hex 12)"; SLOPW="$(openssl rand -hex 12)"
SLO_GRANTS="$(printf '%s\n' \
  "GRANT SELECT, INSERT, ALTER TTL, CREATE TABLE ON default.$T TO slo_rollup" \
  "GRANT CREATE VIEW ON default.${T}_mv TO slo_rollup" \
  "GRANT SELECT ON default.otel_metrics_histogram TO slo_rollup" \
  "GRANT SELECT ON default.otel_metrics_sum TO slo_rollup" | sort)"
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
# The statements of sli-span-metrics.sql, cut as tests/sli-span-metrics-docker.py cuts them:
# comments out first (one holds a ';'), then split on ';'. 6, 7, 8 are 30d-1, 30d-2, 30d-3.
stmt() { sed 's/--.*$//' "$SLI_SQL" | awk -v n="$1" 'BEGIN { RS = ";" } /[^[:space:]]/ { if (++c == n) { print; exit } }'; }
nstmt() { sed 's/--.*$//' "$SLI_SQL" | awk 'BEGIN { RS = ";" } /[^[:space:]]/ { c++ } END { print c }'; }
# CHP <start> <end> <sql>: run as a reader does, with clickhouse-client's --param_start/--param_end.
CHP() { k exec deploy/clickhouse -- clickhouse-client --user e2e --password "$ADMINPW" --format TSV \
          --param_start="$1" --param_end="$2" -q "$3"; }
# 30d-1 as committed, its three ratios rounded to 5 places so the float tails do not decide the test.
s30d1() { CHP "$1" "$2" "SELECT sli_service, calls, sli_errors, round(availability, 5), round(error_budget_calls, 5),
  round(budget_remaining, 5), first_day, last_day, days_with_data FROM ($(stmt 6))"; }
# 30d-2 has no ORDER BY (rows of two layouts come back in either order): sorted here. $3 swaps the
# service, for the layout check.
s30d2() { CHP "$1" "$2" "$(stmt 7 | sed "s/service = 'snowplow'/service = '${3:-snowplow}'/")" | LC_ALL=C sort; }
s30d3() { CHP "$1" "$2" "$(stmt 8)"; }
# The fixture's two days: [D3, D1) holds both, [D3, D2) only the first. Set once the fixture lands.
query() { s30d1 "$D3" "$D1"; s30d2 "$D3" "$D1"; s30d3 "$D3" "$D1"; }

# What the three statements must return over the fixture's two days, worked by hand from fixture.sql.
# Bounds (ms) [25,50,100,250,500,750,1000,1500,2500,5000,10000]: bucket i is (b[i-1], b[i]], bucket 1
# is (0, 25], bucket 12 is (10000, inf). A percentile is the bucket i where the running total first
# reaches q*n (>=, so a rank equal to the running total at a bound is IN the bucket ending there).
# 30d-1, call rows only (bounds empty), per SLI class; budget = 0.001 * calls, remaining = 1 - errors / budget:
#   krateo-frontend  page.load 39 + 1 error, form.submit 2, widget.fetch 58 = 100 calls, 1 error:
#                    availability 0.99, budget 0.1, remaining 1 - 1/0.1 = -9 (overspent)
#   snowplow         /call 600 + 300 + 10 errors + 500, /list 90 errors + 1000 = 2500 calls, 100 errors:
#                    availability 0.96, budget 2.5, remaining 1 - 100/2.5 = -39
#   both: first_day today-3, last_day today-2, 2 days with data
# 30d-2, snowplow, one layout. Summed buckets over both days and routes:
#   counts   [0, 500, 600, 1000, 300, 0, 10, 0, 40, 50, 0, 0]  n = 2500
#   running  [0, 500, 1100, 2100, 2400, 2400, 2410, 2410, 2450, 2500, 2500, 2500]
#   p50 rank 1250 -> bucket 4 (100, 250]; p95 rank 2375 -> bucket 5 (250, 500]; 500 <= 500 -> within 1
#   (0.1.2's interpolating query read this p95 as 479.2: the histogram only says "in (250, 500]")
# 30d-3, krateo-frontend, per interaction (ORDER BY interaction); krateo.widget.fetch is not one:
#   krateo.form.submit  [0 x 11, 2], n 2: ranks 1 and 1.9 -> bucket 12, past the last bound:
#                       (10000, inf] for both; inf > 2000 -> within 0
#   krateo.page.load    [0,0,0,0,0,20,10,9,0,1,0,0], n 40, running ..20, 30, 39, 39, 40:
#                       p50 rank 20 = the running total at 750 -> bucket 6 (500, 750], not (750, 1000];
#                       p95 rank 38 -> bucket 8 (1000, 1500]; 1500 <= 2000 -> within 1
B11='[25,50,100,250,500,750,1000,1500,2500,5000,10000]'
want() { # want <first day> <last day>: the three statements' rows over the fixture's two days
  printf '%s\n' \
    "krateo-frontend	100	1	0.99	0.1	-9	$1	$2	2" \
    "snowplow	2500	100	0.96	2.5	-39	$1	$2	2" \
    "$B11	2500	4	5	100	250	250	500	1" \
    "krateo.form.submit	$B11	2	12	12	10000	inf	10000	inf	0"
  printf '%s' "krateo.page.load	$B11	40	6	8	500	750	1000	1500	1"
}

step "0. ClickHouse, and the users the platform ships"
kubectl --context "$CTX" delete ns $NS --ignore-not-found --wait=true >/dev/null 2>&1
kubectl --context "$CTX" create ns $NS >/dev/null
k create secret generic otel-clickhouse-credentials --from-literal=username=writer --from-literal=password="$WRITERPW" >/dev/null
k create secret generic clickhouse-slo-rollup-credentials --from-literal=username=slo_rollup --from-literal=password="$SLOPW" >/dev/null
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
# The fixture's days and the windows below are UTC dates; the statements read UTC days.
expect "$(CH "SELECT timezone()")" "UTC" "the server's timezone is UTC"
expect "$(nstmt)" "8" "sli-span-metrics.sql has its 8 statements"
for n in 6 7 8; do
  check "statement $n reads krateo_slo_daily" grep -q 'FROM krateo_slo_daily' <<<"$(stmt $n)"
done
check "writer created" CH "CREATE USER writer IDENTIFIED WITH sha256_password BY '$WRITERPW'"
check "writer has the grants of the shipped otelcollector principal" \
  CH "GRANT SHOW, SELECT, INSERT, CREATE DATABASE, CREATE TABLE, CREATE VIEW, CREATE DICTIONARY ON default.* TO writer"
check "slo_rollup created" CH "CREATE USER slo_rollup IDENTIFIED WITH sha256_password BY '$SLOPW'"
while IFS= read -r g; do check "slo_rollup: $g" CH "$g"; done <<<"$SLO_GRANTS"
slo_grants() { CH "SHOW GRANTS FOR slo_rollup" | sort; }
expect "$(slo_grants)" "$SLO_GRANTS" "slo_rollup's SHOW GRANTS is exactly krateo-057's four statements"
[ "$FAILS" = 0 ] || exit 1

step "A. The CronJob runs as slo_rollup; source tables missing"
install "$CHART" || exit 1
for v in CH_USER CH_PASSWORD; do
  expect "$(k get cronjob "$REL-apply" -o jsonpath="{.spec.jobTemplate.spec.template.spec.containers[0].env[?(@.name==\"$v\")].valueFrom.secretKeyRef.name}")" \
    "clickhouse-slo-rollup-credentials" "by default $v comes from slo_rollup's Secret, not the collector's"
done
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
CH "SYSTEM FLUSH LOGS"
expect "$(CH "SELECT arrayStringConcat(arraySort(groupUniqArray(user)), ',') FROM system.query_log WHERE type = 'QueryFinish' AND query_kind = 'Create' AND query LIKE '%IF NOT EXISTS $T%'")" \
  "slo_rollup" "the table and the view were created by slo_rollup, and by no one else"
expect "$(CH "SELECT count() FROM system.tables WHERE database = 'default' AND name = '${T}_mv' AND create_table_query LIKE '%DEFINER = slo_rollup SQL SECURITY DEFINER%'")" \
  "1" "the view refreshes as its definer, slo_rollup"

step "C. Known rows, known answers"
if WRITER < "$HERE/fixture.sql"; then pass "the writer's inserts succeed"; else fail "the writer's inserts"; fi
refresh
# Midnight of the fixture's first day, from the fixture itself (today() could roll over mid-run).
D3="$(CH "SELECT toDateTime(toDate(min(TimeUnix), 'UTC'), 'UTC') FROM otel_metrics_sum WHERE ServiceName = 'snowplow'")"
D2="$(CH "SELECT toDateTime('$D3', 'UTC') + INTERVAL 1 DAY")"; D1="$(CH "SELECT toDateTime('$D3', 'UTC') + INTERVAL 2 DAY")"
DAY3="${D3%% *}"; DAY2="${D2%% *}"
WANT="$(want "$DAY3" "$DAY2")"
expect "$(query)" "$WANT" "30d-1..30d-3 over [today-3, today-1): availability, budget, p50/p95 bucket intervals"
# Whole UTC days: [today-3, today-2) is the first day alone. On it: frontend 39 calls, no error,
# budget 0.039, remaining 1; snowplow 1000 calls (600 + 300 + 10 + 90), 100 errors, availability 0.9,
# budget 1, remaining 1 - 100/1 = -99.
ONE="$(printf '%s\n%s' "krateo-frontend	39	0	1	0.039	1	$DAY3	$DAY3	1" "snowplow	1000	100	0.9	1	-99	$DAY3	$DAY3	1")"
expect "$(s30d1 "$D3" "$D2")" "$ONE" "30d-1: end at midnight leaves end's day out"
expect "$(s30d1 "${DAY3} 12:00:00" "${DAY2} 12:00:00")" "$ONE" "30d-1: mid-day bounds take start's day whole and leave end's day out whole"
expect "$(s30d1 "$(CH "SELECT now() - INTERVAL 30 DAY")" "$(CH "SELECT now()")")" "$(head -2 <<<"$WANT")" \
  "30d-1: start = now - 30 days, end = now reads both fixture days"
expect "$(CH "SELECT route, sum(calls), sumIf(calls, sli_error) FROM $T FINAL WHERE service = 'snowplow' AND empty(bounds) GROUP BY route ORDER BY route")" \
  "$(printf '/call\t1410\t10\n/list\t1090\t90')" "per route: http.route and the error flag land in their key columns"
expect "$(CH "SELECT sum(calls) FROM $T FINAL WHERE service = 'snowplow' AND day = '$DAY3'")" "1000" "day 1: rows summed, not last-wins"
expect "$(CH "SELECT sum(calls) FROM $T FINAL WHERE service = 'snowplow' AND day = '$DAY2'")" "1500" "day 2"
expect "$(CH "SELECT uniqExact(bounds) FROM $T FINAL WHERE service = 'bounds-changed'")" "2" "two bucket layouts stay two rows"
# Two layouts in one window: 30d-2 returns one row per layout and never sums them; a reader then
# reports no percentile for the window. [100,200]: [1,2,0], n 3, p50 rank 1.5 and p95 rank 2.85 ->
# bucket 2 (100, 200]. [100,200,300]: [1,1,1,1], n 4, p50 rank 2 = the running total at 200 ->
# (100, 200]; p95 rank 3.8 -> bucket 4, (300, inf], not within 500.
expect "$(s30d2 "$D3" "$D1" bounds-changed)" \
  "$(printf '%s\n%s' "[100,200,300]	4	2	4	100	200	300	inf	0" "[100,200]	3	2	2	100	200	100	200	1")" \
  "30d-2: two bucket layouts in scope give one row each, no summed percentile"

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
ttl() { CH "SELECT extract(engine_full, 'toIntervalDay[(]([0-9]+)') FROM system.tables WHERE database = 'default' AND name = '$T'"; }
install "$CHART" --set rollup.ttlDays=45 || exit 1
expect "$(run)" "Complete" "the shipped grants include ALTER TTL on the rollup: the apply succeeds"
check "the log says what changed" logsays 'ttl changed: 40 -> 45 days'
expect "$(ttl)" "45" "the table now keeps 45 days"
m="$(CH "SELECT count() FROM system.mutations WHERE table = '$T'")"
expect "$(run)" "Complete" "apply again"
expect "$(CH "SELECT count() FROM system.mutations WHERE table = '$T'")" "$m" "no second ALTER"
# Negative arm: without ALTER TTL the Job must fail loudly, not pretend.
CH "REVOKE ALTER TTL ON default.$T FROM slo_rollup"
install "$CHART" --set rollup.ttlDays=50 || exit 1
expect "$(run)" "Failed" "with ALTER TTL revoked the Job fails rather than pretend"
check "the log names the grant and the statement" logsays 'needs ALTER TTL'
expect "$(ttl)" "45" "the table keeps 45 days"
CH "GRANT ALTER TTL ON default.$T TO slo_rollup"
expect "$(slo_grants)" "$SLO_GRANTS" "re-granted: slo_rollup's grants are krateo-057's four again"
expect "$(run)" "Complete" "re-granted, the apply succeeds"
expect "$(ttl)" "50" "the table now keeps 50 days"
install "$CHART" || exit 1
expect "$(run)" "Complete" "back to the default ttlDays"
expect "$(ttl)" "40" "the table keeps 40 days again"

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
