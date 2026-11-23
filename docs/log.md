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

- **2026-11-23 — a replicated path for audit records, off by default (otel-collector-daemonset 0.1.38).** While a node's agent restarts, its OTLP senders' connections are refused, and agentgateway (1.5.0, 1.7.0) and Go's otlploghttp 0.21.0 drop what they were sending: 86 to 106 of 300 and 79 to 80 of about 249 records in local 8 to 10 s outages, 0 without one. `auditCollector` (Diego, 2026-11-23) is two copies behind one Service, replaced one at a time, with its config cut out of the agents' own so the audit rules stay in one place. In kind, into ClickHouse 26.10.3.7, a roll and a pod deletion lost 0 of 1,200; one copy lost 5 to 19. A crash can still lose records. Nothing uses it until a sender's OTLP logs endpoint points at it.
- **2026-11-23 — a collector restart no longer drops container logs (otel-collector-daemonset 0.1.37).** The agent read container logs with upstream's `start_at: end` and no saved position, so every restart began at the end of each file and the lines written meanwhile never reached `otel_logs`, with no error anywhere. It showed on krateo-057 because agentgateway's controller numbers its pushes: 9 are missing between 07:16:09Z and 07:16:17Z on 2026-11-23, when the 0.1.36 roll restarted that node's agent, and 6 at the 2026-11-18 roll; a day without a roll has none. The agent now checkpoints on the node (`storeCheckpoints`), as root with everything else taken away (Diego, 2026-11-23). In the pinned image a stop of about 6 s under load loses 0 of 2,066 lines, against 1,032 of 1,890 without checkpoints. The roll that installs 0.1.37 still loses one last slice, because there is nothing saved to resume from.
- **2026-11-21 — the collector reads agentgateway 1.6+'s access log (otel-collector-daemonset 0.1.36).** agentgateway 1.6 renamed its access log's OTLP scope (`request` → `agentgateway.access`) and its HTTP keys to OTel semconv (`client.address`, `http.request.method`, `http.response.status_code`, `server.address`, `url.path`, `network.protocol.version`). On 0.1.35 a 1.6+ gateway's access log would have been dropped before `otel_logs`, and `krateo_audit` would have got no gateway rows, with no error anywhere. The agent gateway goes to 1.7.0 (Diego, 2026-11-21) for its error status on failed provider calls, so this ships first and works on today's 1.5.0. `otel_logs` takes both scopes as sent; the audit pipelines adapt a 1.6+ record to the < 1.6 names on entry, deleting the three keys that are also projection outputs (`http.request.method`, `http.response.status_code`, `server.address`) so none survives with the gateway's raw value. The TCP peer in `krateo.request.source_ips` now keeps a bare IPv6 address whole. `tests/gateway-audit-docker.py` runs every case in both shapes and requires identical projections and excluded counts; the 1.6+ shape is derived from the real v1.5.0 fixture by `tests/agentgateway_access_log_v16.py`, which mirrors agentgateway v1.7.0's `semconv.rs`.

