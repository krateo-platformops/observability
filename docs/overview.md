---
type: Architecture
title: clickstack-chart — overview
description: What the six charts deploy — the ClickHouse and MongoDB operator wrappers, the ClickStack wrapper, the two OTel collectors, the SSE proxy — how they version independently, and how they become Krateo compositions.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [observability, clickstack, architecture]
timestamp: 2026-08-07T00:00:00Z
---

# Overview

What the Krateo **observability stack** is and **how it deploys** as a set of Krateo
compositions. This is the deployment view; the internals/runtime view (the custom OTel
collector, the `compositionresolver` processor, the sse-proxy binary) lives in the code repos
`krateo-platformops/otel-collector` and `krateo-platformops/sse-proxy`. Every claim below is
traced to a file in this repo — where a comment disagrees with what the chart actually
renders, the rendered chart wins.

## What the stack is

The Krateo portal's observability backend. It ingests Kubernetes telemetry (pod logs, K8s
Events, host/kubelet/cluster metrics, OTLP traces) into ClickHouse, enriches each K8s event
with a `krateo.io/composition-id` attribute so telemetry can be sliced per Krateo
composition, lets operators explore it through HyperDX, and feeds the **portal events bell**:
a ClickHouse `GET /events?composition_id=<uid>` predefined-query handler plus the
`krateo-sse-proxy` that streams new events to the browser over Server-Sent Events.

This repo is the packaging as Krateo blueprints: it wraps the upstream ClickStack Helm chart
and the upstream OpenTelemetry collector chart, folds in the Krateo-specific glue, and ships
a `values.schema.json` per chart so `core-provider` can generate typed composition CRDs.

## Repo layout — six deployable charts

| Path | Chart name | OCI artifact | Version (`Chart.yaml`) |
|------|------------|--------------|------------------------|
| `charts/clickhouse-operator` | `clickhouse-operator` | `oci://ghcr.io/krateo-platformops/charts/clickhouse-operator` | `0.1.0`, wraps `clickhouse-operator-helm 0.0.5` |
| `charts/mongodb-operator` | `mongodb-operator` | `oci://ghcr.io/krateo-platformops/charts/mongodb-operator` | `0.1.0`, wraps `community-operator 0.13.0` |
| `charts/krateo-observability` | `krateo-observability` | `oci://ghcr.io/krateo-platformops/charts/krateo-observability` | `0.1.11`, `appVersion 3.0.2` |
| `charts/otel-collector-deployment` | `otel-collector-deployment` | `oci://ghcr.io/krateo-platformops/charts/otel-collector-deployment` | `0.3.3`, image `otel-collector:1.0.2` |
| `charts/otel-collector-daemonset` | `otel-collector-daemonset` | `oci://ghcr.io/krateo-platformops/charts/otel-collector-daemonset` | `0.1.5` |
| `charts/krateo-sse-proxy` | `krateo-sse-proxy` | `oci://ghcr.io/krateo-platformops/charts/krateo-sse-proxy` | `0.1.6`, image `sse-proxy:1.1.2` |

All six versions are **literally pinned** in each `Chart.yaml` (no `CHART_VERSION`
placeholder), so a release tag publishes each chart at its own declared version — the OCI
artifact name is the chart name (see [release](./release.md)).

- **The two operator wrappers** (`charts/clickhouse-operator`, `charts/mongodb-operator`,
  both `0.1.0`) are thin dependency wrappers over the unforked upstream operator charts —
  they install the data-layer CRDs and controllers the ClickStack wrapper's CRs need. They
  were folded in from the retired `clickstack-operators-chart` repo (see [log](./log.md)).
- **The ClickStack wrapper** (`charts/krateo-observability`) is the heaviest piece. It
  vendors the upstream `clickstack` `3.0.2` chart as a dependency and passes values through
  under the `clickstack:` key. The `KrateoObservability` composition the installer creates is
  this chart.
  > **Version bumps are load-bearing for re-pull.** `core-provider`/helm cache a chart by its
  > version tag and never re-pull an *unchanged* version, so a behavioral fix MUST ride a
  > version bump or live clusters keep the old artifact (`Chart.yaml` history: `0.1.3`).
- **The two OTel collector charts** each wrap the upstream `opentelemetry-collector`
  `0.158.1` chart. The deployment-mode one runs the custom
  `ghcr.io/krateo-platformops/otel-collector` image (binary `otelcol-krateo`, the
  `compositionresolver` processor); the daemonset-mode one runs the stock
  `otel/opentelemetry-collector-contrib` image for node-level collection.
