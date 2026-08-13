# SLI / SLO — dashboards and alerts

Exposes the three golden SLIs — **availability**, **latency**, **error-rate** —
for the platform services, computed from the telemetry already flowing into
ClickHouse (`otel_traces`, `otel_logs`), and tracks them against configurable
SLO targets. No new collection: the OTel gateway already receives traces from
the instrumented services; this directory only adds the queries, the HyperDX
dashboard and the SLO burn alerts on top.

## SLI definitions

| SLI | Definition | Source |
|-----|------------|--------|
| **Availability** | share of server spans with `StatusCode != 'Error'` over the window | `otel_traces` |
| **Latency** | p50/p95/p99 of server span `Duration`, per service | `otel_traces` |
| **Error-rate** | `ERROR`/`FATAL` log lines per service over the window | `otel_logs` |

The full SQL lives in [`sli-queries.sql`](sli-queries.sql) — each query is
copy-pasteable into a HyperDX chart or the ClickHouse client.

## SLO targets

Targets are **configuration, not code** — set them in `.env` (defaults below):

| Variable | Default | Meaning |
|----------|---------|---------|
| `SLO_AVAILABILITY_TARGET` | `99.9` | availability %, per service, 30-day window |
| `SLO_API_LATENCY_P95_MS` | `500` | API request p95 latency (ms) |
| `SLO_UI_LATENCY_P95_MS` | `2000` | UI page-interaction p95 latency (ms) |
| `SLO_ERROR_RATE_MAX` | `10` | max ERROR/FATAL log lines per 5m per namespace |

## Files

| File | Purpose |
|------|---------|
| `sli-queries.sql` | The SLI queries (availability, latency percentiles, error-rate, burn rate). |
| `bootstrap-slo-alerts.sh` | Creates the HyperDX saved searches + SLO breach alerts via API. |
| `.env.example` | Configuration template (targets + HyperDX access). |
| [`../../docs/SLO.md`](../../docs/SLO.md) | SLI/SLO reference doc + perf-results capture template. |

## Setup

1. Create the dashboard: in HyperDX add one chart per query from
   `sli-queries.sql` (Availability by service, p95 latency by service,
   Error-rate by namespace, SLO burn). Save as **"Platform SLOs"**.
2. Create the breach alerts:

   ```sh
   cp .env.example .env   # fill in values
   ./bootstrap-slo-alerts.sh
   ```

Alerts route through the same channels as every other alert (portal webhook /
alert proxy / email), so an SLO breach shows up in-portal like any incident.
