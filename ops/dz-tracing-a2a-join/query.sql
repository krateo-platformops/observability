-- dz-tracing "done" check: the browser <-> agent span join for portal agent calls (Agentiko chat).
-- Run read-only with --param_start / --param_end (a window [start, end) AFTER snowplow >= 1.12.71,
-- which allowlists the browser span name krateo.agentiko.a2a in rum-ingest, is live on the cluster).
--
-- One portal agent call is expected to produce, in ONE trace:
--   krateo-frontend                 krateo.agentiko.a2a          (browser, via snowplow rum-ingest)
--     krateo-agent-gateway-platform POST /api/a2a/*            child of the browser span
--       kagent-controller           POST /api/a2a/<ns>/<agent>
--         <agent>                   invoke_agent ...
-- A trace is kept whole by the tail-sampling tier when it carries LLM spans (agent turns do), so a
-- browser span without the rest of its trace is a join failure, not sampling.
-- DONE when, over a window with real chat use: browser_spans > 0, and joined_to_gateway,
-- joined_to_kagent and joined_to_agent all equal browser_spans (allow the documented exceptions:
-- a closed tab exports no browser span; a mid-stream abort is ERROR with no status code).
WITH
    t AS (
        SELECT TraceId,
               countIf(ServiceName = 'krateo-frontend' AND SpanName = 'krateo.agentiko.a2a')            AS browser,
               groupArrayIf(SpanId, ServiceName = 'krateo-frontend' AND SpanName = 'krateo.agentiko.a2a') AS browser_ids,
               groupArrayIf(ParentSpanId, ServiceName = 'krateo-agent-gateway-platform'
                                          AND startsWith(SpanName, 'POST /api/a2a'))                     AS gw_parents,
               countIf(ServiceName = 'kagent-controller' AND startsWith(SpanName, 'POST /api/a2a/'))       AS kagent_post,
               countIf(startsWith(SpanName, 'invoke_agent'))                                              AS agent_invoke
        FROM otel_traces
        WHERE Timestamp >= {start:DateTime64(9)} AND Timestamp < {end:DateTime64(9)}
        GROUP BY TraceId
        HAVING browser > 0
    )
SELECT
    count()                                                          AS browser_traces,
    sum(browser)                                                     AS browser_spans,
    countIf(hasAny(gw_parents, browser_ids))                         AS joined_to_gateway,
    countIf(hasAny(gw_parents, browser_ids) AND kagent_post > 0)     AS joined_to_kagent,
    countIf(hasAny(gw_parents, browser_ids) AND agent_invoke > 0)    AS joined_to_agent,
    countIf(length(gw_parents) = 0)                                  AS browser_only_traces
FROM t;
