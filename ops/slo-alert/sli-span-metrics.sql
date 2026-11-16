-- ---------------------------------------------------------------------------------------------
-- THE SLI definitions: the single source for every reader (the cmp-slo-* alerts, the HyperDX
-- "CMP performance SLOs (X5)" dashboard, the portal's SLOs tab, docs/SLO.md's performance rows).
-- A reader that needs a different number changes THIS file, not its own copy.
--
-- Inputs: the span metrics written by otel-collector-daemonset (>= 0.1.15) -- see docs/SLO.md
-- "Span metrics" for which spans are in scope and what is dropped before the connector
-- (/refreshes, /telemetry/v1/traces, abandoned interactions). Both metrics are DELTA
-- (AggregationTemporality 1): a window's total is a plain sum, no max-min.
--   otel_metrics_sum        traces.span.metrics.calls      Value = calls in the interval
--   otel_metrics_histogram  traces.span.metrics.duration   BucketCounts over ExplicitBounds (ms)
-- SLI classes: ServiceName 'snowplow' = API (SERVER spans), 'krateo-frontend' = UI (RUM spans,
-- by Attributes['span.name']). An SLI error is Attributes['krateo.sli.error'] = 'true': a 5xx or
-- no response, never a 4xx.
--
-- Parameters (ClickHouse query parameters; clickhouse-client: --param_start=... --param_end=...):
--   {start:DateTime64(9)}  {end:DateTime64(9)}   the window, [start, end)
-- The window is [start, end): `end` is EXCLUSIVE. Delta points are stamped with the flush time
-- (TimeUnix), so a request at 14:26:29 lands in a 14:26:43 point. To include a period's last
-- flush, set `end` strictly AFTER that flush's TimeUnix (e.g. the next whole minute), never at it.
--
-- Percentiles: the bucket that holds rank q*n, reported as the interval (lower, upper] in ms.
-- Buckets are upper-inclusive (`le`): a rank equal to the cumulative count at bound b is IN the
-- bucket ending at b, not the next one. No interpolation, so the result is exact about what the
-- histogram can say and nothing more.
-- Bucket LAYOUT: never sum histograms with different ExplicitBounds. The layout changed on
-- 2026-11-11 (0.1.26 added 2000 ms). Every percentile query groups by bounds; a window that spans
-- a layout change returns one row per layout, and a reader must then report NO percentile for that
-- window (as krateo-slo-rollup does) rather than pick one.
--
-- Counts are of the spans the services export. snowplow exported every SERVER span during X5
-- (its step-4 calls equalled the load generator's request count exactly).
-- ---------------------------------------------------------------------------------------------

-- === SLI 1: Availability, one figure per SLI class ============================================
SELECT
    ServiceName                                                   AS sli_service,
    sum(Value)                                                    AS calls,
    sumIf(Value, Attributes['krateo.sli.error'] = 'true')         AS sli_errors,
    if(calls = 0, NULL, 1 - sli_errors / calls)                   AS availability
FROM otel_metrics_sum
WHERE MetricName = 'traces.span.metrics.calls'
  AND ServiceName IN ('snowplow', 'krateo-frontend')
  AND TimeUnix >= {start:DateTime64(9)} AND TimeUnix < {end:DateTime64(9)}
GROUP BY ServiceName
ORDER BY ServiceName;

-- === SLI 2: API latency p50 / p95 (snowplow, all SERVER spans in scope) =======================
-- Target: p95 <= 500 ms.
WITH
    agg AS (
        SELECT ExplicitBounds AS b, sumForEach(BucketCounts) AS bc
        FROM otel_metrics_histogram
        WHERE MetricName = 'traces.span.metrics.duration'
          AND ServiceName = 'snowplow'
          AND TimeUnix >= {start:DateTime64(9)} AND TimeUnix < {end:DateTime64(9)}
        GROUP BY ExplicitBounds
    )
SELECT
    b                                                                          AS bounds_ms,
    arraySum(bc)                                                               AS n,
    arrayFirstIndex(x -> x >= 0.50 * n, arrayCumSum(bc))                       AS i50,
    arrayFirstIndex(x -> x >= 0.95 * n, arrayCumSum(bc))                       AS i95,
    if(i50 <= 1, 0, b[i50 - 1])                                                AS p50_lower_ms,
    if(i50 > length(b), inf, b[i50])                                           AS p50_upper_ms,
    if(i95 <= 1, 0, b[i95 - 1])                                                AS p95_lower_ms,
    if(i95 > length(b), inf, b[i95])                                           AS p95_upper_ms,
    p95_upper_ms <= 500                                                        AS p95_within_target
FROM agg
WHERE n > 0;

-- === SLI 3: UI latency p50 / p95, per interaction =============================================
-- Targets: krateo.page.load p95 <= 2000 ms; composition.open, form.submit, page.publish are
-- tracked against the same 2000 ms UI objective. krateo.widget.fetch is NOT a latency SLI: see 4.
WITH
    agg AS (
        SELECT Attributes['span.name'] AS interaction, ExplicitBounds AS b, sumForEach(BucketCounts) AS bc
        FROM otel_metrics_histogram
        WHERE MetricName = 'traces.span.metrics.duration'
          AND ServiceName = 'krateo-frontend'
          AND Attributes['span.name'] IN ('krateo.page.load', 'krateo.composition.open',
                                          'krateo.form.submit', 'krateo.page.publish')
          AND TimeUnix >= {start:DateTime64(9)} AND TimeUnix < {end:DateTime64(9)}
        GROUP BY interaction, ExplicitBounds
    )
SELECT
    interaction,
    b                                                                          AS bounds_ms,
    arraySum(bc)                                                               AS n,
    arrayFirstIndex(x -> x >= 0.50 * n, arrayCumSum(bc))                       AS i50,
    arrayFirstIndex(x -> x >= 0.95 * n, arrayCumSum(bc))                       AS i95,
    if(i50 <= 1, 0, b[i50 - 1])                                                AS p50_lower_ms,
    if(i50 > length(b), inf, b[i50])                                           AS p50_upper_ms,
    if(i95 <= 1, 0, b[i95 - 1])                                                AS p95_lower_ms,
    if(i95 > length(b), inf, b[i95])                                           AS p95_upper_ms,
    p95_upper_ms <= 2000                                                       AS p95_within_target
FROM agg
WHERE n > 0
ORDER BY interaction;

-- === SLI 4: Widget fetch failure share (UI) ===================================================
SELECT
    sum(Value)                                                    AS fetches,
    sumIf(Value, Attributes['krateo.sli.error'] = 'true')         AS failed,
    if(fetches = 0, NULL, failed / fetches)                       AS failure_share
FROM otel_metrics_sum
WHERE MetricName = 'traces.span.metrics.calls'
  AND ServiceName = 'krateo-frontend'
  AND Attributes['span.name'] = 'krateo.widget.fetch'
  AND TimeUnix >= {start:DateTime64(9)} AND TimeUnix < {end:DateTime64(9)};

-- === Error budget burn rate, per SLI class (availability objective 99.9 %) ====================
-- burn = (sli_errors / calls) / (1 - 0.999) over the window; > 14.4 over 1 h is a fast burn.
-- Budget over 30 days needs krateo_slo_daily (charts/krateo-slo-rollup): every otel_* table
-- keeps 7 days. budget = (1 - 0.999) * calls_30d; remaining = 1 - errors_30d / budget.
SELECT
    ServiceName                                                   AS sli_service,
    sum(Value)                                                    AS calls,
    sumIf(Value, Attributes['krateo.sli.error'] = 'true')         AS sli_errors,
    if(calls = 0, NULL, (sli_errors / calls) / (1 - 0.999))       AS burn_rate
FROM otel_metrics_sum
WHERE MetricName = 'traces.span.metrics.calls'
  AND ServiceName IN ('snowplow', 'krateo-frontend')
  AND TimeUnix >= {start:DateTime64(9)} AND TimeUnix < {end:DateTime64(9)}
GROUP BY ServiceName
ORDER BY ServiceName;

-- =============================================================================================
-- 30-day statements, over krateo_slo_daily (charts/krateo-slo-rollup). Every otel_* table keeps
-- 7 days, so a 30-day objective can only be read here. The rollup holds one row per UTC day and
-- (service, span_name, route, sli_error, bounds): a call count when bounds is empty, a summed
-- histogram when it is not (its calls is 0). Read it with FINAL: a refresh writes each recent day
-- again with a newer computed_at, and FINAL keeps only the newest row per key. Every WHERE below
-- is on key columns, which all versions of a row share, so filtering cannot pick an older one.
-- The window is whole UTC days: day >= toDate({start}, 'UTC') AND day < toDate({end}, 'UTC'),
-- the UTC dates of the same instants the statements above read. Same parameters as above, so a
-- reader passes one range to every statement. Midnight bounds are exact; otherwise start's day
-- is in whole and end's day is out whole (start = now - 30 days, end = now: the 30 days before
-- today, today being incomplete).
-- The same percentile and layout rules apply: never sum rows with different bounds.
-- =============================================================================================

