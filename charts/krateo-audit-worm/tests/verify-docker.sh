#!/usr/bin/env bash
# krateo-audit-worm's real seal.sh and verify.sh against a real ClickHouse in one container, with
# no cluster, so it also runs in CI. tests/e2e-kind.sh covers the Jobs; this covers the chain.
#
#   usage: tests/verify-docker.sh [chart-dir]
#
# Proves verify.sh reports each kind of chain damage, names the seal, and keeps a long chain
# inside activeDeadlineSeconds. It timed out at 264 seals on krateo-057 for 5 days.
set -uo pipefail
CHART="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="clickhouse/clickhouse-server:24.8-alpine"
NAME="audit-verify-$$"
LONG_CHAIN="${LONG_CHAIN:-5000}"
LIMIT=60
W="$(mktemp -d)"
trap 'docker rm -f "$NAME" >/dev/null 2>&1; rm -rf "$W"' EXIT
FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
expect() { if [ "$1" = "$2" ]; then pass "$3"; else fail "$3 (got '$1', want '$2')"; fi; }

mkdir -p "$W/scripts"
helm template t "$CHART" --set seal.lagSeconds=0 --show-only templates/configmap-scripts.yaml > "$W/cm.yaml"
python3 - "$W" <<'PY'
import sys, yaml
w = sys.argv[1]
for name, body in yaml.safe_load(open(f"{w}/cm.yaml"))["data"].items():
    open(f"{w}/scripts/{name}", "w").write(body)
PY
docker run -d --name "$NAME" -e CLICKHOUSE_USER=e2e -e CLICKHOUSE_PASSWORD=e2e -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 -v "$W/scripts:/scripts:ro" "$IMAGE" >/dev/null
CH() { docker exec -i "$NAME" clickhouse-client --user e2e --password e2e "$@"; }
for _ in $(seq 1 60); do CH -q "SELECT 1" >/dev/null 2>&1 && break; sleep 1; done
script() { docker exec -e CH_HOST=127.0.0.1 -e CH_PORT=9000 -e CH_DB=default -e CH_USER=e2e -e CH_PASSWORD=e2e -e LAG_SECONDS=0 "$NAME" sh "/scripts/$1.sh" 2>&1; }
# verify -> "<exit> <status> <problem_seals>"
verify() {
  local out rc; out=$(script verify); rc=$?
  local line; line=$(echo "$out" | grep '"krateo.audit.status"' | tail -1)
  echo "$rc $(echo "$line" | python3 -c 'import json,sys
t=sys.stdin.read().strip()
a=json.loads(t)["attributes"] if t else {}
print(a.get("krateo.audit.status","none"), a.get("krateo.audit.problem_seals",""))' 2>/dev/null)"
}
attr() { script verify | grep -o "\"krateo.audit.$1\":\"[^\"]*\"" | tail -1 | cut -d'"' -f4; }
insert() { CH -q "INSERT INTO krateo_audit SELECT now64(9), lower(hex(rand64())), lower(hex(rand64())), 'INFO', 'audit', concat('row ', toString(number), ' ', toString(rand())), map('a','1'), map('b','2') FROM numbers($1)"; }
restore() { CH -q "TRUNCATE TABLE krateo_audit_seal"; CH -q "INSERT INTO krateo_audit_seal SELECT * FROM good_seal"; }
mutate() { CH -q "ALTER TABLE krateo_audit_seal UPDATE $1 WHERE $2 SETTINGS mutations_sync = 2"; }

CH -q "CREATE TABLE krateo_audit (Timestamp DateTime64(9), TraceId String, SpanId String, SeverityText LowCardinality(String), ServiceName LowCardinality(String), Body String, ResourceAttributes Map(LowCardinality(String), String), LogAttributes Map(LowCardinality(String), String)) ENGINE = MergeTree ORDER BY Timestamp"

echo "== a clean chain, with an empty window in it =="
expect "$(verify)" "0 ok " "before the first seal: ok"
seal() { script seal >"$W/seal.out" || { fail "seal.sh failed"; cat "$W/seal.out"; }; sleep 1.1; }
seal; insert 3; seal; seal; insert 2; seal
expect "$(CH -q "SELECT arrayStringConcat(groupArray(toString(row_count)), ',') FROM (SELECT row_count FROM krateo_audit_seal ORDER BY seal_seq)")" "0,3,0,2" "seals cover 0,3,0,2 rows"
expect "$(verify)" "0 ok " "verify: ok"
expect "$(attr checked_seals)" "4" "4 seals checked"
expect "$(attr head_seal_seq)" "4" "chain head is seal 4"
CH -q "CREATE TABLE good_seal AS krateo_audit_seal"; CH -q "INSERT INTO good_seal SELECT * FROM krateo_audit_seal"