- **2026-11-17 — a default receiver a pipeline names is kept (otel-collector-deployment 0.3.23, otel-collector-tailsampling 0.1.17, otel-collector-daemonset 0.1.35).** The three wrappers left out an upstream default receiver whenever it still held upstream's default. A pipeline that named one without configuring it (prometheus on the agent's metrics pipeline, otlp in a traces pipeline on the deployment collector, zipkin on the tail sampler) then pointed at an undeclared receiver, and the collector would refuse to start; `helm template` cannot see that. The rule is now: left out only while it equals upstream's default and no pipeline names it (an extension: while `service.extensions` does not name it). Default pipelines the wrappers leave out are dropped first, so their receivers do not count. The default renders of the deployment and tail-sampling collectors are the same byte for byte, `checksum/config` included. `tests/collector-helm4-render.py` names one such receiver per wrapper and requires it in the config with upstream's default, under Helm 3 and 4.
- **2026-11-17 — the node agent declares only receivers it runs (otel-collector-daemonset 0.1.35).** Upstream's default `prometheus` receiver, a self-scrape of the collector's own telemetry on `:8888`, was declared in the agent's config but named by no pipeline, so it never started. It is now left out like jaeger and zipkin in 0.1.34: by the chart's ConfigMap template, only while it still equals upstream's default, so a `prometheus` receiver set with a scrape config of your own is kept, and `prometheus/agentgateway` is untouched. Template only, no values change; the config loses that one receiver, so `checksum/config` changes and the agents roll once. `tests/collector-helm4-render.py` now fails, in every collector chart, on a declared receiver no pipeline names, a pipeline receiver that is not declared, or an extension declared but not in `service.extensions` (or the reverse).
- **2026-11-17 — the collectors open only the ports something listens on (otel-collector-deployment 0.3.22, otel-collector-daemonset 0.1.34).** Upstream opentelemetry-collector opens a container port and a Service port (and on the daemonset a hostPort on every node) for each of its default receivers: otlp 4317/4318, jaeger 6831/14250/14268, zipkin 9411. The deployment collector runs none of them (its receivers, k8sobjects and k8s_cluster, read the Kubernetes API), and the daemonset ran otlp but never jaeger or zipkin, which were declared and named by no pipeline. A port with no started receiver behind it refuses every connection, so closing it cannot break a working path; on krateo-057 the agents' logs show only the otlp gRPC and HTTP servers starting, and the deployment collector's none. The deployment collector now sets every one of those `ports.<name>.enabled: false` and `service.enabled: false`, since a ClusterIP Service with no ports is invalid. The daemonset closes the jaeger and zipkin ports and also leaves the two receivers out of its config, through its own ConfigMap template (the 0.3.21 design: `configMap.create: false`, `configMap.existingName`, its own `checksum/config`), so a pipeline that later names one cannot start a receiver no port reaches. Every other receiver stays, the prometheus self-scrape included. The deployment collector's config and `checksum/config` are unchanged; its pod rolls once for the pod-template ports. The daemonset's config loses the two receivers, so its pods roll once. `tests/collector-helm4-render.py` now fails, for every chart, on a rendered port with no started listener and on a started listener with no port.
- **2026-11-17 — the collector wrappers render under Helm 4 (otel-collector-deployment 0.3.21, otel-collector-tailsampling 0.1.16).** Both wrapped the upstream opentelemetry-collector chart and left out its default jaeger, otlp, prometheus and zipkin receivers and default pipelines with `null` in `values.yaml`. Helm 4.2 and later drop those nulls before the subchart's defaults merge in (upstream Helm change 31979, the same one behind krateo-observability 0.1.54), so under Helm 4.3.2 they came back into the collector config: in the deployment collector a `prometheus` receiver its image does not compile in. Each wrapper now renders the collector's ConfigMap itself (`templates/configmap.yaml`) and upstream mounts it (`configMap.create: false`, `configMap.existingName`); the template takes `opentelemetry-collector.config` as Helm merged it, leaves out each of those upstream defaults while it still holds upstream's default value, and builds the result with upstream's own config template. No null is left in either `values.yaml`. Under Helm 3 the render is the same object for object, the ConfigMap's name, labels and `checksum/config` value included, so the pods do not restart; under Helm 4 it is the same as under Helm 3. Overrides under `opentelemetry-collector.config` keep their meaning, and a value set at one of the left-out paths is kept. `tests/collector-helm4-render.py`, run by `check-helm4-render`, renders the three collector charts under both and fails on a difference, on any of those upstream defaults, or on a `checksum/config` that does not follow the config; against the previous charts it fails under Helm 4. otel-collector-daemonset had no null and renders the same under both; it is checked, not changed.
- **2026-11-17 — no internal work-item or plan ids in public text.** The public-text rule now covers internal ids of every kind, not only the ones it named: tracker keys (capitals, a dash, a number), one-letter plan-step or finding codes, a decision id with a letter suffix, and a numbered lockdown, phase or stage. Comments and docs that used them now say what the work is or cite the public issue, with the same facts and numbers. `hack/public-text-check.py` fails on each shape; token families that are always technical (CVE and RFC numbers, UTF-8, SHA-256, S3) pass anywhere, and `hack/public-text-allowlist.txt` lists, per file and with a reason, the tokens that are really a variable, a code or a name (test time variables, an slog level, a regex character class, the Apache License URL). Earlier history is not rewritten. Charts bumped for comments only: krateo-observability 0.1.55, krateo-sse-proxy 0.1.28, otel-collector-daemonset 0.1.33, otel-collector-deployment 0.3.20, otel-collector-tailsampling 0.1.15 (its chart description too).
- **2026-11-17 — krateo-observability renders under Helm 4 (0.1.54).** `helm template` of 0.1.52 and 0.1.53 failed under Helm 4.3.2 at the `MONGO_URI` guard, and the guard was right: Helm 4.2 and later drop a null in a parent chart's own `values.yaml` before the subchart's defaults merge in (helm/helm#31979; reported upstream as helm/helm#32132), so `hyperdx.config.MONGO_URI: null` no longer deleted upstream's URI, and with the guard off, clickstack-config carried it with upstream's default password. Helm 3 (3.19, and 3.20, which the platform renders with) deletes it, so nothing deployed was affected. `MONGO_URI` and `HYPERDX_OTEL_EXPORTER_TABLES_TTL` are now empty strings, the same in both versions, and the guard refuses a non-empty `MONGO_URI`. `settings.logger` stays null (no value means "no logger" in both), so under Helm 4 the ClickHouseCluster spec gets upstream's logger; `check-helm4-render` renders under Helm 3.20.2 and 4.3.2 and allows only that difference. The otel-collector-deployment and otel-collector-tailsampling charts null upstream receivers the same way and render them back under Helm 4; not changed here.
- **2026-11-17 — public text stays neutral (`check-public-text`).** This repository is public, so its text names no internal work-item or decision ids, no private repository (name, issue numbers or paths) and no internal team short name; SLO targets may still be stated. Existing files were reworded with the same facts and numbers, and the performance-test directory lost its id prefix (now `ops/perf/2026-11-10/`). `hack/public-text-check.py` (workflow `check-public-text`) fails on any of them in a tracked file's content or path, and in a pull request's title, body or commits; `hack/public-text-allowlist.txt` lists, with reasons, the few tokens that only look like decision ids. Earlier history is not rewritten. The canonical SLI file's comments changed, not its statements. Charts bumped for comments only: krateo-observability 0.1.53, krateo-slo-rollup 0.1.4, krateo-sse-proxy 0.1.27, otel-collector-daemonset 0.1.32, otel-collector-deployment 0.3.19, otel-collector-tailsampling 0.1.14.

- **2026-11-17 — the SLO metrics and alerts become chart defaults (krateo-observability 0.1.52).** The SLO objects proven on krateo-057 on 2026-11-10, `slo-span-calls`, `slo-span-duration`, `cmp-slo-api-p95`, `cmp-slo-api-error-ratio` and `cmp-slo-metrics-overflow`, were applied there by hand from the CMP team's SLO generator. Diego decided the chart ships them, and the CMP team asked that it be their only source. They keep their names, so the roll adopts the live CRs and alert-provider keeps their HyperDX alerts; the composition controller on 057 upgrades with `TakeOwnership`, and [alerts.md](alerts.md#adoption-on-krateo-057) has the read-only pre-check and the ownership step a plain `helm upgrade` needs. The generator's output is committed as a fixture and `check-slo-defaults` fails on any drift between it and the chart. The template gained `ratio`, and stopped casting a threshold to an integer, which would have turned 0.001 into 0.
- **2026-11-16 — ClickStack 3.5.0; HyperDX's dashboard provisioner stays off (krateo-observability 0.1.49).** The clickstack subchart moves 3.2.0 → 3.5.0 (HyperDX 2.35.0 → 2.39.1). It was taken for upstream's new dashboard provisioner, but that can't be scoped away from `krateo-system`: upstream's `effectiveNamespaces` always adds the release namespace, so turning it on lets HyperDX read every ConfigMap there (319 on krateo-057). Granting only a dedicated namespace fails differently: the watcher sidecar is refused on its first sync of `krateo-system`, crash-loops, and takes the HyperDX pod out of its Service (kind). So it stays off, the render fails if a value turns it on, and it comes on in a dedicated namespace once an upstream release allows it (Diego). The pod keeps the namespace's `default` ServiceAccount; no Role, no sidecar. Two upstream additions are nulled so ClickHouse and `clickstack-config` stay as they were (the operator's `settings.logger`, `HYPERDX_OTEL_EXPORTER_TABLES_TTL`). `tests/clickstack-upgrade-kind.py` installs 0.1.48 on kind, writes ClickHouse rows and HyperDX objects the way alert-provider does, upgrades, and checks: every user's `SHOW GRANTS` byte-identical, `cmp` there, row hashes unchanged, ClickHouse, Keeper and MongoDB not restarted, the team, access key, dashboards (tile ids) and alert unchanged, alert-provider's PUT/POST calls and `charts/series` working on 2.39.1, the same ServiceAccount and `kubectl auth can-i` answers as before, and a labelled ConfigMap not provisioned. Upstream's sidecar limit (64Mi) OOMKilled it on kind; that matters when it is turned on.
- **2026-11-16 — `analyst` reads `cmp.health_records` and `showback.usage_records` (krateo-observability 0.1.48).** The ad-hoc query user gets two more SELECT grants, appended to its one GRANT statement, so ops can read per-tenant health (cmp-usage-collector's rows) and usage (showback's) without `default`. Nothing else in `cmp` or `showback`, still no sealing tables or `system.query_log`. `showback` exists only where the showback component runs; ClickHouse 26.10.3.7 loads the grant without the database, logs no error, and applies it once the table appears, which `check-clickhouse-users` now checks in both states with both components' real DDLs. Every other user's grants are byte-identical, pinned in `tests/fixtures/clickhouse-users-grants.json`. Decision: Diego 2026-11-16.
- **2026-11-16 — ClickHouse user `usage_collector` and the `cmp` database (krateo-observability 0.1.47).** The CMP team's cmp-usage-collector writes service health rows to `cmp.health_records`. Its user has one grant, `INSERT, CREATE TABLE ON cmp.health_records`: it creates its own table with `CREATE TABLE IF NOT EXISTS` and inserts, and can't read, create a database or touch `default`. The platform creates `cmp` instead, with a ClickHouse startup script (`CREATE DATABASE IF NOT EXISTS cmp ENGINE = Atomic`), which the server runs on every start before it opens its client ports. Nothing in the chart created a database before, and the image's `CLICKHOUSE_DB` runs only on an empty data directory, so it would never run on 057. A Job would have needed a second principal with CREATE DATABASE. The Secret is generated and kept like the other users', and the render now fails if any variable but a user's own reads that user's Secret (krateo-slo-rollup 0.1.1 once read the collector writer's). The new environment variable restarts ClickHouse once. Decision: Diego 2026-11-16.
- **2026-11-11 — krateo-audit-worm: a failed query is an error, never an answer (0.1.8).** During 057's ClickHouse restart (19:15Z) the seal Job failed with a refused connection, and logged it as "source table krateo_audit does not exist yet" (installer's read). seal.sh tested `[ "$(has T)" != "1" ]`, and `set -e` doesn't see a failure inside a test, so a failed query read as an empty answer. The same shape guarded the chain RESET: one failed row count on a chain without seals would have restarted it from a reset genesis. The Job still exited 1 both times, so no seal was skipped silently. Every answer is now assigned before it's tested, and `has` refuses anything but 0 or 1. `tests/verify-docker.sh` now runs both scripts against an unreachable ClickHouse (the Job fails, the log names the connection error, verify writes no status line), with a really absent table as a control. It also refuses any query inside a test. Against 0.1.7's scripts it fails twice. Its image is the chart's pinned 24.8.14.39-alpine.
- **2026-11-11 — upstream's dead MongoDB default emptied (krateo-observability 0.1.46).** 0.1.44 had to leave upstream clickstack's public default in `krateo-clickstack-mongodb-password` and in `clickstack-secret`'s `MONGODB_PASSWORD`. MCK watches the Secret the CR references, and Helm updates Secrets before the CR, so emptying it in the upgrade that moved the user off it could have set an empty password for a moment. Nothing has read either since then, and MongoDB refuses the value. 0.1.46 sets it to "", and the template refuses a non-empty value. An upgrade on a cluster that hasn't run 0.1.44 is refused, because there emptying is exactly the unsafe case. The guard reads `mongodb-hyperdx-credentials` (it must exist with a `uri`), not the MongoDBCommunity: the per-version composition controller may get Secrets but not MongoDBCommunity objects on 057 or demo, and a `lookup` leaves nothing in the rendered output for rbacgen to grant from (installer's read). An install passes, since its CR starts on the new Secret. `check-hyperdx-mongo-credential` runs the guard against a kind API server: install with no Secret renders; upgrade with no Secret, or with one lacking `uri`, is refused; upgrade with `uri` renders. No pod restarts.
- **2026-11-11 — krateo-audit-worm's ClickHouse client pinned by exact tag (0.1.7).** The seal and verify Jobs ran `clickhouse-server:24.8-alpine`, a floating tag that pulls each new 24.8 build on the next pod. It's now `24.8.14.39-alpine`, the build that tag gave 057's Jobs that day, so nothing that runs changes. `check-clickhouse-image-pin` now checks that image as well.
- **2026-11-11 — ClickHouse and Keeper pinned by exact tag, 26.10.3.7 (krateo-observability 0.1.45).** 0.1.43 meant to pin ClickHouse 26.8.36.3 by digest, with `tag: null` and `hash` set. ClickHouse operator v0.0.7 defaults an empty tag to `latest` before it chooses between tag and hash, so the hash was never used, and the 0.1.44 roll on krateo-057 moved ClickHouse to `clickhouse-server:latest`, 26.10.3.7 that day. Keeper had never been pinned and has run `latest` since install. 057 was healthy on 26.10.3.7 (no failed queries, ingestion running, the 15 users in place), so Diego chose to pin that version rather than go back. Both CRs now carry `tag: "26.10.3.7"` and no hash. The binaries don't change, so the roll restarts ClickHouse and Keeper once each. `check-clickhouse-image-pin` renders the chart, applies the operator's defaulting to both CRs and fails on an empty or inexact tag; the ClickHouse tests run 26.10.3.7. Lesson: a pin is checked against the image the StatefulSet runs, not against the values file.
- **2026-11-11 — HyperDX's MongoDB credential is generated and rotated (krateo-observability 0.1.44).** It was upstream clickstack's public default, and the full `MONGO_URI` with it sat in plain text in the `clickstack-config` ConfigMap. The chart now generates `mongodb-hyperdx-credentials` once and keeps it across upgrades (`username`, `password`, `uri`). The MongoDBCommunity user reads its password from that Secret, HyperDX reads `MONGO_URI` from it through `secretKeyRef`, and the ConfigMap no longer has the key. Upstream's default stays in `krateo-clickstack-mongodb-password` and in `clickstack-secret`'s `MONGODB_PASSWORD`, unreferenced and dead once the user is rotated. A follow-up release empties both once nothing references the old Secret. It can't happen in this release: MCK watches the Secret the CR references, and Helm updates Secrets before the CR, so emptying it now could briefly set an empty password. The first upgrade gives MongoDB the new password in place and rolls HyperDX. A new init container holds the new pod until MongoDB accepts the new URI, because HyperDX 2.35.0 stays Ready without a database when its one connect attempt fails. Rotate by raising `krateo.io/mongodb-credential-generation`. 0.1.44 contains 0.1.43's ClickHouse digest pin, so rolling it also restarts ClickHouse once. `check-hyperdx-mongo-credential` runs the rendered init container and URI against MongoDB 5.0.32.
- **2026-11-11 — the agent gateway's audit record in `krateo_audit` (otel-collector-daemonset 0.1.27, #138).** `logs/gateway-audit` projects the acting hops of the agent gateway's OTLP access log (A2A calls, MCP `tools/call`/`resources/read`/`prompts/get`, LLM completions) into `krateo_audit` as writer `gateway` of krateo-platformops/audit schema v1, while `logs/gateway` keeps every access-log record in `otel_logs` as before. The rest, MCP session plumbing, agent-card fetches, model listing and requests that matched no route, is excluded on an explicit list and counted per protocol and method (`krateo.gateway_audit.excluded`), and an MCP method on neither list is projected rather than dropped (snowplow's ruling as the gateway-audit lead). A refused hop names the target it would have reached (the route's configured backend, agentgateway-policies 0.2.28). The test feeds the real agentgateway v1.5.0's access-log output through the rendered pipelines and checks every record with the library's `auditschema check`: no reason but a marked gap, markers in the library's order, excluded hops all counted, falsifiers reported. Expected volume on 057 is ~16 MB a year.
- **2026-11-11 — ClickHouse users for the `default` lockdown (krateo-observability 0.1.42).** `analyst`, `mcp_reader`, `nightly_reader`, `audit_sealer`, `audit_verifier`, `otel_writer`, `hyperdx` and `slo_rollup` exist beside 0.1.41's portal readers, each with a profile, one per-table GRANT statement and a chart-generated Secret; `clickhouse-default-password` is created for later. 0.1.42 contains 0.1.41 (which was held), restarts ClickHouse once for the new environment variables, and switches no client: `default`, `app` and `otelcollector` are unchanged. Grants and profiles are the ones proven on kind against 057's DDL and the real audit-worm Jobs (2026-11-10); `check-clickhouse-users` loads the rendered config into ClickHouse 26.8.36.3 on every change.
- **2026-11-10 — read-only ClickHouse users for portal reads (krateo-observability 0.1.41).** `sse_proxy` and `snowplow_reader` exist with per-table SELECT grants and a `portal_reader` profile: `readonly=2`, cancel-on-close, an 8 s limit, 2 threads, and a per-user cap of 16 and 8 queries. Passwords live in Secrets the chart generates; ClickHouse reads the hash from the environment. The clients still use `default` until they switch. Tested on kind with ClickHouse 26.8.36.3 and operator 0.0.7: grants, refused writes and refused tables, settings, constraints, and cancellation of a query whose client went away (3 s, against 7 s for `default`).
- **2026-11-09 — the audit-to-reconcile join (`ops/audit-reconcile-join/`).** A query lists, for each portal write in `krateo_audit`, the composition-controller reconciles of the same object within a window, with the actor named and any reconcile two writes can claim flagged as ambiguous (installer#400). It joins on the controller name core-provider sets (`<resource>-<version>-controller`) rather than on a Kind guessed from the plural, which real data breaks (`agentikoes`). It's tested in ClickHouse 26.8.36.3 against 057's own DDL (`check-audit-reconcile-join`), since 057 has no reconcile spans until core-provider's `otel.enabled`.
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
  `filter/slo-spans` drops spans with `krateo.interaction.abandoned == true` (bool), per the CMP team's
  span contract for the performance SLOs: excluded, not counted as errors, and still in `otel_traces`. The CMP team's
  abandoned-flag self-check guards against a frontend bug hiding real slowness this way. Inert
  until snowplow #1061 (expected 1.12.65) forwards the flag. See
  [SLO → span metrics](SLO.md#span-metrics-the-inputs-for-performance-alerts).

- **2026-11-03 — SLO span metrics (otel-collector-daemonset 0.1.15).** Performance alerts are
  built from the `span_metrics` connector. The node-local agent gains a
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
  daemonset's traces and metrics pipelines (#112) — the path platform components'
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
