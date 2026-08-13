---
type: Example
title: Standalone install of both data-layer operators
description: Direct helm install of the clickhouse-operator and mongodb-operator wrapper charts from GHCR, with an override values file per chart.
resource: oci://ghcr.io/krateo-platformops/charts/clickhouse-operator
tags: [observability, helm, example]
timestamp: 2026-08-07T00:00:00Z
---

# Standalone install of both data-layer operators

Installs the two operators on any cluster, no Krateo dependency. The values files
show the passthrough contract: every override goes under `operator:` (the alias of
the upstream subchart).

- `clickhouse-values.yaml` — keeps the wrapper's webhook/cert-manager off (explicit)
  and switches the controller metrics endpoint to plain HTTP.
- `mongodb-values.yaml` — sets `operator.watchNamespace: "*"` so the operator
  reconciles `MongoDBCommunity` CRs cluster-wide instead of only its own namespace.

## Preconditions

- A reachable Kubernetes cluster (`kubectl config current-context`).
- Helm ≥ 3.8 (OCI support). The charts are public — no registry login.

## Run

```sh
helm install clickhouse-operator oci://ghcr.io/krateo-platformops/charts/clickhouse-operator \
  --version 0.1.0 --namespace krateo-system --create-namespace \
  -f clickhouse-values.yaml

helm install mongodb-operator oci://ghcr.io/krateo-platformops/charts/mongodb-operator \
  --version 0.1.0 --namespace krateo-system \
  -f mongodb-values.yaml
```

Verify: `kubectl get deploy -n krateo-system` shows
`clickhouse-operator-controller-manager` and
`mongodb-kubernetes-operator`; `kubectl get crd | grep -E 'clickhouse|mongodb'`
lists the three operator CRDs.
