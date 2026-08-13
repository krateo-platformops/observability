---
type: Log
title: clickstack-chart — log
description: Curated history of the observability chart repo — incidents, load-bearing fixes, renames and structural changes, newest first.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [log, history, incidents]
timestamp: 2026-08-07T00:00:00Z
---

# Log

Curated, newest first. Release notes live in GitHub Releases; the `Chart.yaml` comment
blocks carry the per-bump rationale.

- **2026-08-13 — operator charts folded in (six charts now).** The retired
  `clickstack-operators-chart` repo's two composition wrappers — `clickhouse-operator`
  (wraps `clickhouse-operator-helm 0.0.5`, webhook + cert-manager off) and `mongodb-operator`
  (wraps `community-operator 0.13.0`, empty passthrough), both `0.1.0` — were consolidated
  into this repo, which now publishes **six** charts. These are the data-layer operators the
  ClickStack wrapper's `ClickHouseCluster`/`KeeperCluster`/`MongoDBCommunity` CRs depend on;
  the wrapper declares `deps: [clickhouse-operator, mongodb-operator]` so they install first.
  Each aliases its upstream dependency to `operator` (crdgen Kind-collision avoidance) and
  ships the upstream CRDs as `helm.sh/resource-policy: keep` templates. New examples:
  `examples/composition-registration`, `examples/standalone-operators`.
- **2026-08-07 — Documentation Standard adoption.** The bundle converted to the invariant
  OKF file set; `docs/wiring.md` and `docs/crds.md` folded into
  [configuration](./configuration.md) and [api](./api.md); the stale repo-root
  `compositiondefinition.yaml` re-pointed from the nonexistent OCI artifact
  `charts/observability@0.1.8` to `charts/krateo-observability@0.1.11`.
- **2026-08-04 — `global.imageRegistry`** (sse-proxy chart): mirror/air-gap registry-host
  override; `image.registry` split out of `image.repository`.
- **2026-08-03 — org migration.** Everything re-pointed to `krateo-platformops` (CI caller,
  OCI registry, images). The federated `clickstack-agent` that lived under `kagent/` was
  **extracted** to its own repo and is no longer part of this one.
- **2026-07-26 — wrapper `0.1.10`/`0.1.11`.** Upstream clickstack `3.0.0 → 3.0.2`
  (HyperDX 2.27 → 2.30.1, unlocking the Bearer-authenticated `/api/v2/*` external API), then
  the `krateo-clickstack-api` ClusterIP Service (`:8000`) so in-cluster clients bypass the
  `/api`-stripping Next.js proxy at `:3000`.
- **2026-07-22 — the retention incident fix** (wrapper `0.1.9`, collectors `0.3.3`/`0.1.5`).
  The 10Gi ClickHouse data volume filled in 8 days (no TTL on `otel_*` tables, ~3.6GiB of
  unbounded `system.*` logs) → inserts failed with code 243 → the collectors
  OOM-crash-looped → telemetry dead. Durable fix: 15Gi `dataVolumeClaimSpec`, 3-day
  system-log TTLs in `settings.extraConfig`, `ttl: 168h` in both collectors' clickhouse
  exporter. See [configuration → gotchas](./configuration.md).
- **2026-07-10 — collector image `1.0.2`** (chart `0.3.2`): the forked clickhouse exporter
  recreates the `otel_*` schema on reconnect (a fresh ClickHouse PVC no longer leaves the
  stack table-less until a manual collector restart).
- **2026-07-08 — PVC reaper moved out of the chart.** A composition post-delete hook does
  not run reliably under cdc uninstall; the wrapper's `0.1.7` hook was reverted in `0.1.8`.
  MongoDB PVCs are reaped chart-side via StatefulSet `persistentVolumeClaimRetentionPolicy`;
  the ClickHouse/Keeper reaper lives in the installer's ordered teardown (installer ≥ 0.2.191).
- **2026-07-04 — daemonset `0.1.4`:** headless Service for the node-local OTLP receiver — a
  stable DNS name for engine/cdc/app OTLP export without consuming a Service-CIDR address.
- **2026-07-02 — prometheus receiver nulled** (deployment collector). The minimal
  `krateo-otel-collector` image compiles in only `k8sobjects` + `k8s_cluster`; merely
  *declaring* the prometheus receiver crashlooped the default install. The Prometheus→OTLP
  bridge (added 2026-06-29) remains scaffolded-but-off.
- **2026-06-22 — OTLP ingest wired** (daemonset `0.1.2`/`0.1.3`): `otlp` added to the
  daemonset's traces and metrics pipelines (KOS-1 #112) — the path platform components'
  OTLP telemetry takes into ClickHouse. sse-proxy `0.1.4` added the fixed-name
  `sse-proxy-internal-endpoint` Secret for snowplow RESTActions.
- **2026-06-20 — chart renamed** `krateo-clickstack` → `krateo-observability` (composition
  kind `KrateoObservability`). Chart renames change the generated CRD kind — they are
  API-breaking for compositions.
- **2026-06-18 — `/events` moved to `settings.extraConfig`** (wrapper `0.1.6`). The
  http-handlers ConfigMap + `clickhouse.extraVolumes` mount was inert (upstream renders only
  `cluster.spec`; the CRD has no `extraVolumes`) and `/events` 404'd. The operator-native
  config merge is the only working path; ClickHouse default routes are re-declared alongside.
- **2026-06-16 — bundled OpAMP otel-collector disabled** (`clickstack.otel-collector.enabled:
  false`): it crashlooped (OpAMP supervisor mode ignores the chart `--config`) and duplicated
  Krateo's own ingestion without `krateo.io/composition-id`.
- **2026-06-15 — the inert-resources OOM + the mutable-version lesson.** ClickHouse
  resources moved to `cluster.spec.containerTemplate.resources` (the only path upstream
  honours; the old top-level `resources:` left ClickHouse at ~1Gi and the bell
  `/notifications` query OOMed). The fix had earlier been overwritten onto the
  already-published `0.1.2` — consumers cache by version tag and never re-pulled — so it was
  re-shipped as `0.1.3`: **a behavioral fix MUST ride a version bump.** Same day: sse-proxy
  pinned to a single replica (stateful in-memory SSE hub), and the collector/sse-proxy code
  split out to their own repos (`ops/` folded in here as reference material).