echo "== damage to a seal =="
BAD="repeat('0', 64)"
mutate "segment_hash = $BAD" "seal_seq = 2"
expect "$(verify)" "1 tampered 2:segment" "segment_hash edited"
restore
mutate "seal_hash = $BAD" "seal_seq = 2"
expect "$(verify)" "1 tampered 2:seal 3:linkage" "seal_hash edited: its seal and the next link"
restore
mutate "prev_seal_hash = $BAD" "seal_seq = 3"
expect "$(verify)" "1 tampered 3:seal 3:linkage" "prev_seal_hash edited"
restore
CH -q "ALTER TABLE krateo_audit_seal DELETE WHERE seal_seq = 2 SETTINGS mutations_sync = 2"
expect "$(verify | cut -d' ' -f1,2)" "1 tampered" "a middle seal deleted"
restore
mutate "window_end = window_start" "seal_seq = 2"
expect "$(verify)" "1 tampered 2:segment 2:seal" "a window narrowed so its rows fall outside it"
restore
CH -q "INSERT INTO krateo_audit_seal SELECT * FROM good_seal WHERE seal_seq = 2"
expect "$(verify | cut -d' ' -f1,2)" "1 tampered" "a seal duplicated"
restore
expect "$(verify)" "0 ok " "restored: ok again"

echo "== damage to a row =="
CH -q "ALTER TABLE krateo_audit_worm DELETE WHERE row_hash = (SELECT min(row_hash) FROM krateo_audit_worm) SETTINGS mutations_sync = 2"
expect "$(verify | cut -d' ' -f1,2)" "1 tampered" "a sealed row deleted"
CH -q "TRUNCATE TABLE krateo_audit_worm"
expect "$(verify | cut -d' ' -f1,2)" "1 tampered" "every sealed row deleted"

echo "== a query that fails must fail the run =="
CH -q "RENAME TABLE krateo_audit_seal TO seal_aside"
CH -q "CREATE VIEW krateo_audit_seal AS SELECT throwIf(1) AS seal_seq, now64(9) AS window_start, now64(9) AS window_end, '' AS segment_hash, '' AS prev_seal_hash, '' AS seal_hash, now64(9) AS sealed_at"
R=$(verify)
expect "${R#* }" "none " "the chain query errors: no status line, so no false ok"
[ "${R%% *}" != "0" ] && pass "the chain query errors: the Job fails" || fail "the chain query errored and the Job passed"
CH -q "DROP VIEW krateo_audit_seal"; CH -q "RENAME TABLE seal_aside TO krateo_audit_seal"

echo "== a long chain inside the deadline =="
CH -q "TRUNCATE TABLE krateo_audit_seal"; CH -q "TRUNCATE TABLE krateo_audit_worm"; CH -q "DROP TABLE krateo_audit_worm_mv"
CH -q "INSERT INTO krateo_audit SELECT now64(9), lower(hex(rand64())), lower(hex(rand64())), 'INFO', 'audit', toString(number), map('a','1'), map('b','2') FROM numbers($LONG_CHAIN)"
python3 "$HERE/gen-chain.py" "$LONG_CHAIN" "$NAME"
CH -q "CREATE MATERIALIZED VIEW krateo_audit_worm_mv TO krateo_audit_worm AS SELECT now64(9) AS ingested_at, Timestamp, TraceId, SpanId, SeverityText, ServiceName, Body, ResourceAttributes, LogAttributes, '' AS row_hash FROM krateo_audit WHERE 0"
S=$(date +%s); R=$(verify); E=$(( $(date +%s) - S ))
expect "$R" "0 ok " "$LONG_CHAIN seals: ok"
expect "$(attr checked_seals)" "$LONG_CHAIN" "all $LONG_CHAIN seals checked"
[ "$E" -le "$LIMIT" ] && pass "$LONG_CHAIN seals in ${E}s (limit ${LIMIT}s)" || fail "$LONG_CHAIN seals took ${E}s (limit ${LIMIT}s)"

echo
[ "$FAILS" -gt 0 ] && { echo "$FAILS check(s) failed"; exit 1; }
echo "all verify checks passed"
