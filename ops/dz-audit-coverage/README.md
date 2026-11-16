# dz-audit: every writer, on the schema, nothing leaking

`query.sql` is the "done" check for dz-audit, run over a **roll-free** window after core-provider's
`otel.auditLogs` is on (audit emission is write-driven, so a roll's reconcile wave is reported on its
own, not mixed into the steady state).

```
clickhouse-client --readonly=1 --param_start='…' --param_end='…' --queries-file query.sql
```

**Done:** each writer has rows; every row has `event.name = audit` and a
`krateo.audit.schema_version`; statement 2 returns no rows (no OTLP slog from the controllers in
`otel_logs`); statement 3 shows `audit_rows = worm_rows`.

**Baseline on krateo-057** (2026-11-09 15:00 → 11-16 15:00Z; auditLogs OFF except its 11-15
08:51–09:08Z flip):

| writer | rows | schema_version |
|---|---|---|
| krateo-agent-gateway (gateway-audit) | 3,005 | 1 |
| core-provider | 409 | 1 (flip window only) |
| snowplow | 224 | **missing on all rows** |
| composition-dynamic-controller | 45 | 1 (flip window only) |
| authn | 35 (+2 as `auth`, 11-12) | 1 |
| rest-dynamic-controller (`rest-dynamic-controller/audit`) | 24 | **missing on all rows** |

Leakage: none. WORM: 3,744 = 3,744.