- **The SSE proxy** (`krateo-sse-proxy`) is a small Krateo-built Go service
  (`ghcr.io/krateo-platformops/sse-proxy:1.1.2`), deliberately single-replica (a stateful
  in-memory SSE hub — see [configuration](./configuration.md)).

> The federated `clickstack-agent` chart used to live here under `kagent/`; it was
> **extracted** during the org migration (2026-08-03) and is no longer part of this repo.

## What each chart deploys

### `charts/clickhouse-operator` (data-layer operator)

A dependency wrapper (wrapper `0.1.0`) over the unforked `clickhouse-operator-helm 0.0.5`
(`oci://ghcr.io/clickhouse`). At defaults it renders the operator Deployment
`<release>-operator-controller-manager` (image
`ghcr.io/clickhouse/clickhouse-operator:v0.0.5`) watching all namespaces, the two CRDs
`clickhouseclusters.clickhouse.com` and `keeperclusters.clickhouse.com`, operator RBAC + a
ServiceAccount, and a secure metrics Service on port 8080. The wrapper's own defaults turn
**off** the upstream admission webhook and the cert-manager integration
(`operator.webhook.enable: false`, `operator.certManager.enable: false`) — the operator
reconciles `ClickHouseCluster`/`KeeperCluster` without the webhook, so no
`Certificate`/`Issuer`/`WebhookConfiguration` objects are rendered. This is the operator the
ClickStack wrapper's `ClickHouseCluster`/`KeeperCluster` CRs depend on.

### `charts/mongodb-operator` (data-layer operator)

A dependency wrapper (wrapper `0.1.0`) over MongoDB `community-operator 0.13.0` plus its
bundled `community-operator-crds` subchart. At defaults it renders the operator Deployment
`mongodb-kubernetes-operator` (image `quay.io/mongodb/mongodb-kubernetes-operator:0.13.0`)
watching its **own namespace** (`WATCH_NAMESPACE` from the downward API), the single CRD
`mongodbcommunity.mongodbcommunity.mongodb.com`, and RBAC + ServiceAccounts for both the
operator and the database pods it manages. The wrapper's values are empty (`operator: {}`);
upstream defaults are already correct and no sample `MongoDBCommunity` CR is created
(`createResource: false`). This is the operator the ClickStack wrapper's `MongoDBCommunity`
CR depends on.

> Both operators alias their upstream dependency to `operator` in `Chart.yaml` — a values key
> that pascalizes to the chart's own composition Kind (`ClickhouseOperator`/`MongodbOperator`)
> would collide in crdgen's struct naming, so Helm reads the subchart values from `operator:`.
> The upstream CRDs ship as **templates** (not the helm-special `crds/` dir), so
> core-provider's helm engine applies and upgrades them with the release; all carry
> `helm.sh/resource-policy: keep`, so a release uninstall leaves the CRDs — and any surviving
> data-layer CRs — in place.

### `charts/krateo-observability` (the wrapper)

The vendored `clickstack` subchart renders ClickHouse (a `ClickHouseCluster` CR for the
ClickHouse operator), a Keeper cluster, HyperDX (Deployment + the `krateo-clickstack`
ClusterIP Service on ports 3000/app and 4320/opamp) and MongoDB (a `MongoDBCommunity` CR).
Krateo additions rendered by THIS chart's own templates
(`charts/krateo-observability/templates/`):

- **`otel-clickhouse-credentials` Secret** (`otel-credentials-secret.yaml`, gated on
  `otelCredentials.enabled`) — `username`/`password` for the collectors' ClickHouse
  `otelcollector` user, created in the release namespace.
- **`krateo-clickstack-app-lb` LoadBalancer Service** (`hyperdx-loadbalancer.yaml`, gated on
  `hyperdxLoadBalancer.enabled`) — an ADDITIONAL Service exposing the HyperDX UI on port
  3000 externally (upstream only emits the ClusterIP `krateo-clickstack`), no hardcoded
  `loadBalancerIP`.
- **`krateo-clickstack-api` ClusterIP Service** (`hyperdx-api-service.yaml`, always
  rendered, since `0.1.11`) — exposes the HyperDX Express backend directly on port 8000, so
  in-cluster clients (Composition RESTDefinitions) can call the Bearer-authenticated
  `/api/v2/*` external API without the Next.js proxy's `/api`-stripping at port 3000.

