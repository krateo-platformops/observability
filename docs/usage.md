---
type: Usage
title: clickstack-chart — usage
description: How the installer consumes the six charts, how to install the operators and the wrapper standalone via CompositionDefinition or direct helm install, and local helm template validation.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [usage, installer, helm]
timestamp: 2026-08-07T00:00:00Z
---

# Usage

## The canonical way: via the Krateo installer

The observability stack ships as part of a Krateo platform deploy. The
[installer](https://github.com/krateo-platformops/installer) umbrella pins all six charts as
`tier: observability` components gated on the **portal** feature — enabling the portal brings
up (in dependency order) the `clickhouse-operator` and `mongodb-operator` wrappers first
(their CRDs must be served before any data-layer CR exists), then `krateo-observability`,
`otel-collector-deployment`, `otel-collector-daemonset` and `krateo-sse-proxy` (exposed on
port 8080 for the portal events bell). There is nothing to install by hand from this repo:
the installer emits a CompositionDefinition per chart and `core-provider` reconciles the
compositions.

The deployed chart version is cluster-observable from
`CompositionDefinition.spec.chart.version` — that tag is the version of THIS repo's docs an
agent should read (see [llms.txt](./llms.txt)).

## Standalone: register the wrapper as a composition

Outside the installer you need `core-provider` plus the ClickHouse and MongoDB operators
already installed (the wrapper renders a `ClickHouseCluster` and a `MongoDBCommunity` CR but
ships neither CRD — see [api](./api.md)). Those operators are the `clickhouse-operator` /
`mongodb-operator` wrapper charts in this repo — install them first (either directly, below,
or by registering their `ClickhouseOperator` / `MongodbOperator` compositions, see
[examples/composition-registration](../examples/composition-registration/README.md)). Then:

```sh
kubectl apply -f compositiondefinition.yaml   # registers oci://ghcr.io/krateo-platformops/charts/krateo-observability
```

`core-provider` generates the `KrateoObservability` CRD from the chart's
`values.schema.json`; create an instance to deploy the stack
([examples/observability-composition](../examples/observability-composition/README.md)).

## Direct helm install (no Krateo engine)

Each chart is a plain Helm chart on GHCR:

```sh
helm install clickhouse-operator oci://ghcr.io/krateo-platformops/charts/clickhouse-operator \
  --version 0.1.0 --namespace krateo-system --create-namespace
helm install mongodb-operator oci://ghcr.io/krateo-platformops/charts/mongodb-operator \
  --version 0.1.0 --namespace krateo-system
helm install krateo-observability oci://ghcr.io/krateo-platformops/charts/krateo-observability \
  --version 0.1.11 --namespace krateo-system
helm install otel-collector-deployment oci://ghcr.io/krateo-platformops/charts/otel-collector-deployment \
  --version 0.3.3 --namespace krateo-system
helm install otel-collector-daemonset oci://ghcr.io/krateo-platformops/charts/otel-collector-daemonset \
  --version 0.1.5 --namespace krateo-system
helm install krateo-sse-proxy oci://ghcr.io/krateo-platformops/charts/krateo-sse-proxy \
  --version 0.1.6 --namespace krateo-system
```

Order matters: the two operators first (they install the `ClickHouseCluster` / `KeeperCluster`
/ `MongoDBCommunity` CRDs the wrapper's CRs need), then the wrapper (it creates the
`otel-clickhouse-credentials` Secret both collectors mount), collectors after. The MongoDB
operator watches its **own namespace** by default, so install it where the `MongoDBCommunity`
CR will live (here `krateo-system`) or set `operator.watchNamespace: "*"`; the ClickHouse
operator watches all namespaces at defaults. The hardcoded ClickHouse Service references
(`krateo-clickstack-clickhouse-clickhouse-headless.krateo-system.svc`) assume the
`krateo-system` namespace and the wrapper's `clickstack.fullnameOverride: krateo-clickstack`
— see [configuration](./configuration.md) before deviating.

## Local validation

```sh
for d in charts/*/; do
  helm dependency build "$d" 2>/dev/null || true   # clickstack / opentelemetry-collector deps
  helm lint "$d"
  helm template smoke "$d" --namespace krateo-system >/dev/null
done
```

This is what PR CI runs ([lint.yaml](../.github/workflows/lint.yaml)): helm lint,
`values.schema.json` well-formedness, and a render smoke test per chart — plus the
docs-standard lint (shared org workflow).
