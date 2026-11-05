# krateo-slo-rollup

A daily rollup of the SLI span metrics, so a rolling 28-day SLO can be computed.

Every `otel_*` table keeps 7 days, so nothing in ClickHouse can answer a 28-day question. This
chart keeps one small table, `krateo_slo_daily`, with per-day totals of
`traces.span.metrics.calls` and `traces.span.metrics.duration` (from
`otel-collector-daemonset` ≥ 0.1.15; see [SLO](../../docs/SLO.md)) and a retention of its own.

It adds **no new ingest path** and **nothing to the insert path** of the source tables.

## What it deploys

| object | purpose |
|---|---|
| `…-scripts` ConfigMap | the DDL and the apply logic |
| `…-apply` CronJob | every `apply.schedule`: creates the table and the view if missing, applies a changed TTL, and fails if the view has stopped refreshing |

There is **no Helm hook**, as in `krateo-audit-worm`: the CronJob owns the schema, so the objects
appear on the first run after install. Until the collector has created `otel_metrics_sum` and
`otel_metrics_histogram` the Job fails with a clear message and the next run retries. Source
tables with no rows are fine: the refresh succeeds and writes nothing, so the chart is safe to
install before any span metric exists.

## What the table holds

| column | meaning |
|---|---|
| `day` | UTC day of the row's `TimeUnix` (the end of its interval) |
| `service` | `ServiceName` |
| `span_name` | `Attributes['span.name']`; empty only on the connector's overflow row |
| `route` | `Attributes['http.route']`; empty when the span has none |
| `sli_error` | `Attributes['krateo.sli.error'] = 'true'`: 5xx or no response, never a 4xx |
| `bounds` | `ExplicitBounds`; empty on a call-count row |
| `calls` | that day's sum of `Value`; 0 on a histogram row |
| `bucket_counts` | that day's `BucketCounts`, summed element by element; empty on a call-count row |
| `computed_at` | when the refresh wrote this row |

The error count is `sum(calls)` over `sli_error` rows, not a column of its own: the flag is in the
key so that latency can also be read per outcome.

A key has two rows because the two metrics live in two tables: one for the count, one for the
histogram. `bounds` is in the key so that buckets are only ever summed on identical bounds: if the
bucket layout changes, the old and new layouts stay in separate rows.

Rows are DELTA (`AggregationTemporality = 1`): each is the count for its own interval, so a day is
their plain sum. Anything else in the tables is filtered out, including a cumulative row, which is
a running total and would inflate the sum. Should the connector ever move off DELTA the rollup
stops growing rather than going wrong, and the apply Job reports it as stale once no delta row is
left in the recompute window.

## Why a refreshable view, not an insert-triggered one

An ordinary materialized view runs inside the writer's `INSERT`. Measured on ClickHouse 26.8:

| when the view fails, or its target is gone | result |
|---|---|
| view's query throws, default settings | the writer's `INSERT` **fails**; the rows are already in the source, so the exporter's retry duplicates the whole batch |
| target table dropped, default settings | the writer's `INSERT` **fails** and the rows are **lost** |
| `materialized_views_ignore_errors = 1` in the view's own `SETTINGS` | **ignored**: the `INSERT` still fails |
| `materialized_views_ignore_errors = 1` in the writer's session | the `INSERT` succeeds, the view's rows are dropped |

So that setting only works in the writer's session, which this chart does not own:
`otel_metrics_sum` is written by both collectors for every metric on the platform, and the setting
would have to go on both exporters and be released before this chart, which could not check it. A
user-level setting is no better: `krateo_audit_worm_mv` is written by the same user and must fail
loudly. And what it buys is a rollup that goes silently wrong.

A **refreshable** materialized view runs on ClickHouse's own scheduler, as a separate query. No
insert waits for it, so a failure cannot reach the exporter: with the rollup table dropped, inserts
into the source tables carry on, and the next apply and refresh restore it. Each refresh recomputes
the last `rollup.recomputeDays` whole days from the sources. That also means:

- **Install backfills.** The first refresh picks up what the sources hold, up to 6 days.
- **It heals.** The refresh can stop for up to 5 days, and it recovers whole days when it restarts.
- **It lags.** The current day is up to `rollup.refreshEvery` behind. A 28-day SLO over complete
  days does not read the current day.
- **It costs one small query per refresh**, on the sources' primary key and daily partitions.

Exporter retries that duplicate rows in the sources are counted, in the raw tables and here alike.

