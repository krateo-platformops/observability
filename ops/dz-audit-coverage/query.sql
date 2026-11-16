-- dz-audit "done" check: every audit writer lands in krateo_audit, on the schema, and nothing else
-- leaks. Run read-only with --param_start / --param_end over a ROLL-FREE window after
-- core-provider's otel.auditLogs is flipped on (audit emission is write-driven: a roll produces a
-- reconcile wave, so measure a settled window and report a roll separately).
--
-- Expected writers (ServiceName / ScopeName .../audit): core-provider, composition-dynamic-controller,
-- snowplow, authn, the generated REST controllers (rest-dynamic-controller, scope
-- rest-dynamic-controller/audit today), and the agent gateway's projection (krateo-agent-gateway,
-- observability/gateway-audit).
-- DONE when, over a settled window with real activity: each writer has rows; every row carries
-- event.name='audit' and krateo.audit.schema_version; no OTLP slog rows from core-provider or the
-- composition controllers reach otel_logs (their pod logs arrive via filelog, ScopeName ''); and
-- the WORM copy holds the same count as krateo_audit for the window.

-- 1. Rows per writer, and schema coverage
SELECT ServiceName, ScopeName,
       count()                                                       AS rows,
       countIf(LogAttributes['event.name'] != 'audit')               AS not_event_audit,
       countIf(LogAttributes['krateo.audit.schema_version'] = '')    AS no_schema_version,
       groupUniqArray(LogAttributes['krateo.audit.schema_version'])  AS schema_versions,
       min(Timestamp) AS first_row, max(Timestamp) AS last_row
FROM krateo_audit
WHERE Timestamp >= {start:DateTime64(9)} AND Timestamp < {end:DateTime64(9)}
GROUP BY ServiceName, ScopeName
ORDER BY rows DESC;

-- 2. Leakage: OTLP slog rows (a non-empty ScopeName) from the controllers in otel_logs; expect 0
SELECT ServiceName, ScopeName, count() AS rows
FROM otel_logs
WHERE Timestamp >= {start:DateTime64(9)} AND Timestamp < {end:DateTime64(9)}
  AND ServiceName IN ('core-provider', 'composition-dynamic-controller')
  AND ScopeName != ''
GROUP BY ServiceName, ScopeName;

-- 3. WORM copy: rows in the window in krateo_audit vs krateo_audit_worm; expect equal
SELECT
    (SELECT count() FROM krateo_audit      WHERE Timestamp >= {start:DateTime64(9)} AND Timestamp < {end:DateTime64(9)}) AS audit_rows,
    (SELECT count() FROM krateo_audit_worm WHERE Timestamp >= {start:DateTime64(9)} AND Timestamp < {end:DateTime64(9)}) AS worm_rows;
