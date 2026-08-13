-- ---------------------------------------------------------------------------
-- SLI queries over the ClickStack tables (otel_traces / otel_logs).
-- Each statement is standalone: paste it into a HyperDX chart (SQL mode) or
-- run it with clickhouse-client. Time windows use HyperDX's chart range where
-- available; the standalone versions below default to the last 24h.
-- ---------------------------------------------------------------------------

-- === SLI 1: Availability (per service, 5m buckets) =========================
-- Share of server spans that did not end in error.
SELECT
    toStartOfFiveMinutes(Timestamp)                       AS bucket,
    ServiceName                                           AS service,
    countIf(StatusCode != 'Error') / count()              AS availability,
    count()                                               AS requests
FROM otel_traces
WHERE SpanKind = 'Server'
  AND Timestamp >= now() - INTERVAL 24 HOUR
GROUP BY bucket, service
ORDER BY bucket, service;

-- === SLI 1b: Availability vs. SLO (30-day rolling, per service) ============
-- Compare against SLO_AVAILABILITY_TARGET (e.g. 99.9).
SELECT
    ServiceName                                              AS service,
    round(100 * countIf(StatusCode != 'Error') / count(), 3) AS availability_pct,
    count()                                                  AS requests
FROM otel_traces
WHERE SpanKind = 'Server'
  AND Timestamp >= now() - INTERVAL 30 DAY
GROUP BY service
ORDER BY availability_pct ASC;

-- === SLI 2: Latency percentiles (per service, 5m buckets) ==================
-- Duration is nanoseconds; results in milliseconds.
-- Track p95 against SLO_API_LATENCY_P95_MS / SLO_UI_LATENCY_P95_MS.
SELECT
    toStartOfFiveMinutes(Timestamp)              AS bucket,
    ServiceName                                  AS service,
    quantile(0.50)(Duration) / 1e6               AS p50_ms,
    quantile(0.95)(Duration) / 1e6               AS p95_ms,
    quantile(0.99)(Duration) / 1e6               AS p99_ms
FROM otel_traces
WHERE SpanKind = 'Server'
  AND Timestamp >= now() - INTERVAL 24 HOUR
GROUP BY bucket, service
ORDER BY bucket, service;

-- === SLI 2b: Latency SLO compliance (per service, 24h) =====================
-- Share of requests faster than the latency target (request-based SLO).
-- Replace 500 with the target (ms) for the service class.
SELECT
    ServiceName                                                   AS service,
    round(100 * countIf(Duration / 1e6 <= 500) / count(), 3)      AS within_target_pct,
    round(quantile(0.95)(Duration) / 1e6, 1)                      AS p95_ms,
    count()                                                       AS requests
FROM otel_traces
WHERE SpanKind = 'Server'
  AND Timestamp >= now() - INTERVAL 24 HOUR
GROUP BY service
ORDER BY within_target_pct ASC;

-- === SLI 3: Error-rate (per namespace, 5m buckets) =========================
-- Application ERROR/FATAL log lines (excludes k8s events).
SELECT
    toStartOfFiveMinutes(Timestamp)                       AS bucket,
    ResourceAttributes['k8s.namespace.name']              AS namespace,
    countIf(SeverityText IN ('ERROR', 'FATAL'))           AS errors,
    count()                                               AS total_lines
FROM otel_logs
WHERE ResourceAttributes['telemetry.source'] != 'k8s-events'
  AND Timestamp >= now() - INTERVAL 24 HOUR
GROUP BY bucket, namespace
ORDER BY bucket, namespace;

-- === SLO burn rate (per service, 1h vs 30d error budget) ===================
-- burn_rate > 1 means the service is consuming error budget faster than the
-- SLO allows; > 14.4 on a 99.9% SLO ≈ page-worthy fast burn.
WITH 0.999 AS slo_target
SELECT
    ServiceName                                               AS service,
    countIf(StatusCode = 'Error') / count()                   AS error_ratio_1h,
    round((countIf(StatusCode = 'Error') / count()) / (1 - slo_target), 2)
                                                              AS burn_rate
FROM otel_traces
WHERE SpanKind = 'Server'
  AND Timestamp >= now() - INTERVAL 1 HOUR
GROUP BY service
HAVING count() > 0
ORDER BY burn_rate DESC;