## Why ReplacingMergeTree

Each refresh writes a day's **total**, not an increment, so the engine must keep the newest row
rather than add them: a SummingMergeTree or AggregatingMergeTree would count every refresh again.
`computed_at` is the version. It also keeps the buckets a plain `Array(UInt64)`, which neither
engine would sum without a Nested `…Map` column or `AggregateFunction` states.

The price is that **every read needs `FINAL`**, because the old rows live until a merge. A day that
disappears from the sources writes no rows, so a dropped source partition cannot wipe it here.

## Reading it

[`tests/rollup-query.sql`](tests/rollup-query.sql) is the worked query: error ratio and p95 per
route and overall, the same SQL the kind proof checks against hand-computed values. Sum
`bucket_counts` with `sumForEach` over `FINAL` rows, find the bucket holding the rank in the running
total, and interpolate linearly inside it. Scope the query to one service, and to whole days. It
gives no p95 when two bucket layouts are in scope.

The numbers are estimates, for two independent reasons:

- **Counts are of sampled spans.** snowplow head-samples its SERVER spans at about 5%
  (`traceidratio` 0.05, not parent-based, and errors are not preferentially kept), and span metrics
  are computed from the spans that survive. `calls` is about 5% of real traffic. Nothing in the
  rollup scales it. Ratios and quantiles are estimates from a random sample, so their error is
  wider on a route or a day with little traffic.
- **Quantiles are bucketed.** A p95 is only known to lie inside its bucket; interpolation assumes
  durations are spread evenly across it. A p95 past the last bound (10 s) reads as that bound.

## Retention

`rollup.ttlDays` (default 40: a rolling 28 days plus a margin) goes into the `CREATE TABLE`, so it
applies when the table is created. `CREATE … IF NOT EXISTS` leaves an existing table alone, so the
apply step compares the live TTL with the value and runs `ALTER TABLE … MODIFY TTL` when they
differ, and does nothing when they match.

**The shipped `otelcollector` user cannot run that ALTER**: its grants are `SHOW, SELECT, INSERT,
CREATE DATABASE, CREATE TABLE, CREATE VIEW, CREATE DICTIONARY ON default.*`
([disk-full runbook](../../docs/clickhouse-disk-full.md)). So changing the value on a live cluster
makes the Job **fail**, with the exact statement in its log, until someone with `ALTER TTL` on the
table runs it or that grant is added. The table keeps its old TTL in the meantime.

`rollup.refreshEvery`, `rollup.recomputeDays` and any change to the view's query apply at view
creation only. Changing them means dropping the view (not the table) with an ops principal; the
next apply recreates it. `rollup.recomputeDays` must stay below the sources' retention: a day that
is partly expired there would replace its complete total here.

## Detecting a stopped view

A failing refresh is not visible to the shipped user (`system.view_refreshes` needs its own grant,
and refresh errors stay there). So the apply Job fails with `stale:` when a source row older than
`apply.staleAfterSeconds` was written after the newest `computed_at`. A quiet cluster has no such
row, so it does not false-alarm.

## Removing it

`helm uninstall` removes the CronJob and leaves the table and the view, which keeps refreshing
into the table. To remove them: `DROP VIEW krateo_slo_daily_mv`, then `DROP TABLE
krateo_slo_daily`, with an ops principal (the shipped user cannot drop).

## Proof

`tests/e2e-kind.sh` (manual; refuses anything but a `kind-*` context) runs the chart's real Job on
kind, as a user with exactly the shipped grants, against the exporter's real table DDL. See the
script's header for the steps. It covers missing and empty sources; known rows against hand-computed
counts, error ratio and p95; re-running as a no-op; install over existing rows; a dropped rollup
table with the writer's insert still succeeding; a stopped view; a TTL change with and without the
grant; and four mutations of the SQL, each of which must fail. `tests/rollup_test.yaml` is the
`helm unittest` suite.

## Limits

- **Needs ClickHouse ≥ 24.10**, where refreshable views stopped being experimental. The platform
  runs 26.8.
- **Coverage starts at install**, less the backfill. A reader must check the first `day` before
  calling a window complete. Days older than the backfill stay absent.
- **The overflow row is kept**, with `span_name` and `route` empty and `sli_error` false: if the
  connector's cardinality cap is hit, those calls count as successes of no route.
- **Route cardinality is the connector's**: one row per day per surviving combination. Page-level
  RUM routes are client-supplied, so the cap in the collector is what bounds them.
