---
type: Example
title: Deploy the ClickStack wrapper as a Krateo composition
description: Register the krateo-observability CompositionDefinition, then create a KrateoObservability CR with a minimal block-style values override.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [composition, clickstack, observability]
timestamp: 2026-08-07T00:00:00Z
---

# Deploy the ClickStack wrapper as a composition

Registers the wrapper chart with `core-provider` and creates one instance. This is the
standalone path — under a full installer deploy the installer owns the
CompositionDefinition and you create nothing by hand ([usage](../../docs/usage.md)).

## Preconditions

- `core-provider` running (a stock Krateo installer deploy, `bootstrap` profile is enough).
- The **ClickHouse operator** and the **MongoDB community operator** installed — the chart
  renders a `ClickHouseCluster` and a `MongoDBCommunity` CR but ships neither CRD
  ([api](../../docs/api.md)). Under the installer these are the `clickhouse-operator` and
  `mongodb-operator` components.
- A `krateo-system` namespace, and a StorageClass matching
  `spec.global.storageClassName` in [composition.yaml](./composition.yaml) (`standard`
  there, for kind; the chart default is GKE's `standard-rwo`).

## Apply

```sh
kubectl apply -f ../../compositiondefinition.yaml
# wait for core-provider to generate the CRD, then:
kubectl apply -f ./composition.yaml
```

## Verify

```sh
kubectl get compositiondefinition krateo-observability -n krateo-system   # Ready: True
kubectl get krateoobservabilities.composition.krateo.io -n krateo-system
kubectl get pods -n krateo-system -l app.kubernetes.io/instance --show-labels | grep clickstack
```

The composition installs the wrapper chart: ClickHouse (+Keeper), HyperDX, MongoDB, the
`otel-clickhouse-credentials` Secret and the `krateo-clickstack-api` Service. The
apiVersion in `composition.yaml` (`v0-1-11`) tracks the pinned chart version — if you bump
the CompositionDefinition's `spec.chart.version`, regenerate the CR accordingly.
