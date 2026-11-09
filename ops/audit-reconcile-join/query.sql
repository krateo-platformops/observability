-- OTEL-8 (installer#400): the reconciles each portal write caused, joined out of band.
--
-- One row per successful mutating portal write in krateo_audit between {from} and {to}, with the
-- composition-controller reconciles of the same object that started within {window_s} seconds after
-- it. A write no reconcile followed still gets a row (reconciles = 0).
--
-- Join key, all exact (see README.md for why each part holds):
--   audit  concat(k8s.resource.resource, '-', k8s.resource.version, '-controller')
--     =    span resource k8s.deployment.name, which core-provider sets to "<resource>-<version>-controller"
--   audit  k8s.resource.name   = span k8s.object.name
--   audit  k8s.namespace.name  = span k8s.object.namespace   ('' on both sides for a cluster-scoped object)
--
-- Ambiguity is reported, never resolved: a reconcile whose window also holds another write to the
-- same object counts towards every candidate write, and is flagged in ambiguous_reconciles. If the
-- other write came from a different actor, other_actors names them.
--
-- Parameters: {from:DateTime64(9)}, {to:DateTime64(9)}, {window_s:UInt32}.
--   clickhouse-client --param_from='2026-11-09 00:00:00' --param_to='2026-11-09 12:00:00' \
--                     --param_window_s=600 --queries-file query.sql
WITH
writes AS
(
    SELECT
        -- TraceId/SpanId can be empty on an audit record, so the key hashes the whole record.
        cityHash64(Timestamp, TraceId, SpanId, toString(LogAttributes)) AS write_key,
        Timestamp AS write_ts,
        TraceId AS write_trace_id,
        SpanId AS write_span_id,
        LogAttributes['session.id'] AS session_id,
        if(LogAttributes['user.name'] != '', LogAttributes['user.name'], LogAttributes['enduser.id']) AS actor,
        LogAttributes['http.request.method'] AS method,
        LogAttributes['k8s.resource.group'] AS res_group,
        LogAttributes['k8s.resource.version'] AS res_version,
        LogAttributes['k8s.resource.resource'] AS res_resource,
        LogAttributes['k8s.resource.name'] AS obj_name,
        LogAttributes['k8s.namespace.name'] AS obj_namespace,
        concat(res_resource, '-', res_version, '-controller') AS controller
    FROM default.krateo_audit
    WHERE Timestamp >= {from:DateTime64(9)} AND Timestamp < {to:DateTime64(9)}
      AND LogAttributes['event.name'] = 'audit'
      AND LogAttributes['krateo.action'] = 'call'
      AND LogAttributes['outcome'] = 'success'
      AND method IN ('POST', 'PUT', 'PATCH', 'DELETE')
      -- snowplow's own AuditRecord write follows every audited call; it is the record, not an action.
      AND res_group != 'audit.krateo.io'
),
reconciles AS
(
    SELECT
        Timestamp AS rec_ts,
        TraceId AS rec_trace_id,
        SpanId AS rec_span_id,
        Duration AS rec_duration_ns,
        StatusCode AS rec_status,
        ResourceAttributes['k8s.deployment.name'] AS controller,
        SpanAttributes['k8s.object.kind'] AS obj_kind,
        SpanAttributes['k8s.object.name'] AS obj_name,
        SpanAttributes['k8s.object.namespace'] AS obj_namespace
    FROM default.otel_traces
    WHERE Timestamp >= {from:DateTime64(9)}
      AND Timestamp < {to:DateTime64(9)} + toIntervalSecond({window_s:UInt32})
      AND SpanName = 'reconcile'
      AND SpanKind = 'Server'
),
-- Every (write, reconcile) of the same object with the reconcile inside the write's window.
pairs AS
(
    SELECT w.*, r.rec_ts, r.rec_trace_id, r.rec_span_id, r.rec_duration_ns, r.rec_status, r.obj_kind
    FROM writes AS w
    INNER JOIN reconciles AS r
        ON w.controller = r.controller AND w.obj_name = r.obj_name AND w.obj_namespace = r.obj_namespace
    WHERE r.rec_ts >= w.write_ts AND r.rec_ts < w.write_ts + toIntervalSecond({window_s:UInt32})
),
-- How many writes, and which actors, claim each reconcile.
claims AS
(
    SELECT rec_span_id, count() AS candidate_writes, groupUniqArray(actor) AS candidate_actors
    FROM pairs
    GROUP BY rec_span_id
),
per_write AS
(
    SELECT
        p.write_key,
        count() AS reconciles,
        min(p.rec_ts) AS first_rec_ts,
        countIf(p.rec_status = 'Error') AS error_reconciles,
        countIf(c.candidate_writes > 1) AS ambiguous_reconciles,
        arrayDistinct(arrayFlatten(groupArray(c.candidate_actors))) AS claiming_actors,
        any(p.obj_kind) AS obj_kind,
        groupArray(p.rec_trace_id) AS reconcile_trace_ids
    FROM pairs AS p
    INNER JOIN claims AS c ON p.rec_span_id = c.rec_span_id
    GROUP BY p.write_key
)
SELECT
    w.write_ts,
    w.actor,
    w.session_id,
    w.method,
    w.res_group,
    w.res_version,
    w.res_resource,
    w.obj_namespace,
    w.obj_name,
    w.controller,
    pw.obj_kind,
    pw.reconciles,
    if(pw.reconciles = 0, NULL, dateDiff('millisecond', w.write_ts, pw.first_rec_ts)) AS first_reconcile_after_ms,
    pw.error_reconciles,
    pw.ambiguous_reconciles,
    arrayFilter(a -> a != w.actor, pw.claiming_actors) AS other_actors,
    w.write_trace_id,
    pw.reconcile_trace_ids
FROM writes AS w
LEFT JOIN per_write AS pw
    ON w.write_key = pw.write_key
ORDER BY w.write_ts
SETTINGS join_use_nulls = 0
