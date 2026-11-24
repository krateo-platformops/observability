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

### Upgrading from krateo-observability before 0.1.44

**Stop at 0.1.44 or 0.1.45 first.** From 0.1.46 the chart refuses an upgrade on a cluster that never
ran one of them, with `Secret .../mongodb-hyperdx-credentials has no uri ... Roll 0.1.44 or 0.1.45 first`.
Those versions move HyperDX's MongoDB user onto a generated credential; 0.1.46 then empties the old one,
which is unsafe in the same upgrade (see [log.md](./log.md), 2026-11-11). A fresh install is not affected.

| Installer | Pins krateo-observability |
|---|---|
| before 0.3.585 | before 0.1.44 |
| 0.3.585 to 0.3.588 | 0.1.44 |
| 0.3.589 to 0.3.671 | 0.1.45 |
| 0.3.672 and later | past the guard (0.1.51, then 0.1.55 from 0.3.679) |

So an installer before 0.3.585 goes through one in 0.3.589 to 0.3.671 before a later one. A cluster whose
Installer CR overrides krateo-observability to a version before 0.1.44 needs the same step in the override.

A skipped step is easy to miss: the installer upgrade succeeds and the CompositionDefinition reads Ready at
the new version. The refusal is in the **composition's** `Ready` condition, and the Helm release stays at the
old version. Another sign is a new `krateo-sse-proxy` pod in `CreateContainerConfigError`: it needs a Secret
this chart creates from 0.1.41, which the refused release never made. Before leaving the middle step, check
that Secret `mongodb-hyperdx-credentials` has a `uri` key.

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
