---
type: Runbook
title: observability — platform SLIs / SLOs
description: The service-level indicators the observability stack exposes (availability, latency, error-rate), the span metrics performance alerts are built from (exact metric names and attribute keys), the objectives tracked against them, and where the ClickHouse/HyperDX queries, dashboards and breach alerts live (ops/slo-alert/).
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

## Span metrics (the inputs for performance alerts)

Performance (SLO) alerts are built from metrics, not from scans of `otel_traces`: the node-local
collector (`charts/otel-collector-daemonset` ≥ 0.1.15) runs the `span_metrics` connector over a
fixed set of spans and writes a call counter and a duration histogram to ClickHouse next to every
other metric. An alert-provider `Metric` CR references them by the names below. The pipeline itself
is described in [configuration](./configuration.md#chartsotel-collector-daemonset--node-level-collector).

**Spans in scope, and nothing else:**

| ServiceName | Which spans | SLI |
|-------------|-------------|-----|
| `snowplow` | SpanKind `SERVER` | API |
| `krateo-frontend` | instrumentation ScopeName `snowplow/rum-ingest` | UI (`krateo.page.load`, `krateo.composition.open`, `krateo.form.submit`, `krateo.page.publish`, `krateo.widget.fetch`) |

Spans whose `http.route` is `/refreshes` (the SSE stream) or `/telemetry/v1/traces` (the RUM
ingest itself) are **dropped** before the connector (X5, acmp). They are excluded here rather than
in the `Metric` CR because alert-provider labels match `Attributes` by exact value only, so a CR
cannot say "route is not x". They are still in `otel_traces`.

**Metrics, exactly as they land:**

| MetricName | Table | Type | Unit (`MetricUnit`) |
|------------|-------|------|---------------------|
| `traces.span.metrics.calls` | `otel_metrics_sum` | monotonic sum, `Value` = spans | empty |
| `traces.span.metrics.duration` | `otel_metrics_histogram` | explicit-bucket histogram over span duration | `ms` |

- **`ServiceName`** column: `snowplow` or `krateo-frontend`.
- **`ResourceAttributes`**: `{'service.name': …}` and nothing else. The pipeline drops every other
  resource attribute (pod name, SDK details, browser user agent) so restarts and pods do not
  multiply rows.
- **`Attributes`** (all values are strings, ClickHouse `Map(LowCardinality(String), String)`):

  | Key | Values | Present on |
  |-----|--------|------------|
  | `service.name` | `snowplow`, `krateo-frontend` | every row but the overflow row |
  | `span.name` | e.g. `GET /call`, `krateo.page.load` | every row but the overflow row |
  | `status.code` | `STATUS_CODE_UNSET`, `STATUS_CODE_OK`, `STATUS_CODE_ERROR` | every row but the overflow row |
  | `krateo.sli.error` | `true`, `false` | every row but the overflow row |
  | `http.route` | a route template, e.g. `/call` (snowplow's own; on RUM spans, a snowplow route or the portal's) | spans that carry it |
  | `http.response.status_code` | the raw code, e.g. `200`, `404`, `500` | spans that carry it |
  | `otel.metric.overflow` | `true` | only the overflow row, if the cardinality cap is ever hit; it carries no other key |

  A key the span did not carry is **absent** from the map, not empty-valued: ClickHouse returns `''`
  for `Attributes['http.route']` either way, so filter with `mapContains` when the difference
  matters. `span.kind` and `collector.instance.id`, which the connector adds by default, are
  turned off (see configuration).
- **`ScopeName`**: `spanmetricsconnector`.
- **`krateo.sli.error` is the SLI's error flag; 4xx is not an error (D3).** It is set before the
  connector:
  - `true` when `http.response.status_code >= 500`;
  - `true` when the span has **no** `http.response.status_code` and its status is `ERROR` (no
    response at all: abort, network failure, CORS);
  - `false` otherwise, 4xx included.

  Use it, not `status.code`, for availability: the browser's fetch instrumentation sets status
  `ERROR` at `>= 400`, so `status.code` would count a RUM 404 as an error. Only
  `http.response.status_code` is read; every source in scope emits that key (as an int), and none
  emits the old `http.status_code`. `http.response.status_code` keeps the raw code.
- **Temporality: DELTA** (`AggregationTemporality = 1`). Each row is the count for its own
  interval (`StartTimeUnix` → `TimeUnix`, flushed every 60 s); an interval with no spans writes no
  row. A window's total is a plain `sum` over rows, across nodes and collector restarts, with no
  per-series reset handling: `sum(Value)` for calls, `sumForEach(BucketCounts)` and `sum(Count)`
  for the histogram. HyperDX 2.35.0, which evaluates the alert-provider's tiles, reads
  `AggregationTemporality = 1` rows as already-per-interval for both sums and histograms.
- **Histogram buckets** (`ExplicitBounds`, ms): `[25, 50, 100, 250, 500, 750, 1000, 1500, 2500,
  5000, 10000]`, so `BucketCounts` has 12 entries; the last is `> 10000`. Bucket *i* counts
  durations `≤ ExplicitBounds[i]` and `> ExplicitBounds[i-1]`. These resolve a p95 anywhere from
  50 ms to 10 s and are not tuned to any target; the targets are deploy-time configuration.
- **Retention:** the same 7-day TTL as every `otel_metrics_*` table.
- **Where the spans come from.** snowplow exports its own spans to the node-local agent
  (`OTEL_EXPORTER_OTLP_ENDPOINT=http://$(HOST_IP):4318`, the agent's hostPort) and forwards the
  browser's RUM spans to the same endpoint after rebuilding them from an allowlist (five span
  names; `http.route`, `http.request.method`, `http.response.status_code`; resource reduced to
  `service.name=krateo-frontend`; scope `snowplow/rum-ingest`).
- **UI caveat: today the `krateo-frontend` rows are absent, and when RUM is on only one UI SLI has
  data.**
  - The frontend's RUM is **off by default**. It starts only when the frontend's
    `OTEL_COLLECTOR_URL` is set (to `<SNOWPLOW_API_BASE_URL>/telemetry/v1/traces`): the frontend
    chart ships it empty, the installer sets no value, and `ui/src/index.tsx` gates on that key
    alone (`OTEL_TRACES_ENDPOINT` by itself does not start tracing).
  - When on, the frontend emits only **`krateo.widget.fetch`** (kind `CLIENT`). Nothing emits
    `krateo.page.load`, `krateo.composition.open`, `krateo.form.submit` or
    `krateo.page.publish` yet (`ui/src/otel/spanContract.ts`: "NOTHING EMITS THIS YET"). Alerts
    on those four names have no data until the frontend ships them.

### Cardinality

Rows per flush (60 s) per node agent, per metric:

- **snowplow:** one row per (`span.name`, `http.route`, `http.response.status_code`,
  `krateo.sli.error`). `span.name` is `<METHOD> <route>`, so it does not multiply `http.route`.
  snowplow registers about 40 method+route patterns; a request no pattern serves has no route and
  is named `<METHOD>`. With the ~15 status codes an HTTP API realistically returns, that is about
  (40 + 7) × 15 ≈ 700. `status.code` and `krateo.sli.error` both follow the code (`ERROR` and
  `true` exactly for 5xx and panics), so in practice neither multiplies. The **loose bound**
  counts `krateo.sli.error` as an independent ×2: **≈ 1,400**.
- **krateo-frontend:** 5 span names × `http.route` × `http.response.status_code` ×
  `status.code` × `krateo.sli.error` (×2). On `krateo.widget.fetch` the route must be one snowplow serves (checked against
  its live mux). On the four page-level spans it is the portal router's pattern, which snowplow
  can only shape-check, so it is client-supplied.
- **Hard cap:** `aggregation_cardinality_limit: 2000` per service per agent per flush, above
  snowplow's loose bound. Under DELTA the connector's state is cleared at every flush, so the cap
  holds per interval: at most 2001 rows (the extra one is `otel.metric.overflow=true`) per
  service per metric per agent per minute, whatever a client sends. Quiet intervals write
  nothing.

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
