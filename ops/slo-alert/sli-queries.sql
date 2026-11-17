-- ---------------------------------------------------------------------------
-- SLI 3, error-rate, over otel_logs: the one SLI that is not built from span
-- metrics (log lines have no span-metrics equivalent).
--
-- Availability, latency (API and UI) and the error-budget burn rate are NOT
-- here: their single definition is sli-span-metrics.sql, over the span metrics
-- (traces.span.metrics.calls / .duration) that the cmp-slo-* alerts and the
-- HyperDX dashboard of the CMP performance SLOs read. The otel_traces scans
-- that used to live here were a second, competing definition and are gone.
--
-- The statement is standalone: paste it into a HyperDX chart (SQL mode) or run
-- it with clickhouse-client. It defaults to the last 24h.
-- ---------------------------------------------------------------------------

-- === SLI 3: Error-rate (per namespace, 5m buckets) =========================
-- Application log lines at ERROR or above. Excludes k8s events, the observability stack (rule 3 in
-- docs/alerts.md) and krateo-budget-evaluator, whose ERROR lines are breached budgets.
SELECT
    toStartOfFiveMinutes(Timestamp)                       AS bucket,
    ResourceAttributes['k8s.namespace.name']              AS namespace,
    countIf(SeverityNumber >= 17)                         AS errors,
    count()                                               AS total_lines
FROM otel_logs
WHERE ResourceAttributes['telemetry.source'] != 'k8s-events'
  AND ServiceName NOT IN ('krateo-observability', 'krateo-alert-troubleshooter', 'krateo-alert-provider', 'incident-controller', 'incident-provider', 'krateo-clickstack', 'krateo-clickstack-clickhouse-clickhouse', 'krateo-clickstack-keeper-keeper', 'krateo-clickstack-mongodb', 'clickhouse-mcp-server', 'clickhouse-operator', 'incident-agent', 'autopilot', 'agentiko', 'kagent', 'repo-mcp-server', 'krateo-budget-evaluator')
  AND Timestamp >= now() - INTERVAL 24 HOUR
GROUP BY bucket, namespace
ORDER BY bucket, namespace;
