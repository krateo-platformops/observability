---
type: Configuration
title: clickstack-chart — configuration
description: The whole per-chart values surface — the ClickStack passthrough, the /events handler and retention TTLs, the collector pipelines, the single-replica sse-proxy — and the operational gotchas.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [configuration, values, clickhouse, opentelemetry]
timestamp: 2026-08-07T00:00:00Z
---

# Configuration

The per-chart `values.yaml` surface and the real operational gotchas. Everything is traced to
the charts; where a stale comment disagrees with the rendered chart, the chart wins. Every
chart ships a `values.schema.json` typing its full surface (that schema is also what
`core-provider` turns into the composition CRD — [api](./api.md)).

## The operator wrappers — the `operator:` passthrough

Both `charts/clickhouse-operator` and `charts/mongodb-operator` share one contract, enforced
by `values.schema.json` (`additionalProperties: false` at the root): exactly **two**
top-level keys.

| Key | Purpose |
|---|---|
| `global` | Helm global values propagated to the upstream subchart. |
| `operator` | The whole upstream chart's values, passed through verbatim (the Helm **alias** of the upstream dependency — see [overview](./overview.md)). |

The schema types only the keys the wrappers set; everything else the upstream chart documents
passes through unchanged (`additionalProperties: true` under `operator`).

### `charts/clickhouse-operator`

Wrapper defaults (`values.yaml`) — webhook and cert-manager off:

```yaml
operator:
  webhook:
    enable: false
  certManager:
    enable: false
```

Everything else is the upstream `clickhouse-operator-helm 0.0.5` default. The upstream keys
that matter (all under `operator.`):

| Key | Upstream default | Effect |
|---|---|---|
| `controller.watchNamespaces` | `[]` (all) | Namespaces watched for `ClickHouseCluster`/`KeeperCluster`. |
| `crd.enable` | `true` | Ship the two CRDs with the release (templated). |
| `crd.keep` | `true` | `helm.sh/resource-policy: keep` on the CRDs — uninstall leaves them. |
| `manager.image.repository` / `tag` | `ghcr.io/clickhouse/clickhouse-operator` / appVersion (`0.0.5`) | The operator image. |
| `manager.resources` | 10m/64Mi req, 500m/128Mi lim | Operator pod resources. |
| `metrics.enable` / `port` / `secure` | `true` / `8080` / `true` | The controller `/metrics` Service. |
| `rbac.namespaced` | `false` | `false` = ClusterRole (all namespaces); `true` = Role (release namespace). |

### `charts/mongodb-operator`

Wrapper defaults: **none** — `values.yaml` is an empty passthrough (`operator: {}`). Upstream
`community-operator 0.13.0` defaults are correct for the platform: the operator and the
`community-operator-crds` subchart install, and no sample `MongoDBCommunity` CR is created.
The upstream keys that matter (all under `operator.`):

| Key | Upstream default | Effect |
|---|---|---|
| `operator.watchNamespace` | unset (own namespace) | `"*"` = watch all namespaces (also widens RBAC). |
| `operator.operatorImageName` / `operator.version` | `mongodb-kubernetes-operator` / `0.13.0` | The operator image (from `registry.operator`, `quay.io/mongodb`). |
| `operator.resources` | 500m/200Mi req, 1100m/1Gi lim | Operator pod resources. |
| `community-operator-crds.enabled` | `true` | Ship the `MongoDBCommunity` CRD with the release. |
| `createResource` | `false` | `true` would create a sample `MongoDBCommunity` CR — keep `false`. |
| `agent` / `versionUpgradeHook` / `readinessProbe` | pinned versions | Sidecar images the operator injects into database pods. |

## `charts/krateo-observability` — the wrapper surface

The heavy upstream values live under `clickstack:` and are passed through verbatim; Helm's
top-level `global` propagates into clickstack and all its subcharts
(clickhouse / otel / hyperdx / mongodb).

### Global / exposure

- `global.storageClassName: "standard-rwo"` — overrides the upstream default (`local-path`)
  for GKE.
- **HyperDX UI exposure:** `hyperdxLoadBalancer.enabled: true`, `name:
  krateo-clickstack-app-lb`, `port: 3000`. Renders an ADDITIONAL `LoadBalancer` Service
  (upstream only emits the ClusterIP `krateo-clickstack`, ports 3000/4320); selector mirrors
  the upstream app pods, no hardcoded `loadBalancerIP`. Expose HyperDX through the installer
  CR / this flag, not by hand-patching the upstream Service.
