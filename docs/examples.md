---
type: ExampleIndex
title: clickstack-chart — examples
description: Runnable examples under examples/, each paired with a README stating preconditions and the one apply command.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [examples, composition]
timestamp: 2026-08-07T00:00:00Z
---

# Examples

Each example is a runnable manifest + a README with preconditions and the one apply command.

- [observability-composition](../examples/observability-composition/README.md) — deploy the
  ClickStack wrapper as a Krateo composition: register the
  [`CompositionDefinition`](../compositiondefinition.yaml), then create a
  `KrateoObservability` CR with a minimal block-style values override.
- [composition-registration](../examples/composition-registration/README.md) — register the
  two operator wrapper charts with `core-provider` (a `CompositionDefinition` per chart) and
  instantiate a `ClickhouseOperator` composition CR, no installer involved.
- [standalone-operators](../examples/standalone-operators/README.md) — direct
  `helm install oci://…` of both operators on any cluster, with an override values file per
  chart (cluster-wide MongoDB watch, ClickHouse plain-HTTP metrics).

The reference manifests under [`ops/`](../ops/README.md) (ClickHouse config, HA policies,
alert bootstrap) are operational material, not examples — the live artifacts are the charts.
