# SLI / SLO — dashboards and alerts

Exposes the three golden SLIs — **availability**, **latency**, **error-rate** —
for the platform services, computed from telemetry already flowing into
ClickHouse, and tracks them against configurable SLO targets. No new
collection: this directory only holds the SLI queries and the bootstrap of
the HyperDX breach alerts.

## SLI definitions

Each SLI has exactly one definition. A reader that needs a different number
(an alert, a dashboard tile, the portal, a performance row in
[`docs/SLO.md`](../../docs/SLO.md)) changes the file below, not its own copy.

| SLI | Definition | Source | File |
|-----|------------|--------|------|
| **Availability** | `1 - sli_errors / calls` per SLI class (`snowplow` = API, `krateo-frontend` = UI); an error is `krateo.sli.error = 'true'` (5xx or no response, never 4xx) | span metrics: `traces.span.metrics.calls` in `otel_metrics_sum` | [`sli-span-metrics.sql`](sli-span-metrics.sql) |
| **Latency** | p50 / p95 bucket of `traces.span.metrics.duration`: API over all snowplow SERVER spans, UI per interaction (`krateo.page.load`, `.composition.open`, `.form.submit`, `.page.publish`) | span metrics: `otel_metrics_histogram` | [`sli-span-metrics.sql`](sli-span-metrics.sql) |
| **Widget-fetch failure share** | failed / all `krateo.widget.fetch` calls (not a latency SLI) | span metrics: `otel_metrics_sum` | [`sli-span-metrics.sql`](sli-span-metrics.sql) |
| **Error-budget burn rate** | `(sli_errors / calls) / (1 - 0.999)` per SLI class | span metrics: `otel_metrics_sum` | [`sli-span-metrics.sql`](sli-span-metrics.sql) |
| **Error-rate** | `ERROR`/`FATAL` log lines per namespace, 5-minute buckets | `otel_logs` | [`sli-queries.sql`](sli-queries.sql), SLI 3 |

`sli-span-metrics.sql` takes the window as ClickHouse query parameters,
`[start, end)`:

```sh
clickhouse-client --param_start='2026-11-10 15:12:00' --param_end='2026-11-10 15:41:00' \
  --queries-file ops/slo-alert/sli-span-metrics.sql
```

A percentile is the bucket that holds rank `q*n`, reported as `(lower, upper]`
ms with no interpolation; buckets are upper-inclusive. Histograms with
different `ExplicitBounds` are never summed: a window spanning the 2026-11-11
layout change returns one row per layout and reports no percentile. The rules,
and why a window must reach past the first collector flush after the period,
are in the file's header and in [`docs/SLO.md`](../../docs/SLO.md#slis).
`tests/sli-span-metrics-docker.py` runs every statement against fixture rows
and the committed X5 rows.

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
| `sli-span-metrics.sql` | THE SLI definitions from span metrics: availability, API/UI latency p50/p95, widget-fetch failure share, burn rate over `otel_metrics_*` (7 days); then the 30-day statements over `krateo_slo_daily` ([krateo-slo-rollup](../../charts/krateo-slo-rollup/README.md)), whole UTC days read with `FINAL`: **30d-1** availability and error budget per SLI class, **30d-2** API p50/p95, **30d-3** UI p50/p95 per interaction. |
| `sli-queries.sql` | SLI 3, error-rate over `otel_logs` (no span-metrics equivalent). |
| `bootstrap-slo-alerts.sh` | Creates HyperDX saved searches + breach alerts via API. |
| `.env.example` | Configuration template (targets + HyperDX access). |
| [`../../docs/SLO.md`](../../docs/SLO.md) | SLI/SLO reference doc + perf-results capture template. |

## Dashboards and alerts

- **HyperDX "CMP performance SLOs (X5)"** (12 tiles) exists on krateo-057,
  created through the HyperDX API; it is not shipped by a chart yet. A
  reconciled, chart-shipped form is being proposed.
- **Portal SLOs tab** on `/observability` is being designed by frontend.
- **SLO alerts**: the alert-provider Alerts `cmp-slo-*`, over the Metrics
  `slo-span-calls` and `slo-span-duration`, read the same span metrics.
  krateo-observability ships them from 0.1.52 (`defaultAlerts`, `defaultMetrics`;
  [alerts.md](../../docs/alerts.md#slo-objects-0152)).

All of them must read `sli-span-metrics.sql` (and `sli-queries.sql` SLI 3 for
error-rate), not their own queries.

`bootstrap-slo-alerts.sh` creates three HyperDX saved-search alerts: a count
of server spans in error, a count of server spans slower than
`SLO_API_LATENCY_P95_MS`, and the error-rate count of SLI 3. The first two are
event-count breach signals over `otel_traces`, not SLI definitions: the
availability, latency and burn-rate figures are the ones in
`sli-span-metrics.sql`.

```sh
cp .env.example .env   # fill in values
./bootstrap-slo-alerts.sh
```

Alerts route through the same channels as every other alert (portal webhook /
alert proxy / email), so an SLO breach shows up in-portal like any incident.
