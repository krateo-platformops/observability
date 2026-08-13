---
type: Example
title: Composition registration via core-provider
description: Register the clickhouse-operator and mongodb-operator charts as CompositionDefinitions and instantiate a ClickhouseOperator composition CR, without the installer.
resource: compositiondefinitions.core.krateo.io
tags: [observability, core-provider, composition, example]
timestamp: 2026-08-07T00:00:00Z
---

# Composition registration via core-provider

Consumes the charts the way the Krateo installer does, but by hand: a
`CompositionDefinition` per chart (core-provider generates the `ClickhouseOperator`
/ `MongodbOperator` CRDs from each chart's `values.schema.json`), then one
`ClickhouseOperator` CR whose creation helm-installs the wrapper chart.

## Preconditions

- A cluster with **core-provider** running and the
  `compositiondefinitions.core.krateo.io` CRD served (any Krateo install provides
  this).
- The `krateo-system` namespace exists.

## Run

```sh
kubectl apply -f manifest.yaml
```

A fresh registration generates the composition CRD asynchronously — if the
`ClickhouseOperator` CR is rejected on the first pass, wait for
`kubectl get compositiondefinition -n krateo-system` to show `READY True`, then
re-apply. Verify the operator landed:

```sh
kubectl get clickhouseoperators.composition.krateo.io -n krateo-system
kubectl get deploy -n krateo-system | grep controller-manager
```

Note: the Krateo installer manages these same objects when `features.portal` is on —
don't apply this example on a cluster where the installer already owns
`clickhouse-operator` / `mongodb-operator` components.
