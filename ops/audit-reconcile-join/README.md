# Audit-to-reconcile join (OTEL-8)

`query.sql` answers installer#400 (OTEL-8): given a portal action, which reconciles did it cause,
and who acted. It joins **out of band**, at query time. Nothing is stamped on the user's object, so
there's no new ResourceVersion, no informer churn, and no inherited sampling decision.

One row per successful mutating portal write (`POST`, `PUT`, `PATCH`, `DELETE`) in
`default.krateo_audit`, with:

| column | meaning |
|---|---|
| `actor`, `session_id` | who acted: `user.name`, or `enduser.id` when that's empty; and the W3C-baggage session |
| `res_group`, `res_version`, `res_resource`, `obj_namespace`, `obj_name` | the object written, as the audit record names it |
| `gvr`, `obj_kind` | the composition GVR the join matched on, and the Kind its span reports |
| `reconciles`, `first_reconcile_after_ms`, `error_reconciles` | the reconciles of that object that started within `window_s` after the write, how soon the first one came, and how many ended in error |
| `ambiguous_reconciles`, `other_actors` | reconciles that another write to the same object can also claim, and who made those writes |
| `write_trace_id`, `reconcile_trace_ids` | the trace of the write, and the reconcile traces, for drilling down in HyperDX |

A write with no reconcile after it still gets a row, with `reconciles = 0`.

## The join key

| audit record (`LogAttributes`) | reconcile span |
|---|---|
| `k8s.resource.group` + `/` + `k8s.resource.version` + `/` + `k8s.resource.resource` | span attribute `krateo.composition.gvr`, else resource attribute `krateo.io/composition-gvr` |
| `k8s.resource.name` | `k8s.object.name` |
| `k8s.namespace.name` | `k8s.object.namespace` (`''` on both sides for a cluster-scoped object) |

Why the composition GVR, and not the Kind:

- **The audit record has no Kind.** snowplow records the API path's parts: group, version and the
  *plural* resource (`acmefeedbacks`).
- **Guessing a Kind from a plural is wrong on real data.** On krateo-057, Kind `Agentiko` is
  plural `agentikoes`, and Kind `AgentgatewayPolicies` is plural `agentgatewaypolicies`.
- **The GVR is exact, and both sides carry it in the same form**, `<group>/<version>/<plural>` with
  the dashed version the audit record has (`composition.krateo.io/v0-1-0/acmefeedbacks`).

Why the span attribute first: core-provider is moving to pooled composition controllers, where one
process hosts several Kinds. A process's resource attributes (`krateo.io/composition-gvr`,
`service.instance.id`, `k8s.deployment.name`) then name the pool, and a join on them would credit a
write to every Kind in the pool. From cdc 2.13.100 every root span also carries
`krateo.composition.gvr`, naming the controller of THAT span, so the query prefers it. Spans from an
older cdc have only the resource attribute, which for a dedicated controller says the same thing, so
a window that spans the upgrade still joins. Until 2026-11-15 the key was `k8s.deployment.name`
(`<resource>-<version>-controller`); on 7 days of krateo-057 data both keys give the same 70 writes,
700 reconciles and 63 matched writes.

The span's `k8s.object.kind` is still returned, as `obj_kind`, so the reader sees the Kind. The
output column `controller` is now `gvr`.

## Ambiguity is shown, not resolved

A controller reconciles an object for any reason: a write, a timer, a dependency. So "this
reconcile was caused by that write" is a claim, not a fact. Every reconcile inside a write's window
is attributed to that write. When two writes to the same object both have it in their window, it
counts towards **both**, and each row reports it in `ambiguous_reconciles`. `other_actors` names the
other writer, so a second actor is never hidden. The query never picks one.

## Window

`window_s` is how long after a write a reconcile is still attributed to it. `600` (10 minutes) is a
sensible default: a watch-triggered reconcile starts within seconds, and a composition's Helm work
can take minutes. A longer window pulls in more timer-driven reconciles, which show up as
attributions rather than being hidden.

## Limits

- **7 days, not 365.** `otel_traces` keeps spans for 7 days and `krateo_audit` for 365. Older
  writes come back with `reconciles = 0`.
- **A create without a name can't join.** snowplow records the name from the request path, so a
  `POST` that names its object only in the body (`generateName`, or a create to the collection URL)
  is recorded as `-`. On krateo-057 a `POST alerts` carried its name and a `POST flexes` was
  recorded as `-`. Joining those needs the created object's name on the audit record, which is
  snowplow's to add.
- **No reconcile spans until tracing is on.** composition controllers export spans only with
  core-provider's `otel.enabled` (with `otel.tracing`, default true). Until then every write shows
  `reconciles = 0`. The tail sampler keeps reconcile traces whole, since they're outside the portal
  path, so every reconcile reaches `otel_traces`.
- **snowplow's own AuditRecord writes are excluded** (`audit.krateo.io`). One follows every
  audited call; it's the record, not an action.

## Running it

```sh
kubectl exec -i -n krateo-system krateo-clickstack-clickhouse-clickhouse-0-0-0 -- \
  clickhouse-client --readonly=1 \
  --param_from='2026-11-09 00:00:00' --param_to='2026-11-10 00:00:00' --param_window_s=600 \
  --queries-file /dev/stdin < ops/audit-reconcile-join/query.sql
```

It's tested by `tests/audit-reconcile-join-docker.py` (workflow `check-audit-reconcile-join`). The
test runs the query in the ClickHouse version krateo-057 runs, against tables created from 057's own
DDL (`tests/fixtures/`). It loads matches and negative controls (a reconcile outside the window,
before the write, on another name, namespace or resource, a non-root span, a non-reconcile span, a
namespaced twin of a cluster-scoped object), excluded writes (dry run, failed, AuditRecord) and an
ambiguous pair.
