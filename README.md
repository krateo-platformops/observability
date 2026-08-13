# clickstack-chart

The deployment unit for Krateo's observability stack: four Helm charts — the ClickStack
wrapper, two OpenTelemetry collectors, the SSE proxy — published to
`oci://ghcr.io/krateo-platformops/charts` and turned into Krateo compositions by the
[installer](https://github.com/krateo-platformops/installer).

## What is this

A chart repo, not a code repo: it wraps the upstream ClickStack chart (ClickHouse + OTel
gateway + HyperDX + MongoDB) and the upstream `opentelemetry-collector` chart, folds in the
Krateo glue (the `/events` handler, the composition-id enrichment wiring, the portal-bell
SSE proxy), and ships a `values.schema.json` per chart so `core-provider` can generate typed
composition CRDs. The collector and sse-proxy **code** lives in
`krateo-platformops/otel-collector` and `krateo-platformops/sse-proxy`.
Full picture: [docs/index.md](docs/index.md).

## Install

Normally installed by the **Krateo installer** (portal feature, `tier: observability`).
Standalone, in dependency order (wrapper first — it creates the credentials Secret the
collectors mount; ClickHouse + MongoDB operators must already be installed):

```sh
helm install krateo-observability oci://ghcr.io/krateo-platformops/charts/krateo-observability \
  --version 0.1.11 --namespace krateo-system --create-namespace
helm install otel-collector-deployment oci://ghcr.io/krateo-platformops/charts/otel-collector-deployment \
  --version 0.3.3 --namespace krateo-system
helm install otel-collector-daemonset oci://ghcr.io/krateo-platformops/charts/otel-collector-daemonset \
  --version 0.1.5 --namespace krateo-system
helm install krateo-sse-proxy oci://ghcr.io/krateo-platformops/charts/krateo-sse-proxy \
  --version 0.1.6 --namespace krateo-system
```

Details and the composition path: [docs/usage.md](docs/usage.md).

## Configure

See [docs/configuration.md](docs/configuration.md). Most used:

| Setting | Default | Effect |
|---|---|---|
| `global.storageClassName` (wrapper) | `standard-rwo` | StorageClass for ClickHouse/Keeper/Mongo volumes (GKE default; override off-GKE). |
| `hyperdxLoadBalancer.enabled` (wrapper) | `true` | The extra LoadBalancer Service exposing the HyperDX UI on `:3000`. |
| `clickstack.clickhouse.cluster.spec.*` (wrapper) | 8Gi limit / 15Gi volume | The ONLY path upstream honours for ClickHouse resources/storage — top-level `clickhouse.*` keys are inert. |

## Examples

- [examples/observability-composition](examples/observability-composition) — deploy the
  ClickStack wrapper as a Krateo composition (CompositionDefinition + a `KrateoObservability` CR).

## Docs

- [docs/index.md](docs/index.md) — the map (bundle + the `ops/` reference corpus)
- [docs/overview.md](docs/overview.md) — what the four charts deploy and how
- [docs/usage.md](docs/usage.md) — installer path, standalone install, local validation
- [docs/configuration.md](docs/configuration.md) — the whole per-chart values surface
- [docs/api.md](docs/api.md) — generated composition types, HTTP surfaces, upstream CRs
- [docs/examples.md](docs/examples.md) — examples index
- [docs/release.md](docs/release.md) — how a release ships
- [docs/log.md](docs/log.md) — curated history

## Develop & release

```sh
for d in charts/*/; do helm dependency build "$d"; helm lint "$d"; helm template smoke "$d" >/dev/null; done
```

Push a plain-semver tag (`X.Y.Z`, no `v`) — CI publishes every chart at its own
literally-pinned `Chart.yaml` version. Runbook: [docs/release.md](docs/release.md).
