---
type: Runbook
title: clickstack-chart — release
description: How a release ships — a plain-semver tag publishes every first-class chart at its own literally-pinned version to oci://ghcr.io/krateo-platformops/charts.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [release, ci, oci]
timestamp: 2026-08-07T00:00:00Z
---

# Release

This repo uses the canonical shape-agnostic
[`release-oci.yaml`](../.github/workflows/release-oci.yaml) workflow (byte-identical across
krateo-* package repos). One tag publishes **all six charts**, each at its own version.

## The runbook

1. **Bump the changed chart's `version` in its `Chart.yaml`** — all six charts are
   *literally pinned* (no `CHART_VERSION` placeholder), so the chart version is authored in
   the PR, not derived from the tag. **This is mandatory for any behavioral change**:
   `core-provider`/helm cache charts by version tag and never re-pull an unchanged version
   ([configuration → gotchas](./configuration.md)). Keep the `Chart.yaml` comment history
   (each bump documents why). For the operator wrappers, bump `appVersion` + the aliased
   upstream dependency pin together when moving to a new upstream operator release (and
   refresh the vendored `.tgz` with `helm dependency update`).
2. **Merge to `main`** with PR CI green: [`lint.yaml`](../.github/workflows/lint.yaml)
   (helm lint + schema well-formedness + render smoke per chart, and the docs-standard
   lint), plus the org security workflow.
3. **Tag with plain semver — `X.Y.Z`, no `v` prefix.** The workflow triggers on
   `[0-9]+.[0-9]+.[0-9]+` only. Historically this repo's tags track the upstream
   clickstack line (`3.0.x`, `3.1.0`) — the tag value does not touch chart versions here
   (no placeholders), it only triggers the publish.
4. **CI publishes** every first-class chart (any `Chart.yaml` not vendored inside another
   chart's `charts/`) to `oci://ghcr.io/krateo-platformops/charts` — the artifact name is
   the **chart name**: `clickhouse-operator`, `mongodb-operator`, `krateo-observability`,
   `otel-collector-deployment`, `otel-collector-daemonset`, `krateo-sse-proxy`. Upstream
   dependencies (`clickstack`, `opentelemetry-collector`, the vendored `clickhouse-operator-helm`
   / `community-operator` `.tgz` under each operator chart's `charts/`) are
   `helm dependency build`-vendored at package time and skipped by discovery.
5. **Wire the consumers**: bump the version pin in the installer's component pins (and, for
   standalone installs, [`compositiondefinition.yaml`](../compositiondefinition.yaml)).
   A wrapper-version bump changes the composition CR apiVersion
   (`v0-1-11` style — [api](./api.md)); note that a composition chart-version bump is
   handled by cdc as an in-place handover, but stateful re-rolls of ClickHouse/Keeper are
   the risk to watch on live clusters.

## Verify

```sh
helm show chart oci://ghcr.io/krateo-platformops/charts/krateo-observability --version <X.Y.Z>
```

Image versions are pinned inside values (`otel-collector:1.0.2`, `sse-proxy:1.1.2`) and ship
from their own code repos (`krateo-platformops/otel-collector`,
`krateo-platformops/sse-proxy`) — releasing a new image requires a chart values bump here
plus a chart version bump.
