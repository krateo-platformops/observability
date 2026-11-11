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

- **2026-11-11 — upstream's dead MongoDB default emptied (krateo-observability 0.1.46).** 0.1.44 had to leave upstream clickstack's public default in `krateo-clickstack-mongodb-password` and in `clickstack-secret`'s `MONGODB_PASSWORD`. MCK watches the Secret the CR references, and Helm updates Secrets before the CR, so emptying it in the upgrade that moved the user off it could have set an empty password for a moment. Nothing has read either since then, and MongoDB refuses the value. 0.1.46 sets it to "", and the template refuses a non-empty value. An upgrade on a cluster that hasn't run 0.1.44 is refused, because there emptying is exactly the unsafe case. The guard reads `mongodb-hyperdx-credentials` (it must exist with a `uri`), not the MongoDBCommunity: the per-version composition controller may get Secrets but not MongoDBCommunity objects on 057 or demo, and a `lookup` leaves nothing in the rendered output for rbacgen to grant from (installer's read). An install passes, since its CR starts on the new Secret. `check-hyperdx-mongo-credential` runs the guard against a kind API server: install with no Secret renders; upgrade with no Secret, or with one lacking `uri`, is refused; upgrade with `uri` renders. No pod restarts.
- **2026-11-11 — krateo-audit-worm's ClickHouse client pinned by exact tag (0.1.7).** The seal and verify Jobs ran `clickhouse-server:24.8-alpine`, a floating tag that pulls each new 24.8 build on the next pod. It's now `24.8.14.39-alpine`, the build that tag gave 057's Jobs that day, so nothing that runs changes. `check-clickhouse-image-pin` now checks that image as well.
- **2026-11-11 — ClickHouse and Keeper pinned by exact tag, 26.10.3.7 (krateo-observability 0.1.45).** 0.1.43 meant to pin ClickHouse 26.8.36.3 by digest, with `tag: null` and `hash` set. ClickHouse operator v0.0.7 defaults an empty tag to `latest` before it chooses between tag and hash, so the hash was never used, and the 0.1.44 roll on krateo-057 moved ClickHouse to `clickhouse-server:latest`, 26.10.3.7 that day. Keeper had never been pinned and has run `latest` since install. 057 was healthy on 26.10.3.7 (no failed queries, ingestion running, the 15 users in place), so Diego chose to pin that version rather than go back. Both CRs now carry `tag: "26.10.3.7"` and no hash. The binaries don't change, so the roll restarts ClickHouse and Keeper once each. `check-clickhouse-image-pin` renders the chart, applies the operator's defaulting to both CRs and fails on an empty or inexact tag; the ClickHouse tests run 26.10.3.7. Lesson: a pin is checked against the image the StatefulSet runs, not against the values file.
- **2026-11-11 — HyperDX's MongoDB credential is generated and rotated (krateo-observability 0.1.44).** It was upstream clickstack's public default, and the full `MONGO_URI` with it sat in plain text in the `clickstack-config` ConfigMap. The chart now generates `mongodb-hyperdx-credentials` once and keeps it across upgrades (`username`, `password`, `uri`). The MongoDBCommunity user reads its password from that Secret, HyperDX reads `MONGO_URI` from it through `secretKeyRef`, and the ConfigMap no longer has the key. Upstream's default stays in `krateo-clickstack-mongodb-password` and in `clickstack-secret`'s `MONGODB_PASSWORD`, unreferenced and dead once the user is rotated. A follow-up release empties both once nothing references the old Secret. It can't happen in this release: MCK watches the Secret the CR references, and Helm updates Secrets before the CR, so emptying it now could briefly set an empty password. The first upgrade gives MongoDB the new password in place and rolls HyperDX. A new init container holds the new pod until MongoDB accepts the new URI, because HyperDX 2.35.0 stays Ready without a database when its one connect attempt fails. Rotate by raising `krateo.io/mongodb-credential-generation`. 0.1.44 contains 0.1.43's ClickHouse digest pin, so rolling it also restarts ClickHouse once. `check-hyperdx-mongo-credential` runs the rendered init container and URI against MongoDB 5.0.32.
- **2026-11-11 — the agent gateway's audit record in `krateo_audit` (otel-collector-daemonset 0.1.27, AUDIT-4, #138).** `logs/gateway-audit` projects the acting hops of the agent gateway's OTLP access log (A2A calls, MCP `tools/call`/`resources/read`/`prompts/get`, LLM completions) into `krateo_audit` as writer `gateway` of krateo-platformops/audit schema v1, while `logs/gateway` keeps every access-log record in `otel_logs` as before. The rest, MCP session plumbing, agent-card fetches, model listing and requests that matched no route, is excluded on an explicit list and counted per protocol and method (`krateo.gateway_audit.excluded`), and an MCP method on neither list is projected rather than dropped (snowplow's ruling as AUDIT-4 lead). A refused hop names the target it would have reached (the route's configured backend, agentgateway-policies 0.2.28). The test feeds the real agentgateway v1.5.0's access-log output through the rendered pipelines and checks every record with the library's `auditschema check`: no reason but a marked gap, markers in the library's order, excluded hops all counted, falsifiers reported. Expected volume on 057 is ~16 MB a year.
- **2026-11-11 — ClickHouse users for the `default` lockdown (krateo-observability 0.1.42).** `analyst`, `mcp_reader`, `nightly_reader`, `audit_sealer`, `audit_verifier`, `otel_writer`, `hyperdx` and `slo_rollup` exist beside 0.1.41's portal readers, each with a profile, one per-table GRANT statement and a chart-generated Secret; `clickhouse-default-password` is created for later. 0.1.42 contains 0.1.41 (which was held), restarts ClickHouse once for the new environment variables, and switches no client: `default`, `app` and `otelcollector` are unchanged. Grants and profiles are the ones proven on kind against 057's DDL and the real audit-worm Jobs (2026-11-10); `check-clickhouse-users` loads the rendered config into ClickHouse 26.8.36.3 on every change.
- **2026-11-10 — read-only ClickHouse users for portal reads (krateo-observability 0.1.41).** `sse_proxy` and `snowplow_reader` exist with per-table SELECT grants and a `portal_reader` profile: `readonly=2`, cancel-on-close, an 8 s limit, 2 threads, and a per-user cap of 16 and 8 queries. Passwords live in Secrets the chart generates; ClickHouse reads the hash from the environment. The clients still use `default` until they switch. Tested on kind with ClickHouse 26.8.36.3 and operator 0.0.7: grants, refused writes and refused tables, settings, constraints, and cancellation of a query whose client went away (3 s, against 7 s for `default`).
- **2026-11-09 — OTEL-8's audit-to-reconcile join (`ops/audit-reconcile-join/`).** A query lists, for each portal write in `krateo_audit`, the composition-controller reconciles of the same object within a window, with the actor named and any reconcile two writes can claim flagged as ambiguous (installer#400). It joins on the controller name core-provider sets (`<resource>-<version>-controller`) rather than on a Kind guessed from the plural, which real data breaks (`agentikoes`). It's tested in ClickHouse 26.8.36.3 against 057's own DDL (`check-audit-reconcile-join`), since 057 has no reconcile spans until core-provider's `otel.enabled`.
- **2026-11-07 — `sse-proxy` 1.4.2 (krateo-sse-proxy 0.1.23).** Each client's buffer holds one full poll, 500 events, so ordinary bursts no longer drop events for a client that has kept up. On 1.4.1, polls over 64 rows dropped about 1 event in 9 (sse-proxy#30).
- **2026-11-07 — `sse-proxy` 1.4.1 (krateo-sse-proxy 0.1.22).** The stream is live events: the cursor starts 30 seconds back and stays there while idle, so the first client no longer gets an hour of backlog into a 64-slot buffer. A client with no topic no longer gets a row twice (sse-proxy#30).
- **2026-11-05 — `sse-proxy` 1.4.0 (krateo-sse-proxy 0.1.21).** Refuses a token in the query string
  on every path, even with valid credentials, and answers CORS preflights before authentication.
  Without that, a portal on another origin can't connect unless a gateway answers them, which left the
  bell down on a0-openstack-d3. See krateo-platformops/sse-proxy#40.

- **2026-11-05 — HyperDX self-telemetry reaches a real collector (krateo-observability 0.1.28).** Its
  endpoint named the bundled collector subchart this chart disables, so 0 spans arrived in 7 days. It
  now uses the node-local agent. Logs stay off OTLP because `filelog` already has them, and traces are
  sampled at 5%. Run against HyperDX 2.35.0 locally: 0 OTLP logs, sampling honoured, service name
  correct. Never unset the endpoint to disable this: the SDK then defaults to `in-otel.hyperdx.io`,
  and `HYPERDX_API_KEY` is set.

- **2026-11-05 — alert exclusions name the real agent (krateo-observability 0.1.27).** Six alert
  filters excluded `krateo-autopilot`, which matches no rows; the agent's `ServiceName` is `autopilot`.
  No default alert's count changes today, but the `ops/` error queries, which have no text filter,
  stop counting its rows. `sre-jwt-in-telemetry` loses its `ServiceName` exclusion: it hid the services
  most likely to leak a token, and its regex cannot match its own query. This ships separately from
  the `agentiko` rename, and both names must stay listed across that roll.

- **2026-11-05 — JWT redaction on `k8sobjects` (otel-collector-deployment 0.3.13, daemonset 0.1.18).**
  The `k8sobjects` stream was the one log pipeline without redaction (installer#394). Image 1.0.5
  adds `transformprocessor`, and installer-managed clusters get this through the pin bump. The 0.3.9
  statements are not restored, because they would not have worked: `k8sobjects` bodies are nested
  maps, which `replace_pattern(body, ...)` leaves untouched. Both collectors now target `body.string`,
  and a record is converted only when the pattern matches. The daemonset change is precautionary:
  every OTLP body on krateo-057 is a string, and 0 JWT-shaped values were found in `krateo_audit`.

- **2026-11-05 — the JWT detector sees `k8s-events` (krateo-observability 0.1.26).**
  `sre-jwt-in-telemetry` was scoped to `telemetry.source = ''`, which excluded the one stream
  without redaction, so a leak there could not fire it. No leak occurred: 0 JWT-shaped values in
  about 26.5M rows over 7 days. Removing the scope catches both body and attribute cases on
  synthetic rows, and still ignores redacted rows and the observability stack.

- **2026-11-05 — severity and trace context from JSON logs (otel-collector-daemonset 0.1.17).**
  `SeverityText` was empty on every filelog row, and `TraceId` could not be set, because the
  pipeline never parsed JSON (#54). Guarded operators now promote `level` or `severity_text`,
  `trace_id` and `span_id`. `Body` stays raw because alerts, the portal and nightly-review read it,
  and the parsed copy is removed in the receiver so storage stays flat. Case is normalised in the
  operators and not in `transform`, which also runs on `logs/audit`, and `krateo-audit-worm` hashes
  `SeverityText`. The `ops/` error queries move to `SeverityNumber >= 17`, so a panic counts.
  Correlation for snowplow also needs snowplow#1184: 0 of 96,315 of its rows carried `trace_id`.

- **2026-11-05 — daily SLO rollup (krateo-slo-rollup 0.1.0).** Every `otel_*` table keeps 7 days,
  so the 28-day objective had nothing to read. `krateo_slo_daily` keeps per-day call counts and
  duration buckets for 40 days. The view is **refreshable**, not insert-triggered: measured on
  26.8, an ordinary view's failure fails the exporter's insert, and
  `materialized_views_ignore_errors` is only honoured in the writer's session, not in the view.
  See [SLO → 28-day rollup](SLO.md#28-day-rollup).

- **2026-11-03 — abandoned interactions leave the SLIs (otel-collector-daemonset 0.1.16).**
  `filter/slo-spans` drops spans with `krateo.interaction.abandoned == true` (bool), per acmp's
  X5 span contract: excluded, not counted as errors, and still in `otel_traces`. acmp's
  `abandoned_flag_selfcheck` guards against a frontend bug hiding real slowness this way. Inert
  until snowplow #1061 (expected 1.12.65) forwards the flag. See
  [SLO → span metrics](SLO.md#span-metrics-the-inputs-for-performance-alerts).

- **2026-11-03 — SLO span metrics (otel-collector-daemonset 0.1.15).** Performance alerts are
  built from the `span_metrics` connector (decision D5). The node-local agent gains a
  `traces/slo` pipeline that keeps only `snowplow` SERVER spans and `krateo-frontend`
  `snowplow/rum-ingest` spans, and a `metrics/slo` pipeline that writes
  `traces.span.metrics.calls` and `traces.span.metrics.duration` (ms, buckets 25 … 10000) to
  `otel_metrics_sum` / `otel_metrics_histogram` through the existing exporter. `/refreshes` and
  `/telemetry/v1/traces` are dropped from the SLI spans, and `krateo.sli.error` (`true` for 5xx
  or no response, `false` for 4xx) is a dimension, because alert-provider labels can only match
  exact values. DELTA temporality, because the connector runs once per node. The `traces`
  pipeline is unchanged. The chart's `appVersion` is now `0.158.0`, the collector it renders. Names and
  attribute keys for `Metric` CRs: [SLO → span metrics](SLO.md#span-metrics-the-inputs-for-performance-alerts).

- **2026-11-02 — chart `sources` name this repo by its current name.** The four charts that
  listed `github.com/krateo-platformops/clickstack-chart` (this repo's old name, which only
  redirects) now list `github.com/krateo-platformops/observability`: wrapper `0.1.25`,
  collectors `0.3.12`/`0.1.14`, sse-proxy `0.1.18`. Metadata only, no rendered change; bumped
  anyway so the next tag does not overwrite a published version with different metadata.

- **2026-11-01 — the incident pipeline's exclusion covers its new name (0.1.24).** The
  log-scoped alerts exclude ServiceName `incident-provider` next to `incident-controller`, ahead
  of the controller's rename, so the 0.1.20 loop stays closed whichever name a cluster's build
  logs under. See [alerts.md](alerts.md#the-three-rules).

- **2026-10-05 — the default alerts stop alerting on the incident pipeline (0.1.20).** The
  log-scoped alerts exclude ServiceName `incident-controller`, whose own `Reconciler error` lines
  kept `sre-krateo-composition-reconcile-error` firing every minute on krateo-057, and
  `sre-volume-attach-failure` skips the incident-check pods' events in `krateo-incident-checks`.
  Measured counts in [alerts.md](alerts.md#the-three-rules).

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