-- === 30d-1: Availability and error budget, per SLI class (objective 99.9 %) =================
-- budget = (1 - 0.999) * calls; budget_remaining = 1 - sli_errors / budget (negative = overspent).
-- d.calls / d.sli_error name the columns: unqualified, `calls` is the alias below.
SELECT
    d.service                                                     AS sli_service,
    sum(d.calls)                                                  AS calls,
    sumIf(d.calls, d.sli_error)                                   AS sli_errors,
    if(calls = 0, NULL, 1 - sli_errors / calls)                   AS availability,
    if(calls = 0, NULL, (1 - 0.999) * calls)                      AS error_budget_calls,
    if(calls = 0, NULL, 1 - sli_errors / ((1 - 0.999) * calls))   AS budget_remaining,
    min(d.day)                                                    AS first_day,
    max(d.day)                                                    AS last_day,
    uniqExact(d.day)                                              AS days_with_data
FROM krateo_slo_daily AS d FINAL
WHERE empty(d.bounds)
  AND d.service IN ('snowplow', 'krateo-frontend')
  AND d.day >= toDate({start:DateTime64(9)}, 'UTC') AND d.day < toDate({end:DateTime64(9)}, 'UTC')
GROUP BY d.service
ORDER BY d.service;

-- === 30d-2: API latency p50 / p95 over whole days (snowplow), one row per layout ============
WITH
    agg AS (
        SELECT bounds AS b, sumForEach(bucket_counts) AS bc
        FROM krateo_slo_daily FINAL
        WHERE notEmpty(bounds)
          AND service = 'snowplow'
          AND day >= toDate({start:DateTime64(9)}, 'UTC') AND day < toDate({end:DateTime64(9)}, 'UTC')
        GROUP BY bounds
    )