- **HyperDX API exposure (in-cluster):** the `krateo-clickstack-api` ClusterIP Service
  (always rendered, `templates/hyperdx-api-service.yaml`) reaches the HyperDX Express
  backend directly at port 8000 — required for the Bearer-authenticated `/api/v2/*` external
  API, which 404s through the port-3000 Next.js proxy (it strips the `/api` prefix).
- `clickstack.hyperdx.ingress.enabled: false`; `clickstack.fullnameOverride:
  krateo-clickstack` (keeps ClickHouse/Keeper/Mongo names stable under `core-provider`'s
  random release names — every collector/sse-proxy ClickHouse reference hardcodes
  `krateo-clickstack-clickhouse-clickhouse-headless`).

### Krateo additions (this chart's own values)

- `otelCredentials` → the `otel-clickhouse-credentials` Secret: `secretName`,
  `username: otelcollector`, `password: otelcollectorpass`. **These MUST match the ClickStack
  ClickHouse `otelcollector` user** (clickstack provisions it with the same password).
  Created in the release namespace (the collectors' namespace), so no cross-namespace
  replication.
- **The `/events` HTTP handler** lives under
  `clickstack.clickhouse.cluster.spec.settings.extraConfig.http_handlers` — NOT a ConfigMap.
  The operator merges `settings.extraConfig` into `/etc/clickhouse-server/config.d/`. The
  rule set declares `GET /events?composition_id=<uid>` (a `predefined_query_handler` over
  `otel_logs`, filtered to `telemetry.source = 'k8s-events'` and the
  `krateo.io/composition-id` attribute) **plus re-declared defaults** — `/ping`,
  `/replicas_status`, `/metrics`, and the catch-all `/` `dynamic_query_handler` — because
  once `http_handlers` is defined ClickHouse stops serving its default routes (operator
  probes would fail without them).
- `settings.extraConfig` also carries the **3-day system-log TTLs**
  (`query_log`/`text_log`/`metric_log`/`part_log`/`asynchronous_metric_log`/`trace_log`) —
  retention-incident fix, do not remove (see Gotchas).

### ClickHouse resources & storage (the load-bearing path)

- `clickstack.clickhouse.cluster.spec.containerTemplate.resources`: limits `cpu 4 / memory
  8Gi`, requests `cpu 1 / memory 2Gi`. **`cluster.spec` is the ONLY path upstream clickstack
  honours** — it renders only `clickhouse.cluster.spec` into the `ClickHouseCluster` CR.
- `clickstack.clickhouse.cluster.spec.dataVolumeClaimSpec.resources.requests.storage: 15Gi`
  — the REAL data-volume size (raised from the upstream 10Gi after the retention incident).
  Helm deep-merges `cluster.spec` over the upstream default, so only the overridden key is
  set.
- `clickstack.clickhouse.image: clickhouse/clickhouse-server:26.3-alpine` and
  `clickstack.clickhouse.persistence.size: 50Gi` are **INERT** (top-level keys upstream
  ignores) and kept un-moved on purpose — realizing the image = a deliberate ClickHouse
  version upgrade; to realize later move `image` → `cluster.spec.containerTemplate.image`
  (storage is already real at 15Gi via `dataVolumeClaimSpec`).
- `clickstack.otel.resources` (the OTel gateway): limits `cpu 1 / memory 512Mi`, requests
  `cpu 100m / memory 256Mi`.
- `clickstack.otel-collector.enabled: false` — the bundled OpAMP otel-collector subchart is
  DISABLED: it crashlooped (OpAMP supervisor mode ignoring the chart `--config`) and is
  redundant (Krateo's own collectors do the ClickHouse ingestion with
  `krateo.io/composition-id`).
- `clickstack.mongodb`: `enabled: true`, `persistence.size: 10Gi`, plus a StatefulSet PVC
  retention policy (`whenDeleted: Delete`) passed through `spec.statefulSet.spec` so MongoDB
  PVCs are reaped on teardown. The ClickHouse/Keeper orphaned-PVC reaper is **not** a chart
  hook (a composition post-delete hook doesn't run reliably under cdc uninstall) — it lives
  in the installer's ordered-teardown path (installer ≥ 0.2.191).

## `charts/otel-collector-deployment` — cluster-level collector

All under `opentelemetry-collector:` (the upstream dep):

- `mode: deployment`, `replicaCount: 1`, image
  `ghcr.io/krateo-platformops/otel-collector:1.0.2` (`1.0.2` = clickhouse-exporter
  schema-recreate-on-reconnect; `1.0.1` = patched k8sobjects receiver), `command.name:
  otelcol-krateo` (the binary kept its name when the image repo was renamed).
- `clusterRole.create: true` with read rules on core/apps/batch/autoscaling resources plus
  `get` on the Krateo CR groups (`composition.krateo.io`, `templates.krateo.io`,
  `widgets.templates.krateo.io`, `deployment.krateo.io`, `core.krateo.io`) — needed so
  `compositionresolver` can resolve an involvedObject to its owning composition.
- `presets` all disabled (the chart hand-rolls `config`).
- `config.receivers`: `k8sobjects` (watch Events) + `k8s_cluster` (60s);
  `jaeger`/`otlp`/`zipkin`/`prometheus` are **nulled**. The `prometheus` null is
  load-bearing: the minimal `krateo-otel-collector` image compiles in only
  `k8sobjects`+`k8s_cluster`, and the collector type-validates every *declared* receiver —
  declaring `prometheus` crashloops the default install.
- **Prometheus→OTLP bridge (scaffolded, off):** `collector.prometheusScrape.enabled` is a
  MARKER only. Enabling the bridge is a three-part change: (1) a collector image built WITH
  the prometheus receiver, (2) restore the scrape config preserved in comments under
  `config.receivers`, (3) add `prometheus` to the metrics pipeline receivers.
- `config.processors`: `memory_limiter` (75% / 20% spike), `k8sattributes` (metadata + all
  pod labels), `resource` (inserts `telemetry.source: k8s-events`), `compositionresolver`
  (`cache_ttl 5m`, `negative_cache_ttl 30s`, `label_key: krateo.io/composition-id`), `batch`.
- `config.exporters.clickhouse`:
  `tcp://krateo-clickstack-clickhouse-clickhouse-headless.krateo-system.svc:9000`, database
  `default`, tables `otel_logs`/`otel_traces`/`otel_metrics`, `create_schema: true`,
  **`ttl: 168h`** (7-day TTL stamped into table creation — retention incident, do not
  remove), retry-on-failure enabled.
- Pipelines: `logs` (k8sobjects → … → clickhouse), `metrics` (k8s_cluster → memory_limiter,
  batch → clickhouse), `traces: null`.
- `extraEnvs`: `CH_USERNAME`/`CH_PASSWORD` from the `otel-clickhouse-credentials` Secret.
- `resources`: limits `cpu 500m / memory 512Mi`, requests `cpu 100m / memory 256Mi`.

## `charts/otel-collector-daemonset` — node-level collector

Wraps the same upstream chart in `daemonset` mode, stock
`otel/opentelemetry-collector-contrib` image (`command.name: otelcol-contrib`):

- **Headless Service** (`service.enabled: true`, `clusterIP: None`) — a stable in-cluster
  DNS name for the node-local OTLP receiver
  (`<release>-opentelemetry-collector.<ns>.svc:4317` grpc / `:4318` http; under the
  installer the release is `otel-collector-daemonset`, which is the endpoint core-provider's
  `otel.endpoint` points at). Headless → consumes no Service-CIDR address.
- `presets`: `logsCollection` (without collector logs), `hostMetrics`,
  `kubernetesAttributes` (with `extractAllPodLabels`), `kubeletMetrics`.
- `config.receivers`: `filelog` with a container parser + multiline recombine;
  `hostmetrics`/`kubeletstats` at 60s (kubeletstats adds pod/container cpu+memory
  limit/request utilization and uptime metrics).
- `config.processors.transform`: strips ANSI escapes from log bodies and copies the pod
  label `krateo.io/composition-id` into the log attributes (label-based attribution —
  the daemonset does not run `compositionresolver`).
- `config.exporters.clickhouse`: same endpoint/creds as the deployment chart;
  `create_schema: true` here is what creates `otel_traces` (the gateway has no traces
  pipeline); **`ttl: 168h`** — do not remove.
- Pipelines: `logs` (filelog), `metrics` (`hostmetrics, kubeletstats, otlp`), `traces`
  (`otlp`) — the `otlp` entries are how engine/cdc/app OTLP metrics and traces reach
  ClickHouse (KOS-1 #112).
- Same `extraEnvs` Secret wiring and resources as the deployment chart.

## `charts/krateo-sse-proxy`

- **`replicaCount: 1` is deliberate.** sse-proxy is a STATEFUL in-memory SSE hub (each pod
  runs its own ClickHouse poller + client hub). Behind an L4 LoadBalancer with
  `sessionAffinity: None`, a client pins to one backend, so a degraded replica
  deterministically 503s a fixed subset of users on `/notifications` — exactly what broke
  the portal bell. A single hub eliminates the split; the poller resumes from `lastSeenUnix`
  on restart. (To scale >1: gate readiness on poller health + add session affinity, or move
  to a shared event store.)
- `image`: `registry: ghcr.io` + `repository: krateo-platformops/sse-proxy` + `tag: "1.1.2"`
  (registry split out so a mirror relocation only swaps the host);
  `global.imageRegistry` overrides the registry host for mirror/air-gapped installs.
- `service.type: ClusterIP`, `service.port: 8080`; container port 8080, `/health`
  liveness+readiness probes. Env: `CLICKHOUSE_URL/USER/PASSWORD`, `LISTEN_ADDR :8080`.
- `clickhouse.url:
  http://krateo-clickstack-clickhouse-clickhouse-headless.krateo-system.svc:8123`,
  `clickhouse.user: default`, password from the `clickhouse-credentials` Secret
  (`passwordSecret.optional: true`).
- Renders the fixed-name **`sse-proxy-internal-endpoint` Secret** (`server-url` → this
  chart's own Service) — snowplow RESTActions reference it by that exact name; don't rename.
- `resources`: limits `cpu 200m / memory 128Mi`, requests `cpu 50m / memory 32Mi`. Hardened
  securityContext (`runAsNonRoot`, `readOnlyRootFilesystem`, drop ALL caps).

## Dependencies (what must exist around the stack)

- **The ClickHouse operator** (the `ClickHouseCluster`/`KeeperCluster` CRDs) and the
  **MongoDB community operator** (`MongoDBCommunity`) — the wrapper renders these CRs but
  ships none of their CRDs ([api](./api.md)). Those CRDs and controllers come from the
  `clickhouse-operator` / `mongodb-operator` wrapper charts **in this repo** (their `operator:`
  surface is above); under the installer they are the compositions the wrapper declares as
  dependencies, installed first.
- **Upstream chart deps** (`Chart.lock`): `clickstack 3.0.2`, `opentelemetry-collector
  0.158.1`.
- **The `otel-clickhouse-credentials` Secret** (rendered by the wrapper) — both collectors'
  `extraEnvs` reference it.
- **Stable names** via `clickstack.fullnameOverride: krateo-clickstack` — the ClickHouse
  endpoint every consumer hardcodes derives from it.

## Gotchas

- **A behavioral fix MUST ride a version bump.** `core-provider`/helm cache a chart by
  version tag and never re-pull an unchanged version. `0.1.2` was once mutably overwritten
  with the 8Gi ClickHouse change; live clusters kept the cached old artifact and ClickHouse
  OOMed the bell `/notifications` query — the fix was re-shipped as a new version. Never
  overwrite a published version.
- **ClickHouse settings only count under `cluster.spec`.** Upstream clickstack ignores
  top-level `clickhouse.resources` / `clickhouse.image` / `clickhouse.persistence` /
  `clickhouse.extraVolumes`. A top-level `resources:` is INERT and silently leaves
  ClickHouse at the operator default (~1Gi) → OOM; an `extraVolumes` ConfigMap mount is a
  dead object (why the `/events` handler moved into `settings.extraConfig` in `0.1.6`).
- **Retention TTLs are load-bearing.** With no TTL the `otel_*` tables + `system.*` logs
  filled the 10Gi volume in 8 days → inserts failed with code 243 → the collectors
  OOM-crash-looped → telemetry dead (2026-07 incident). The paired fixes: 15Gi
  `dataVolumeClaimSpec`, 3-day system-log TTLs (wrapper `0.1.9`), `ttl: 168h` in both
  collectors' clickhouse exporter (`0.3.3`/`0.1.5`). `ttl` applies only at table CREATION
  (`CREATE … IF NOT EXISTS`); pre-existing tables need a live `ALTER TABLE … MODIFY TTL`.
- **sse-proxy is single-replica on purpose** (above).
- **otel credentials must match the ClickHouse user.** `otelCredentials.username/password`
  must equal the `otelcollector` user clickstack provisions; a mismatch silently fails
  ClickHouse writes.
- **Keep the bundled otel-collector disabled.** Re-enabling `clickstack.otel-collector`
  brings back the crashlooping OpAMP collector and duplicates ingestion without
  `krateo.io/composition-id`.
- **Don't declare the prometheus receiver on the minimal image.** The collector
  type-validates declared receivers even when unused — `unknown type: "prometheus"` →
  CrashLoopBackOff (why it is nulled, not gated).
- **The composition types are generated, not authored.** Change the chart values /
  `values.schema.json` surface and let `core-provider` re-derive the CRD ([api](./api.md)).
