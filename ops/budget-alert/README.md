# Budget Alerts — threshold rules on ShowbackRecords

A **budget is a threshold on rated showback data** (per Org / Tenant / Service /
Tag). No separate budget engine: breach detection **reuses the existing alert
pipeline** (OTel → ClickHouse → HyperDX), delivery is **in-portal + email**
(optional webhook / Autopilot).

## How it works

```
budgets (ClickHouse table)          ← budget definitions (scope + amount + warn ratio)
        │
        ▼
budget_status (ClickHouse view)     ← current-period spend vs. amount, per budget
        │                              (joins showback_daily / showback_daily_by_tag
        │                               produced by the showback engine)
        ▼
budget-evaluator (CronJob)          ← every 15m, logs one JSON line per budget
        │                              in warning/breached state (stdout)
        ▼
OTel DaemonSet → otel_logs          ← standard log collection, nothing new
        ▼
HyperDX saved search + alert        ← same mechanism as every other alert
        ▼
in-portal (webhook → alert proxy → portal notifications) + email channel
```

This is exactly the **heartbeat-canary pattern** already used for pipeline
self-monitoring: a CronJob emits structured stdout logs, the pipeline picks
them up, HyperDX alerts on them.

## Components

| File | Purpose |
|------|---------|
| `ddl/001_budgets.sql` | `budgets` table — budget definitions (scope, period, amount, warn ratio). |
| `ddl/002_budget_status.sql` | `budget_status` view — spend-to-date vs. amount, status `ok`/`warning`/`breached`. |
| `budget-evaluator-cronjob.yaml` | CronJob logging warning/breached budgets as OTel-shaped JSON lines. |
| `bootstrap-budget-alerts.sh` | Creates the HyperDX saved searches + alerts (warning + breach) via API. Exits non-zero with a FAILED summary if any step does not complete. |
| `tests/smoke-budget-ddl.sh` | Executes the DDL against clickhouse-local (docker fallback) with sample data and asserts the `budget_status` view materializes the expected statuses. |
| `.env.example` | Configuration template for the bootstrap script. |

## Budget scoping

A budget row selects a scope with empty-string wildcards:

| Column | `''` means |
|--------|------------|
| `tenant` | whole Org |
| `service` | all services |
| `tag_key` | not tag-scoped (uses `showback_daily`) |
| `tag_value` | any value of `tag_key` (uses `showback_daily_by_tag`) |

`period` is `monthly` (spend since start of current month) or `daily`.
`warn_ratio` (default `0.8`) drives the early-warning alert before the hard
breach at `1.0`.

> Budget **definitions** (actual amounts, scopes) are configuration and are
> seeded by the consuming assembly — this repo ships only the mechanism with
> no budget rows.

## Setup

1. Apply the DDL to the ClickHouse database that hosts the showback tables,
   rendering `{{database}}` with the target database (same convention as the
   showback engine DDL; its default database is `showback`):

   ```sh
   sed 's/{{database}}/showback/g' ddl/001_budgets.sql | clickhouse-client --multiquery
   sed 's/{{database}}/showback/g' ddl/002_budget_status.sql | clickhouse-client --multiquery
   ```

2. Deploy the evaluator. `CLICKHOUSE_DATABASE` in the CronJob (default
   `showback`) **must match the database the DDL was rendered with**,
   otherwise the `budget_status` view is not found and the job fails:

   ```sh
   kubectl apply -f budget-evaluator-cronjob.yaml
   ```

3. Create the HyperDX alerts (webhooks for the in-portal channel and the
   email channel must exist in HyperDX first). The script exits non-zero
   with a `FAILED` summary when any saved search or alert cannot be
   created, so a partial bootstrap never looks like success:

   ```sh
   cp .env.example .env   # fill in values
   ./bootstrap-budget-alerts.sh
   ```

## Verifying the DDL locally

`tests/smoke-budget-ddl.sh` renders `{{database}}`, executes DDL 001+002
against a throwaway ClickHouse (clickhouse-local, or the
`clickhouse/clickhouse-server:24.8-alpine` image via docker when no local
binary exists), inserts sample budgets + spend, and asserts the
`budget_status` view returns the expected `ok`/`warning`/`breached` rows —
including ReplacingMergeTree FINAL supersede semantics, tag-scoped budgets,
daily/monthly period windows and the `enabled` flag:

```sh
tests/smoke-budget-ddl.sh
```

## Log-line shape (D19a)

The evaluator emits one JSON line per warning/breached budget following the
OTel Logs Data Model, so a collector json parser can map it 1:1 onto an
OTLP LogRecord while HyperDX keeps querying it as JSON in `Body`:

```json
{
  "timestamp": "2026-01-01T12:00:00Z",
  "trace_id": "<32 hex — one per evaluator run>",
  "span_id": "<16 hex — one per line>",
  "severity_text": "WARN | ERROR",
  "severity_number": 13,
  "body": "budget threshold crossed",
  "attributes": {
    "event.name": "krateo.budget.threshold",
    "service.name": "krateo-budget-evaluator",
    "krateo.budget.id": "...", "krateo.budget.org": "...",
    "krateo.budget.status": "warning | breached", "...": "..."
  }
}
```

Severity maps `warning`→`WARN`/13 and `breached`→`ERROR`/17. The evaluator
is a **scheduled origin**: there is no inbound trace context or baggage to
propagate (nothing calls it), so each run mints a fresh `trace_id` shared by
all lines of that run — the alerts of one evaluation are correlatable — and
a per-line `span_id`. The line is built entirely inside ClickHouse
(`toJSONString` escapes all user-supplied values); the shell never parses
row data.

## Delivery channels

- **In-portal**: the alert webhook targets the autopilot-alert-proxy, which
  forwards to the portal notification endpoint (same route as every other
  alert shown in the portal).
- **Email**: a second alert channel pointing at an email-integration webhook
  (HyperDX email integration or an SMTP relay webhook).
- **Optional**: point `BUDGET_ALERT_PROXY_WEBHOOK_ID` at the agent-routed
  webhook to let Autopilot react to breaches.