SELECT
    b                                                                          AS bounds_ms,
    arraySum(bc)                                                               AS n,
    arrayFirstIndex(x -> x >= 0.50 * n, arrayCumSum(bc))                       AS i50,
    arrayFirstIndex(x -> x >= 0.95 * n, arrayCumSum(bc))                       AS i95,
    if(i50 <= 1, 0, b[i50 - 1])                                                AS p50_lower_ms,
    if(i50 > length(b), inf, b[i50])                                           AS p50_upper_ms,
    if(i95 <= 1, 0, b[i95 - 1])                                                AS p95_lower_ms,
    if(i95 > length(b), inf, b[i95])                                           AS p95_upper_ms,
    p95_upper_ms <= 500                                                        AS p95_within_target
FROM agg
WHERE n > 0;

-- === 30d-3: UI latency p50 / p95 over whole days, per interaction, one row per layout =======
WITH
    agg AS (
        SELECT span_name AS interaction, bounds AS b, sumForEach(bucket_counts) AS bc
        FROM krateo_slo_daily FINAL
        WHERE notEmpty(bounds)
          AND service = 'krateo-frontend'
          AND span_name IN ('krateo.page.load', 'krateo.composition.open',
                            'krateo.form.submit', 'krateo.page.publish')
          AND day >= toDate({start:DateTime64(9)}, 'UTC') AND day < toDate({end:DateTime64(9)}, 'UTC')
        GROUP BY interaction, bounds
    )
SELECT
    interaction,
    b                                                                          AS bounds_ms,
    arraySum(bc)                                                               AS n,
    arrayFirstIndex(x -> x >= 0.50 * n, arrayCumSum(bc))                       AS i50,
    arrayFirstIndex(x -> x >= 0.95 * n, arrayCumSum(bc))                       AS i95,
    if(i50 <= 1, 0, b[i50 - 1])                                                AS p50_lower_ms,
    if(i50 > length(b), inf, b[i50])                                           AS p50_upper_ms,
    if(i95 <= 1, 0, b[i95 - 1])                                                AS p95_lower_ms,
    if(i95 > length(b), inf, b[i95])                                           AS p95_upper_ms,
    p95_upper_ms <= 2000                                                       AS p95_within_target
FROM agg
WHERE n > 0
ORDER BY interaction;
