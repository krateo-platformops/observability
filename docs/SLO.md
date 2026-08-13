---
type: Runbook
title: observability — platform SLIs / SLOs
description: The service-level indicators the observability stack exposes (availability, latency, error-rate), the objectives tracked against them, and where the ClickHouse/HyperDX queries, dashboards and breach alerts live (ops/slo-alert/).
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [slo, sli, observability, alerting, hyperdx, clickhouse]
timestamp: 2026-08-13T00:00:00Z
---

# Platform SLIs / SLOs

Reference for the service-level indicators exposed by the observability stack
and the objectives tracked against them. The queries, dashboard layout and
breach alerts live in [`ops/slo-alert/`](../ops/slo-alert/).

## SLIs

All three SLIs are computed from telemetry already collected by the stack —
no additional instrumentation is required for services that emit OTLP traces
and structured logs.

| SLI | Definition | Source table |
|-----|------------|--------------|
| **Availability** | ratio of server spans with `StatusCode != 'Error'` | `otel_traces` |
| **Latency** | p50 / p95 / p99 of server-span `Duration`, per service | `otel_traces` |
| **Error-rate** | `ERROR`/`FATAL` structured log lines per service/namespace | `otel_logs` |

## SLOs (default targets)

Targets are deployment configuration (`ops/slo-alert/.env`), not code.
Defaults:

| Objective | Target | Window |
|-----------|--------|--------|
| Availability | ≥ 99.9 % | 30-day rolling |
| API request latency | p95 ≤ 500 ms | 5-minute buckets |
| UI interaction latency | p95 ≤ 2 s | 5-minute buckets |
| Error-rate | ≤ 10 ERROR/FATAL lines / 5 m / namespace | 5-minute buckets |

**Error budget:** at 99.9 % availability the monthly budget is ~43 minutes of
failed requests. The burn-rate query in `sli-queries.sql` reports consumption;
a burn rate above ~14× on the 1-hour window is a fast-burn (page-worthy)
signal.

## Dashboards

The **"Platform SLOs"** HyperDX dashboard is built from the queries in
[`ops/slo-alert/sli-queries.sql`](../ops/slo-alert/sli-queries.sql):

1. Availability by service (5m) + 30-day availability vs. target
2. Latency p50/p95/p99 by service + share of requests within target
3. Error-rate by namespace
4. Error-budget burn rate by service

Breach alerts are bootstrapped by
[`ops/slo-alert/bootstrap-slo-alerts.sh`](../ops/slo-alert/bootstrap-slo-alerts.sh)
and route through the standard alert channels (portal / alert-proxy / email).

## Performance results

Performance runs are captured here so the tracked SLOs stay tied to measured
baselines. Record one row per run:

| Date | Scenario | Load | Availability | API p95 (ms) | UI p95 (ms) | Notes |
|------|----------|------|--------------|--------------|-------------|-------|
| _(pending)_ | e2e full-loop (`demo/tests`) | — | — | — | — | first baseline run to be recorded |

How to capture a run:

1. Execute the load/e2e scenario (see `demo/tests/`).
2. While the run window is active, execute the queries in
   `ops/slo-alert/sli-queries.sql` scoped to the run's time range.
3. Append the resulting numbers to the table above and link the raw output.
