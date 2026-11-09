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
| `controller`, `obj_kind` | the composition controller the join matched, and the Kind its span reports |
| `reconciles`, `first_reconcile_after_ms`, `error_reconciles` | the reconciles of that object that started within `window_s` after the write, how soon the first one came, and how many ended in error |
| `ambiguous_reconciles`, `other_actors` | reconciles that another write to the same object can also claim, and who made those writes |
| `write_trace_id`, `reconcile_trace_ids` | the trace of the write, and the reconcile traces, for drilling down in HyperDX |

A write with no reconcile after it still gets a row, with `reconciles = 0`.

## The join key

| audit record (`LogAttributes`) | reconcile span |
|---|---|
| `k8s.resource.resource` + `-` + `k8s.resource.version` + `-controller` | resource attribute `k8s.deployment.name` |
| `k8s.resource.name` | `k8s.object.name` |
| `k8s.namespace.name` | `k8s.object.namespace` (`''` on both sides for a cluster-scoped object) |

Why the controller name, and not the Kind:

- **The audit record has no Kind.** snowplow records the API path's parts: group, version and the
  *plural* resource (`acmefeedbacks`).
- **The span has no group or version.** unstructured-runtime's `StartReconcileSpan`
  (`pkg/telemetry/tracing.go`) sets only `k8s.object.kind/name/namespace`. Its resource
  (`buildResource`) adds `service.name`, `service.version` and `k8s.deployment.name`.
- **Guessing a Kind from a plural is wrong on real data.** On krateo-057, Kind `Agentiko` is
  plural `agentikoes`, and Kind `AgentgatewayPolicies` is plural `agentgatewaypolicies`.
- **The controller name is exact.** core-provider sets every composition controller's
  `DEPLOYMENT_NAME` to `<resource>-<apiVersion>-controller` (`helm/core-provider/assets/cdc/configmap.yaml`;
  `reclaimleaked.go` relies on the same name). That's the span's `k8s.deployment.name`, and
  `apiVersion` is the dashed form the audit record carries (`v0-1-0`). On krateo-057 all 70
  composition controllers follow it, for example `acmefeedbacks-v0-1-0-controller`.

The span's `k8s.object.kind` is still returned, as `obj_kind`, so the reader sees the Kind.

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
