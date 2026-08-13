---
type: ChartRepo
title: clickstack-chart — index
description: The map of the clickstack-chart doc bundle — the six Helm charts that deploy Krateo's observability stack (the ClickHouse + MongoDB operators, the ClickStack wrapper, two OTel collectors, the SSE proxy).
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [observability, clickstack, clickhouse, opentelemetry, chart-repo]
timestamp: 2026-08-07T00:00:00Z
---

# clickstack-chart

This repo is the **deployment unit** for Krateo's observability stack: six
independently-versioned Helm charts, published to `oci://ghcr.io/krateo-platformops/charts`,
that the [installer](https://github.com/krateo-platformops/installer) turns into Krateo
compositions — the ClickHouse and MongoDB operator wrappers (`clickhouse-operator`,
`mongodb-operator`) that provide the data-layer CRDs, the ClickStack wrapper
(`krateo-observability`: ClickHouse + OTel gateway + HyperDX + MongoDB), the cluster-level and
node-level OTel collectors, and the SSE proxy that feeds the portal events bell. This is the
**deployment / wiring** half of the component docs; the **internals** half (the custom
collector code, the `compositionresolver` processor, the sse-proxy binary) lives in the code
repos `krateo-platformops/otel-collector` and `krateo-platformops/sse-proxy`.

## The bundle (start here)

- [overview](./overview.md) — what the six charts deploy, how they version, and how they
  become Krateo compositions.
- [usage](./usage.md) — how the installer consumes the charts, direct `helm install oci://…`,
  and local `helm template` validation.
- [configuration](./configuration.md) — the whole per-chart values surface: the operator
  wrappers' `operator:` passthrough, the ClickStack passthrough, the `/events` handler,
  retention TTLs, collector pipelines, the single-replica sse-proxy, and the operational
  gotchas.
- [api](./api.md) — the contract: the six generated composition types, the
  CompositionDefinition, the operator CRDs the wrappers install, the HTTP surfaces (`/events`,
  SSE, HyperDX API), and the upstream CRs the data layer deploys but does not own.
- [examples](./examples.md) — the runnable examples under `examples/`.
- [release](./release.md) — how a release ships (semver tag → all first-class charts to GHCR).
- [log](./log.md) — curated history (incidents, renames, version-bump lessons).
- [llms.txt](./llms.txt) — the version-pinned agent index of this bundle.

## Deep corpus (code-adjacent, kept in place)

- [`ops/`](../ops/README.md) — deployment-adjacent reference config folded in from the former
  code repo: ClickHouse config references, HA policies, alert bootstrap scripts, reference
  collector manifests. The **live** artifacts are the charts under `charts/`; `ops/` is
  reference material (e.g. `ops/clickhouse-config/http-handlers.xml` is the historical copy of
  the `/events` handler that now lives in
  `charts/krateo-observability/values.yaml` under `cluster.spec.settings.extraConfig`).
- Code-repo internals: `krateo-platformops/otel-collector` (the custom collector image and the
  `compositionresolver` processor, versioned at the **image** tag) and
  `krateo-platformops/sse-proxy` (the SSE hub/poller). Read those at the image tag that the
  charts here pin.