The `GET /events?composition_id=<uid>` ClickHouse HTTP handler is **not a template**: since
`0.1.6` it lives in `values.yaml` under
`clickstack.clickhouse.cluster.spec.settings.extraConfig.http_handlers` (the operator-native
config merge). The former `clickhouse-http-handlers` ConfigMap + `extraVolumes` mount was
inert — upstream clickstack renders only `clickhouse.cluster.spec` — and was removed.

`clickstack.fullnameOverride: krateo-clickstack` keeps the ClickHouse/Keeper/Mongo resource
names stable regardless of the release name `core-provider` assigns, so the collectors and
sse-proxy can resolve `krateo-clickstack-clickhouse-clickhouse-headless`.

### `charts/otel-collector-deployment` (cluster-level)

A single-replica `opentelemetry-collector` in `deployment` mode running the custom
`krateo-otel-collector` image. Pipelines: `logs` (`k8sobjects` Events →
`memory_limiter, k8sattributes, resource, compositionresolver, batch` → ClickHouse) and
`metrics` (`k8s_cluster` → ClickHouse); `traces: null`. The `compositionresolver` processor
stamps `krateo.io/composition-id`. It ships its own ClusterRole (read on K8s workloads + `get`
on the Krateo CR groups) and pulls ClickHouse creds from the `otel-clickhouse-credentials`
Secret via `extraEnvs`. A Prometheus→OTLP bridge is scaffolded but **nulled by default** (the
minimal collector image has no `prometheus` receiver — see
[configuration](./configuration.md)).

### `charts/otel-collector-daemonset` (node-level)

A daemonset-mode `opentelemetry-collector` (stock contrib image) collecting pod logs
(`filelog`), host metrics and kubelet metrics, plus **OTLP ingest**: its `metrics` and
`traces` pipelines consume the node-local `otlp` receiver so the engine/cdc/apps can export
OTLP → ClickHouse. A **headless Service** (`0.1.4`) gives that receiver a stable in-cluster
DNS name (`<release>-opentelemetry-collector.<ns>.svc:4317` grpc / `:4318` http).

### `charts/krateo-sse-proxy`

A Deployment + ClusterIP Service (port 8080, `/health` probes) polling ClickHouse
(`clickhouse.url` → `krateo-clickstack-clickhouse-clickhouse-headless:8123`) and pushing new
K8s events to browsers over SSE. It also renders the **`sse-proxy-internal-endpoint`
Secret** — the fixed-name Endpoint that snowplow RESTActions use to fetch the historical
`/events` snapshot in-cluster. `replicaCount: 1` is deliberate.

## How it becomes compositions

The [installer](https://github.com/krateo-platformops/installer) umbrella pins **all six
charts** as `tier: observability` components gated on the portal feature, ordered by
dependencies: the `clickhouse-operator` + `mongodb-operator` wrappers install first (their
CRDs must be served before any data-layer CR exists), then `krateo-observability` (which
declares `deps: [clickhouse-operator, mongodb-operator]`), then `otel-collector-deployment` →
`otel-collector-daemonset`, and `krateo-sse-proxy` (exposed on port 8080 for the portal
bell). On uninstall, the installer's reverse-dependency drain removes `krateo-observability`
and its data-layer CRs **before** the operators, so the operators stay alive to clear those
CRs' finalizers. For each, `core-provider` reads the chart's `values.schema.json` and
generates a typed composition CRD — `ClickhouseOperator`, `MongodbOperator`,
`KrateoObservability`, `OtelCollectorDeployment`, `OtelCollectorDaemonset`, `KrateoSseProxy`.
The repo-root [`compositiondefinition.yaml`](../compositiondefinition.yaml) registers the
wrapper standalone (outside the installer). Details: [api](./api.md), [usage](./usage.md).

## Cross-references

- **Code repos (internals & runtime):** `krateo-platformops/otel-collector` (collector image,
  `compositionresolver`) and `krateo-platformops/sse-proxy` — versioned at the **image**
  tags the charts pin; this repo's docs are versioned at the **chart** tags.
- **Installer umbrella:** `krateo-platformops/installer` (owns the CompositionDefinitions).
- **Upstream:** [`ClickHouse/ClickStack-helm-charts`](https://github.com/ClickHouse/ClickStack-helm-charts),
  [`open-telemetry/opentelemetry-helm-charts`](https://github.com/open-telemetry/opentelemetry-helm-charts).
