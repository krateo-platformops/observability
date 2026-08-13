#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Budget DDL smoke test
#
# Actually executes ddl/001_budgets.sql + ddl/002_budget_status.sql against a
# throwaway ClickHouse (clickhouse-local; docker fallback), together with stub
# `showback_daily` / `showback_daily_by_tag` rollup tables whose schemas
# mirror the showback engine DDL (006_showback_daily.sql /
# 008_showback_daily_by_tag.sql). It then inserts sample budgets + spend and
# asserts that the `budget_status` view materializes the expected statuses.
#
# Covered:
#   - {{database}} rendering (same convention as the showback engine DDL)
#   - ReplacingMergeTree FINAL semantics (a newer budget row supersedes an
#     older one with the same scope + budget_id)
#   - ok / warning / breached classification (warn_ratio and amount edges)
#   - tag-scoped budgets (showback_daily_by_tag arm, tag_value filtering)
#   - period windows (daily budgets ignore yesterday; monthly ignore old rows)
#   - enabled=0 budgets excluded; budgets with no spend report ok/0
#
# Usage:
#   ./smoke-budget-ddl.sh
#
# Environment:
#   SMOKE_DATABASE    database name to render into {{database}} (default: showback)
#   CLICKHOUSE_IMAGE  image for the docker fallback (default: clickhouse/clickhouse-server:24.8-alpine)
#
# Exit codes: 0 = pass, 1 = assertion/DDL failure, 2 = no ClickHouse runtime.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DDL_DIR="$SCRIPT_DIR/../ddl"
DB="${SMOKE_DATABASE:-showback}"
IMAGE="${CLICKHOUSE_IMAGE:-clickhouse/clickhouse-server:24.8-alpine}"

run_clickhouse() {
  # One clickhouse-local session, multiquery SQL on stdin.
  if command -v clickhouse-local >/dev/null 2>&1; then
    clickhouse-local --multiquery
  elif command -v clickhouse >/dev/null 2>&1; then
    clickhouse local --multiquery
  elif command -v docker >/dev/null 2>&1; then
    docker run --rm -i --entrypoint clickhouse "$IMAGE" local --multiquery
  else
    echo "[smoke] SKIP: no clickhouse-local, clickhouse or docker on PATH" >&2
    exit 2
  fi
}

SQL_FILE="$(mktemp)"
trap 'rm -f "$SQL_FILE"' EXIT

