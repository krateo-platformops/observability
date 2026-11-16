# dz-tracing: the browser ↔ agent span join

`query.sql` is the "done" check for dz-tracing's last gap. A portal agent call (Agentiko chat) must
give one trace that holds the browser's `krateo.agentiko.a2a` span (krateo-frontend, ingested by
snowplow rum-ingest), the platform gateway's `POST /api/a2a/*` as its child, kagent-controller's
`POST /api/a2a/<ns>/<agent>`, and the agent's `invoke_agent`.

```
clickhouse-client --readonly=1 --param_start='…' --param_end='…' --queries-file query.sql
```

**Done:** over a window with real chat use after snowplow ≥ 1.12.71 is live (its rum-ingest
allowlists the span name; earlier versions drop it and count `snowplow_rum_ingest_spans_total
{outcome="dropped_name"}`), `browser_spans > 0` and `joined_to_gateway = joined_to_kagent =
joined_to_agent = browser_spans`. A closed tab exports no browser span; a mid-stream abort is an
ERROR with no status code; neither breaks the join.

**Baseline on krateo-057** (2026-11-09 15:00 → 11-16 15:00Z, snowplow 1.12.70): 0 browser spans,
as expected before the allowlist ships.
