---
type: API
title: clickstack-chart — api
description: The contract — the six generated composition types and their CompositionDefinitions, the operator CRDs the wrapper charts install, the HTTP surfaces (/events, SSE, HyperDX API, OTLP), and the upstream CRs the data layer deploys but does not own.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [api, compositiondefinition, crd, clickhouse]
timestamp: 2026-08-07T00:00:00Z
---

# API

## No hand-authored CRDs

**This repo authors NO CRD of its own.** There is no `crds-subchart/`, and none of its own
templates hand-write a `CustomResourceDefinition` — the four non-operator charts render only
ConfigMaps, Secrets, Services, Deployments and RBAC. The two operator wrappers DO render
CRDs, but those are **vendored upstream** operator CRDs — shipped as templates by the wrapped
charts, not authored here (the operator CRDs are listed below).
The typed composition surface it exposes to the platform is **generated**: `core-provider`
reads each chart's `values.schema.json` and derives a composition CRD, so the contract is
edited by changing the chart values surface, never by editing CRD YAML.

## The generated composition types

The installer registers each chart as a composition (`tier: observability`, portal-gated):

| Kind | Chart | OCI artifact |
|------|-------|--------------|
| `ClickhouseOperator` | `charts/clickhouse-operator` | `oci://ghcr.io/krateo-platformops/charts/clickhouse-operator` |
| `MongodbOperator` | `charts/mongodb-operator` | `oci://ghcr.io/krateo-platformops/charts/mongodb-operator` |
| `KrateoObservability` | `charts/krateo-observability` | `oci://ghcr.io/krateo-platformops/charts/krateo-observability` |
| `OtelCollectorDeployment` | `charts/otel-collector-deployment` | `oci://ghcr.io/krateo-platformops/charts/otel-collector-deployment` |
| `OtelCollectorDaemonset` | `charts/otel-collector-daemonset` | `oci://ghcr.io/krateo-platformops/charts/otel-collector-daemonset` |
| `KrateoSseProxy` | `charts/krateo-sse-proxy` | `oci://ghcr.io/krateo-platformops/charts/krateo-sse-proxy` |

All live in API group `composition.krateo.io`; the served version is derived from the
pinned chart version (`0.1.11` → `v0-1-11`, the operator wrappers at `0.1.0` → `v0-1-0`), so
**a chart-version bump changes the CR's apiVersion**. The CR `spec` is the chart's values
(typed by `values.schema.json`) — for the operator wrappers that is the two-key shape
(`global`, `operator`) from [configuration](./configuration.md). The deployed version is
always readable from the cluster: `CompositionDefinition.spec.chart.version`.

The repo-root [`compositiondefinition.yaml`](../compositiondefinition.yaml) registers the
wrapper standalone (`core.krateo.io/v1alpha1`, name `krateo-observability`, namespace
`krateo-system`); in a real install the installer umbrella owns this and the per-collector
CompositionDefinitions.

## HTTP surfaces the deployed stack exposes

- **`GET /events?composition_id=<uid>`** on ClickHouse HTTP (port 8123, via
  `krateo-clickstack-clickhouse-clickhouse-headless`): a `predefined_query_handler`
  declared in the wrapper's `cluster.spec.settings.extraConfig.http_handlers`. Returns the
  latest 50 K8s events for a composition UID as JSON (name/namespace/uid/kind, reason,
  message, type, eventTime, source_component) from `otel_logs` rows where
  `telemetry.source = 'k8s-events'`. This is the ONLY supported event-query path — the
  catch-all `/` dynamic-query handler exists to preserve ClickHouse's native API, not for
  portal consumers. `/ping`, `/replicas_status` and `/metrics` are re-declared alongside it.
- **The sse-proxy** (ClusterIP `:8080`, exposed by the installer for the browser): serves
  the portal bell — the historical events snapshot and the `/notifications` SSE stream —
  plus `/health` probes. Its in-cluster address is published as the fixed-name
  **`sse-proxy-internal-endpoint` Secret** (`server-url`, `insecure: "true"`), which
  snowplow RESTActions reference by exact name. The endpoint/stream contract is owned by the
  code repo `krateo-platformops/sse-proxy`.
- **HyperDX**: the UI at the `krateo-clickstack-app-lb` LoadBalancer (`:3000`) and the
  Bearer-authenticated external API (`/api/v2/{alerts,webhooks,dashboards,sources}`,
  HyperDX ≥ 2.28) at the **`krateo-clickstack-api`** ClusterIP (`:8000`) — the direct
  backend port, because the port-3000 proxy strips `/api` and 404s the v2 routes.
- **OTLP ingest**: the daemonset collector's `otlp` receiver (`:4317` grpc / `:4318` http),
  reachable at the headless Service `otel-collector-daemonset-opentelemetry-collector.<ns>.svc`
  under the installer — the endpoint platform components export traces/metrics to.

## Operator CRDs installed by the wrapper charts

The two operator wrappers install the data-layer operator CRDs (rendered as **templates** by
the vendored upstream charts — so applied and upgraded with the release, never via the
helm-special `crds/` dir — all carrying `helm.sh/resource-policy: keep`, so a release
uninstall leaves them and any surviving CRs in place):

| CRD | Kind (short names) | Installed by |
|---|---|---|
| `clickhouseclusters.clickhouse.com` | `ClickHouseCluster` (`chc`, `clickhouse`) | `clickhouse-operator` |
| `keeperclusters.clickhouse.com` | `KeeperCluster` | `clickhouse-operator` |
| `mongodbcommunity.mongodbcommunity.mongodb.com` | `MongoDBCommunity` (`mdbc`) | `mongodb-operator` |

The operator wrappers expose no HTTP endpoints. The ClickHouse operator serves a controller
`/metrics` Service (port 8080, secure by default — [configuration](./configuration.md)).

## CRs it deploys but does NOT own

The wrapper's heavy lifting is done through those upstream operators' CRs, owned by the CRDs
above (installed by the operator wrappers in this repo, never authored here):

- **`ClickHouseCluster`** and **`KeeperCluster`** (the clickhouse.com operator) — rendered by
  the vendored `clickstack` subchart, configured here only through
  `clickstack.clickhouse.cluster.spec`. The `clickhouse-operator` wrapper installs the CRDs
  and runs the controller. Remember: upstream renders ONLY `cluster.spec` — top-level
  `clickhouse.*` keys are inert ([configuration](./configuration.md)).
- **`MongoDBCommunity`** (the MongoDB community operator) — rendered by the subchart;
  `spec.statefulSet.spec` passes through, which is how the chart sets the PVC retention
  policy. The `mongodb-operator` wrapper installs the CRD and runs the controller.
- The two `otel-collector-*` charts render standard workloads + collector config from the
  upstream `opentelemetry-collector` chart — no Krateo-owned CRD.

## Where the real schemas live

- **Composition types:** each chart's `values.schema.json` in THIS repo, read at the chart
  tag.
- **`ClickHouseCluster` / `MongoDBCommunity`:** the upstream operators.
- **Runtime contracts** (the `compositionresolver` label semantics, the SSE stream shape):
  the code repos `krateo-platformops/otel-collector` and `krateo-platformops/sse-proxy`, at
  the image tags the charts pin.