{
  echo "CREATE DATABASE IF NOT EXISTS $DB;"

  # Stub rollups — schemas copied from the showback engine DDL
  # (006_showback_daily.sql / 008_showback_daily_by_tag.sql).
  cat <<EOF
CREATE TABLE $DB.showback_daily
(
    day      Date,
    org      LowCardinality(String),
    tenant   LowCardinality(String),
    service  LowCardinality(String),
    metric   LowCardinality(String),
    currency LowCardinality(String),
    quantity Float64,
    cost     Float64
)
ENGINE = SummingMergeTree((quantity, cost))
PARTITION BY toYYYYMM(day)
ORDER BY (org, tenant, service, metric, currency, day);

CREATE TABLE $DB.showback_daily_by_tag
(
    day       Date,
    org       LowCardinality(String),
    tenant    LowCardinality(String),
    service   LowCardinality(String),
    metric    LowCardinality(String),
    currency  LowCardinality(String),
    tag_key   LowCardinality(String),
    tag_value String,
    quantity  Float64,
    cost      Float64
)
ENGINE = SummingMergeTree((quantity, cost))
PARTITION BY toYYYYMM(day)
ORDER BY (org, tenant, service, metric, currency, tag_key, tag_value, day);
EOF

  # The DDL under test, rendered exactly like the showback engine renders it.
  sed "s/{{database}}/$DB/g" "$DDL_DIR/001_budgets.sql"
  echo ";"
  sed "s/{{database}}/$DB/g" "$DDL_DIR/002_budget_status.sql"
  echo ";"

  cat <<EOF
-- Budgets. b2-warn is inserted twice with the same scope + budget_id:
-- the second (newer updated_at) row must win via ReplacingMergeTree FINAL.
INSERT INTO $DB.budgets
    (budget_id, org, tenant, service, tag_key, tag_value, period, amount, currency, warn_ratio, enabled, updated_at)
VALUES
    ('b1-ok',      'acme', '', 'svc-a', '',     '',      'monthly', 1000, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000'),
    ('b2-warn',    'acme', '', 'svc-b', '',     '',      'monthly',   10, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000');

INSERT INTO $DB.budgets
    (budget_id, org, tenant, service, tag_key, tag_value, period, amount, currency, warn_ratio, enabled, updated_at)
VALUES
    ('b2-warn',    'acme', '', 'svc-b', '',     '',      'monthly',  100, 'EUR', 0.8, 1, '2026-01-02 00:00:00.000'),
    ('b3-breach',  'acme', '', 'svc-c', '',     '',      'monthly',  100, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000'),
    ('b4-tag',     'acme', '', '',      'team', 'alpha', 'monthly',   50, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000'),
    ('b5-daily',   'acme', '', 'svc-d', '',     '',      'daily',    100, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000'),
    ('b6-monthly', 'acme', '', 'svc-e', '',     '',      'monthly',  100, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000'),
    ('b7-disabled','acme', '', 'svc-f', '',     '',      'monthly',  100, 'EUR', 0.8, 0, '2026-01-01 00:00:00.000'),
    ('b8-nospend', 'acme', '', 'svc-g', '',     '',      'monthly',  100, 'EUR', 0.8, 1, '2026-01-01 00:00:00.000');

-- Spend. Rows dated 2020-01-01 are outside every current window; the
-- yesterday() row must be excluded from the daily budget b5 only.
INSERT INTO $DB.showback_daily
    (day, org, tenant, service, metric, currency, quantity, cost)
VALUES
    (today(),      'acme', 't1', 'svc-a', 'cpu', 'EUR', 1, 100),
    (today(),      'acme', 't1', 'svc-b', 'cpu', 'EUR', 1,  85),
    (today(),      'acme', 't1', 'svc-c', 'cpu', 'EUR', 1, 150),
    (today(),      'acme', 't1', 'svc-d', 'cpu', 'EUR', 1,  30),
    (yesterday(),  'acme', 't1', 'svc-d', 'cpu', 'EUR', 1, 500),
    (today(),      'acme', 't1', 'svc-e', 'cpu', 'EUR', 1,  20),
    ('2020-01-01', 'acme', 't1', 'svc-e', 'cpu', 'EUR', 1, 999),
    (today(),      'acme', 't1', 'svc-f', 'cpu', 'EUR', 1, 500);

INSERT INTO $DB.showback_daily_by_tag
    (day, org, tenant, service, metric, currency, tag_key, tag_value, quantity, cost)
VALUES
    (today(), 'acme', 't1', 'svc-x', 'cpu', 'EUR', 'team', 'alpha', 1,  60),
    (today(), 'acme', 't1', 'svc-x', 'cpu', 'EUR', 'team', 'beta',  1, 999);

SELECT budget_id, amount, spend, status
FROM $DB.budget_status
ORDER BY budget_id
FORMAT TSV;
EOF
} > "$SQL_FILE"

echo "[smoke] rendering {{database}} -> $DB and executing DDL 001+002 + sample data"
ACTUAL="$(run_clickhouse < "$SQL_FILE")"

EXPECTED="$(cat <<'EOF'
b1-ok	1000	100	ok
b2-warn	100	85	warning
b3-breach	100	150	breached
b4-tag	50	60	breached
b5-daily	100	30	ok
b6-monthly	100	20	ok
b8-nospend	100	0	ok
EOF
)"

echo "[smoke] budget_status materialized:"
printf '%s\n' "$ACTUAL" | sed 's/^/  /'

if [ "$ACTUAL" = "$EXPECTED" ]; then
  echo "[smoke] PASS: budget_status matches expected statuses (FINAL, tag scope, period windows, enabled flag all verified)"
else
  echo "[smoke] FAIL: budget_status differs from expected:" >&2
  diff <(printf '%s\n' "$EXPECTED") <(printf '%s\n' "$ACTUAL") >&2 || true
  exit 1
fi
