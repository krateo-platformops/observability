---
type: Configuration
title: observability — the default alert set
description: The 37 Alert CRs (31 core, 6 extended) and 4 Metric CRs the chart seeds, among them the SLO objects adopted from krateo-057 (0.1.52), the measured evidence behind every filter and threshold, and the three rules that decide whether an Alert can fire at all — `where` is ClickHouse SQL and not the Lucene the CRD advertises, severity needs `otel-collector-daemonset` ≥ 0.1.17 and JSON logs, and log alerts must exclude the observability stack or they diagnose themselves. Event alerts skip the DELETED copy of an expired Event.
resource: oci://ghcr.io/krateo-platformops/charts/krateo-observability
tags: [alerting, alerts, observability, clickhouse, hyperdx, sre, runbook]
timestamp: 2026-09-18T00:00:00Z
---

# The default alert set

Reference for `defaultAlerts.alerts` in `charts/krateo-observability/values.yaml`. The set
itself lives there; this file records the evidence behind it.

## Why this file exists

An alert whose filter never evaluates is indistinguishable from a cluster with nothing wrong.
Both sit at `OK` forever. That is not a hypothetical failure mode — it is what the five alerts
this set replaces did, from install until they were measured.

So every `where` in the set was RUN against real rows before it was written, in a single
all-or-nothing query (one syntax error anywhere fails the whole thing), and what each returned
is recorded below.

## The three rules

**1. `where` is ClickHouse SQL, not Lucene** — whatever the CRD field description says. The
reconciler copies the filter verbatim into the query's `WHERE` clause, where Lucene dies:

```
WHERE (Body:"MemoryPressure" OR Body:"DiskPressure")
-> Code: 62. DB::Exception: Syntax error: failed at position 110 (Body) ... (SYNTAX_ERROR)
```

**2. Filter on `SeverityNumber`, and only with `otel-collector-daemonset` ≥ 0.1.17 live.** Before
0.1.17 the pipeline left severity empty on every row (measured over 24h: **0 non-empty of
8,140,061**), so a severity predicate counts zero forever, and that still applies to any cluster on
an older chart. From 0.1.17, JSON log lines carry it, but non-JSON lines stay empty, so an alert on a
component that doesn't log JSON must still filter on `Body`. Use `SeverityNumber >= 17`, not
`SeverityText`: the text is the level as emitted, so `PANIC` and `ERROR+1` don't match `'ERROR'`.

**3. Log alerts must exclude the observability stack; event alerts need not.** HyperDX writes
the query it is running into its own container logs, so a log alert searching for a word finds
the line in which the platform echoed that word and diagnoses itself. Container logs carry
`telemetry.source = ''` and events carry `'k8s-events'`, so scoping to events is a structural
guard; scoping to logs needs the explicit `ServiceName NOT IN (...)` exclusion.

The incident pipeline counts as part of that stack. `incident-controller` logs `Reconciler error`
for each Incident it cannot reconcile, and the incident checks it runs in `krateo-incident-checks`
raise their own events. On krateo-057 (24h to 2026-10-05) the former were 21,440 rows, which kept
`sre-krateo-composition-reconcile-error` ALERT in all 1,440 minutes, and the check pods' `FailedMount`
(a check's ConfigMap gone before its pod mounted it) were 57 of the 131 rows
`sre-volume-attach-failure` matched. Every alert that fires opens an Incident, so an alert on
this pipeline's own noise keeps feeding itself. The log alerts exclude `incident-controller`, and
`sre-volume-attach-failure` skips `krateo-incident-checks`:

| alert, 24h on krateo-057 | rows before | rows after | minutes ALERT before | after | still matches |
|---|---|---|---|---|---|
| `sre-krateo-composition-reconcile-error` | 21,446 | 11 | 1,440 | 30 | core-provider CompositionDefinition errors, mongodb-operator |
| `sre-volume-attach-failure` | 131 | 74 | 124 | 55 | FailedMount in kube-system and gmp-system, FailedAttachVolume in krateo-system |

"Minutes ALERT" counts the minutes whose trailing 15 minutes held at least the threshold.

The controller is being renamed `incident-provider`, and its OTel `service.name` changes with it.
Until no cluster runs a build that logs as `incident-controller`, either name can be writing these
lines, so every list excludes both, `'incident-controller', 'incident-provider'`. Dropping the old
name is a separate change, made once the rename has rolled out everywhere.

## Event alerts skip DELETED records

The collector's `k8sobjects` receiver watches Events, so it writes one row per change to an
Event: `ADDED`, `MODIFIED` as its count grows, and `DELETED` when the apiserver expires it, an
hour after its last update by default. The `DELETED` row repeats the Event's last state: same
reason, same message. An alert counting it fires a second time an hour after every failure, and
that echo opens or counts on an Incident like a real firing. On krateo-057, the two `installer`
Warnings of 2026-10-13 22:03 opened an incident, and their `DELETED` copies at 23:03 opened
another for the same problem.

So every event alert carries `JSONExtractString(Body, 'type') != 'DELETED'`. `MODIFIED` stays: it
is the Event recurring. Over the 7 days to 2026-10-19 13:00 on krateo-057, with this file's
thresholds and windows (the alerts with no rows in the window are left out):

| alert | rows before | rows after | minutes ALERT before | after |
|---|---|---|---|---|
| `sre-pod-crashloop` | 2 | 2 | 8 | 8 |
| `sre-pod-oomkilled` | 12 | 6 | 30 | 15 |
| `sre-pod-unschedulable` | 57 | 44 | 144 | 111 |
| `sre-pod-sandbox-failure` | 98 | 49 | 22 | 11 |
| `sre-probe-failing` | 266 | 223 | 184 | 156 |
| `sre-volume-attach-failure` | 14 | 10 | 15 | 15 |
| `sre-node-not-ready` | 33 | 21 | 91 | 66 |
| `sre-node-resource-pressure` | 161 | 88 | 639 | 418 |
| `sre-krateo-external-resource-failure` | 3,616 | 1,815 | 134 | 0 |
| `sre-kubernetes-warning-burst` | 5,473 | 3,211 | 254 | 190 |

"Minutes ALERT" counts the minutes whose trailing window held at least the threshold.

## Measured behaviour

Counts are matching rows over **7 days** on a live cluster (krateo-057). **A zero is not a
defect** — it means that failure mode did not occur in the window, which is the healthy state
for most of this list. The number establishes that the clause *evaluates*; it says nothing
about whether your cluster has that problem.

| alert | tier | source | threshold | window | rows in 7d |
|---|---|---|---|---|---|
| `sre-pod-crashloop` | core | events | above 0 | 5m | 69 |
| `sre-pod-oomkilled` | core | events | above 0 | 5m | 3 |
| `sre-image-pull-failure` | core | events | above 0 | 5m | 0 |
| `sre-pod-unschedulable` | core | events | above 0 | 15m | 0 |
| `sre-pod-evicted` | core | events | above 0 | 5m | 0 |
| `sre-pod-sandbox-failure` | core | events | above 0 | 5m | 0 |
| `sre-probe-failing` | core | events, liveness or startup probe event repeated 3+ times | above 5 | 15m | 1 (0 firing windows) |
| `sre-container-not-ready` | core | metric `k8s.container.ready`, max per container | below 1 for 5 consecutive 1m windows | 1m | 1 firing run (the CMP team's test) |
| `sre-job-failed` | core | events | above 0 | 15m | 0 |
| `sre-volume-attach-failure` | core | events | above 3 | 15m | 218 |
| `sre-pvc-provisioning-failure` | core | events | above 0 | 15m | 0 |
| `sre-node-not-ready` | core | events | above 0 | 5m | 0 |
| `sre-node-resource-pressure` | core | events | above 0 | 5m | 0 |
| `sre-node-preempted` | core | events | above 0 | 5m | 0 |
| `sre-loadbalancer-failure` | core | events | above 0 | 15m | 2,008 |
| `sre-telemetry-pipeline-stalled` | core | all rows | below 100 | 15m | 58,302,075 |
| `sre-krateo-composition-reconcile-error` | core | logs | above 2 | 15m | 59,811 |
| `sre-krateo-provider-connection-failure` | core | events | above 10 | 15m | 1,706 |
| `sre-rest-controller-vendor-5xx` | core | logs, generated REST controllers' failed observes with a vendor 5xx | above 19 for 2 consecutive windows | 15m | 328 (1 firing run, the 2026-11-12 outage) |
| `sre-rest-controller-vendor-5xx-persistent` | core | logs, the same rows | above 3 for 4 consecutive windows | 15m | 328 (1 firing run, the 2026-11-12 outage) |
| `sre-app-panic` | core | logs | above 0 | 15m | 0 |
| `sre-app-out-of-memory` | core | logs | above 0 | 15m | 0 |
| `sre-certificate-expiry-warning` | core | logs | above 0 | 1h | 7,434 |
| `sre-jwt-in-telemetry` | core | logs | above 1 | 15m | 0 (see below) |
| `sre-jwt-in-traces` | core | traces | above 1 | 15m | 0 of 45,180 spans |
| `sre-credential-header-in-traces` | core | traces | above 1 | 15m | 0 in 7 days (0.1.38) |
| `sre-clickhouse-disk-headroom` | core | metric `ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes`, min | below 3 GiB | 15m | n/a (0.1.30; source changed in 0.1.60) |
| `cmp-slo-api-p95` | core | metric `traces.span.metrics.duration` (snowplow), p95 | above 500 ms | 1h | n/a (0.1.52, see below) |
| `cmp-slo-api-error-ratio` | core | metric `traces.span.metrics.calls` (snowplow), share with `krateo.sli.error` = "true" | above 0.001 | 1h | n/a (0.1.52) |
| `cmp-slo-metrics-overflow` | core | metric `traces.span.metrics.calls`, overflow rows | above 1 | 1h | n/a (0.1.52) |
| `sre-rbac-denied-burst` | extended | logs | above 20 | 15m | 3,240 |
| `sre-upstream-timeouts` | extended | logs | above 50 | 15m | 1,217 |
| `sre-volume-resize-issue` | extended | events | above 0 | 1h | 4 |
| `sre-krateo-external-resource-failure` | extended | events | above 200 | 15m | 57,567 |
| `sre-kubernetes-warning-burst` | extended | events | above 100 | 15m | 71,291 |
| `sre-kubernetes-events-missing` | extended | events, any | below 1 | 30m | quietest 30 minutes: 718 (2026-11-24) |

### `sre-jwt-in-telemetry`: a zero that is not vacuous

Unlike the other log alerts, this one has neither `telemetry.source = ''` nor a `ServiceName`
exclusion. The first would hide `k8s-events` (chart 0.1.26). The second (chart 0.1.27) exists to stop
an alert matching its own query text, and this regex cannot match itself, while the excluded services
are the likeliest to leak: agents pass `--token eyJ...` on every kubectl call. This is the one
deliberate exception to rule 3.

This one's 7-day count is **0**, and by the rule above a zero normally establishes nothing about
whether the clause evaluates. It is established here by a **control arm** measured in the same
query:

```
rows containing  REDACTED-JWT                                839
of those, rows ALSO matching eyJ[A-Za-z0-9._-]{20,}             0
```

So the regex is live and discriminating: the collector's redaction fired 839 times in the window,
and the detector correctly matches none of those redacted rows. A zero for the detector therefore
means *no token escaped redaction*, not *the filter cannot match*.

It exists because the collector's `transform` runs `error_mode: ignore` — a redaction statement
that **fails** is skipped silently and the pipeline keeps shipping. Switching to `propagate`
would turn a redaction bug into a telemetry outage, so this detector is the only thing that
survives a skipped statement. For installer#394.

One caveat on the control: the 839 tracks whatever is currently leaking into logs. If an upstream
leak is fixed (installer#395 removes a bearer token from a URL), that count legitimately drops and
the control weakens. Re-measure it rather than assuming it holds.

## What a threshold means

`above N` is **`count >= N`**, not `count > N`. Read from the running HyperDX build (2.35.0,
`packages/api/build/tasks/checkAlerts/index.js`):

```js
ABOVE -> value >= threshold        ABOVE_EXCLUSIVE -> value > threshold
BELOW -> value <  threshold        BELOW_OR_EQUAL  -> value <= threshold
```

So **`threshold: 0` with `above` is unconditionally true** and fires on an empty result. The first
version of this catalogue shipped 15 such alerts. Every one went `ALERT` on a cluster where the
condition was not occurring, and each firing alert opened a `TroubleshootingReport` — 3 reports
became 18 within minutes.

Verified by A/B on a live cluster rather than by reading: two alerts with a clause matching zero
rows, identical but for the threshold — **0 fired, 1 stayed OK** — and a third with threshold 1 over
a clause matching 2,538 rows fired correctly.

A threshold is therefore **the smallest count that should fire**. `1` means "any occurrence".

## Reading an Alert's state

alert-provider writes these four fields on each `Alert`'s `.status`. They answer different
questions, and two of them are easy to confuse:

| field | what it means | moves when |
|---|---|---|
| `state` | HyperDX's raw state: `OK`, `ALERT` or `PENDING`. Empty means not evaluated yet | every read |
| `stateSince` | when `state` last **changed** to its current value (a `lastTransitionTime`) | only when a read finds a different `state`, never on a re-read |
| `stateObservedAt` | when the provider last **read** the state | every read |
| `okSince` | when the alert last turned `OK`; unset while it is anything else | on the transition to `OK` |

- **"Firing since"** is `stateSince` on an `ALERT` alert. `stateObservedAt` is not a firing time:
  an alert ALERTing for three days has a `stateObservedAt` from the last minute.
- For an `OK` alert, `stateSince` equals `okSince`.
- **Empty `stateSince` means unknown, never now.** An alert that was already `ALERT` or `PENDING`
  when its cluster upgraded to an alert-provider with the field (0.2.83, PR #119) has an empty
  `stateSince` until its next transition, because the provider never saw when that state began.
- A stale `stateObservedAt` means the provider stopped reading, not that the alert stopped firing.
  `Ready` stays as it was.

The field exists from alert-provider 0.2.83 (charts `alert-provider` and `alert-provider-crds`). A
cluster on an older release has no `stateSince` at all.

## Threshold calibration

Non-zero thresholds were checked against the share of **673 fifteen-minute windows** each would be
ALERTing in. They are written as N+1 against the measured `> N`, which is not pedantry — the
boundary case is where the difference lives:

| alert | threshold | % of windows firing |
|---|---|---|
| `sre-probe-failing` | >= 6 | 3.1%  (at `>= 5` it is 3.3%) |
| `sre-volume-attach-failure` | >= 4 | 0.6%  (**at `>= 3` it is 9.4%** — a 15x difference) |
| `sre-krateo-provider-connection-failure` | >= 11 | 1.6% |
| `sre-rest-controller-vendor-5xx` | >= 20, 2 windows in a row | 3 of 678 windows ALERT (0.4%), all in one outage |
| `sre-rest-controller-vendor-5xx-persistent` | >= 4, 4 windows in a row | 1 of 678 windows ALERT (0.1%), the same outage |
| `sre-telemetry-pipeline-stalled` | < 100 | 0.0%  (observed floor: 67,081 rows/window) |

`below` is exclusive, so the dead-man's switch reads correctly as "fewer than 100 rows".

Where measurement contradicted the draft, the draft changed: `sre-loadbalancer-failure` was written
at 5, measured at max 3 per window, and would have fired in 0% of windows while a LoadBalancer
delete was failing continuously — hiding a leaking billed cloud resource. It fires on any
occurrence.

## What is deliberately not covered

Saturation — CPU, memory, disk and PV percentages, node capacity, certificate **days
remaining**. The Alert CRD counts rows in a logs source and has no way to address a metric, so
none of it can be expressed as a LOG alert, and writing it as a log search would produce exactly
the always-OK alerts rule 1 is about.

**Update:** [alert-troubleshooter#34](https://github.com/krateo-platformops/alert-troubleshooter/issues/34)
(*Metric as a custom resource + metricRef on Alert*) is **CLOSED**, and the Alert CRD now carries
`metricRef` and `labels`. So saturation is no longer blocked on a design — it needs a `Metric` CR
per quantity, and there are **zero** `Metric` resources on krateo-057, so the metric path is
unexercised rather than unavailable. `sre-clickhouse-disk-headroom` (0.1.30) is the first metric
alert: the minimum of `k8s.volume.available` on the ClickHouse claim, below 3 GiB. It needs
`otel-collector-daemonset` 0.1.19 for `k8s.volume.*`, and `alert-provider` 0.2.61, whose `labels`
match resource attributes. With an older `alert-provider` the claim filter matches no points, and a
`below` alert fires on the empty window. See installer#393.

**Source since 0.1.60.** It reads `ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes` instead,
ClickHouse's own figure for the filesystem its data is on (Metric `clickhouse-disk-available`),
scraped from its Prometheus port by `otel-collector-daemonset` 0.1.39 or later. The kubelet is no longer
needed, so a cluster without kubelet access (a child) has the alert too. The threshold, interval and
runbook are unchanged, and there is no claim label to pin. Two consequences: the collector must be
rolled to 0.1.39 **before** this chart, or the metric does not exist yet and the alert fires on the
empty window; and `below` also fires when ClickHouse stops answering its metrics port, which the
kubelet's figure did not (the description says so, and step 1 of the runbook is to find out which).

`sre-jwt-in-telemetry` carries `observability.krateo.io/analysis: human` from 0.1.30: its incident
goes to a person with no RCA, so no agent reads the leaked row.

## Cost

Each firing alert starts a model-driven RCA and opens a `TroubleshootingReport`, **one per
alert** (observed: 3 firing alerts, exactly 3 reports). `AlertTroubleshooter`'s
`reportCooldown` bounds re-runs per alert, not the fan-out across alerts.

## Naming

Entries are prefixed `sre-` because Helm will not adopt a resource it does not already own: an
entry reusing the name of an alert someone hand-applied fails the upgrade outright with an
ownership error. A draft of this set did exactly that against a hand-applied CR on a live
cluster.

The SLO set (`cmp-slo-*` and its Metrics `slo-span-*`, 0.1.52) is the one deliberate exception: it
keeps the names of the SLO objects proven on krateo-057 on 2026-11-10, so those CRs are adopted rather
than recreated.
See the next section for the adoption step.

## SLO objects (0.1.52)

The CMP API objectives, as two Metrics and three core Alerts, each evaluated over the last hour:

| object | what it reads | fires when |
|---|---|---|
| Metric `slo-span-calls` | `traces.span.metrics.calls` (sum, DELTA) from the spanmetrics connector | |
| Metric `slo-span-duration` | `traces.span.metrics.duration` (histogram, ms, buckets 25 ms to 10 s) | |
| Alert `cmp-slo-api-p95` | p95 of `slo-span-duration`, `service.name` = snowplow | at least 500 ms |
| Alert `cmp-slo-api-error-ratio` | sum of `slo-span-calls` with `krateo.sli.error` = "true" (5xx or no response; 4xx is not a failure) over the sum of all of snowplow's, a `ratio` alert | a share of at least 0.001 |
| Alert `cmp-slo-metrics-overflow` | sum of `slo-span-calls` rows with `otel.metric.overflow` = "true" | at least 1 row: the SLIs above under-count without going NO DATA |

**Source.** The specs are the CMP team's SLO generator output, the same objects that were proven on
krateo-057 on 2026-11-10 (applied there by hand). The chart is now their only source, at the CMP
team's request. The output is committed as
[`tests/fixtures/slo-objects-render.yaml`](../tests/fixtures/slo-objects-render.yaml), with the
generator commit in its header, and `tests/slo-defaults.py` (workflow `check-slo-defaults`) fails when
the chart's rendered specs differ from it in any field, value or JSON type. A change to a target or a
description is made in the generator and in `values.yaml` together, and the fixture is refreshed from
the generator. displayName and description are the generator's text.

**Needs:**

- **alert-provider 0.2.68** for these five: `spec.description` (0.2.68's rename), `metricRef` and
  `aggregation: p95` (0.2.51), `ratio` (0.2.58). The chart as a whole already needs 0.2.69, for
  `runbook` (0.1.40), so the chart's minimum doesn't move.
- **otel-collector-daemonset 0.1.26**: the spanmetrics connector that emits both metrics, with the
  bucket bounds the descriptions name.
- **`krateo.sli.error` stamped on the SLI spans by snowplow and the frontend.** Without it the error
  ratio's numerator is empty and the alert reads 0.

They are core. They stay silent while the API meets its objectives, and on a cluster with no snowplow
traffic: HyperDX evaluates an empty window as 0, and all three are `above`. They don't open incidents:
alert-provider marks an alert on an aggregated value `OpensIncidents=False` (`AggregatedValue`).

**Labels.** The chart's standard two, `app.kubernetes.io/managed-by: krateo-observability-defaults` and
`app.kubernetes.io/part-of: krateo-observability`, like every other default. The extra label the
hand-applied objects carry on 057 is not rendered: nothing selects on it.

### Adoption on krateo-057

On krateo-057 the five already exist, applied with `kubectl apply` and carrying no Helm ownership. The
chart renders them under the same names on purpose: an adopted CR is patched in place, so
alert-provider keeps its `status.hyperdxAlertId` and the HyperDX alert it points to. A recreated CR
would get a new HyperDX alert. Demo, and any new cluster, has none of them, so there they are created.

**Pre-check (read-only).** Run before the roll. Each line should show an empty `managed-by` and
`release`, `generation=1`, and, for the Alerts, a `hyperdxAlertId`. Note the ids, to compare after,
and the `labels` (the extra label the hand apply added shows there).

```sh
kubectl -n krateo-system get \
  alerts.observability.krateo.io/cmp-slo-api-p95 \
  alerts.observability.krateo.io/cmp-slo-api-error-ratio \
  alerts.observability.krateo.io/cmp-slo-metrics-overflow \
  metrics.observability.krateo.io/slo-span-calls \
  metrics.observability.krateo.io/slo-span-duration \
  -o jsonpath='{range .items[*]}{.kind}/{.metadata.name}{"\t"}managed-by={.metadata.labels.app\.kubernetes\.io/managed-by}{"\t"}release={.metadata.annotations.meta\.helm\.sh/release-name}/{.metadata.annotations.meta\.helm\.sh/release-namespace}{"\t"}generation={.metadata.generation}{"\t"}hyperdxAlertId={.status.hyperdxAlertId}{"\t"}labels={.metadata.labels}{"\n"}{end}'
helm get metadata krateo-observability -n krateo-system
```

A `release` naming anything but `krateo-observability/krateo-system` means another release owns the
object: stop, and find out which.

**Why a plain upgrade would fail.** Helm 3 refuses to put a resource that exists in the cluster but
not in the release's previous manifest into the release, unless it already carries Helm's ownership
metadata: `<kind> "<name>" in namespace "krateo-system" exists and cannot be imported into the current
release: invalid ownership metadata`. The check is `checkOwnership` in Helm's
`pkg/action/validate.go` (the same in v3.19.0 and v3.20.2), and it needs all three of:

- label `app.kubernetes.io/managed-by` equal to exactly `Helm`;
- annotation `meta.helm.sh/release-name` equal to the release name, `krateo-observability`;
- annotation `meta.helm.sh/release-namespace` equal to the release namespace, `krateo-system`.

Helm skips that check only with `TakeOwnership` (`helm upgrade --take-ownership`, Helm 3.17 and
later), when it adopts whatever exists.

**On krateo-057 the composition controller adopts them itself.** krateo-observability is installed
and upgraded there by the composition-dynamic-controller (2.13.105 on 2026-11-16, built with plumbing
v1.80.0 and Helm v3.20.2), and it passes `TakeOwnership: true` on install, reconcile and upgrade
(core-provider `go/composition-dynamic-controller/internal/composition/composition.go`). Its reconcile
also treats a live object that the stored manifest lacks as last applied in its rendered form
(plumbing `helm/v3/reconcile.go`), and three-way merges it to the render, so the live object takes
the chart's spec. So the roll to 0.1.52 needs no manual step there. plumbing's envtest exercises that
adoption (`reconcile_test.go`: a live object missing from the stored manifest is patched to the
rendered value) with an object that already carries Helm's ownership metadata. The unowned case on
057 was read from the source: the dry-run render passes `TakeOwnership` through (`config.go`).

**A plain `helm upgrade` adopts without writing the spec.** Measured on kind on 2026-11-17 with Helm
v3.20.2, both with `--take-ownership` and after the ownership step below: the upgrade puts the
hand-applied Alert into the release, but leaves its spec as it was (`generation` 1, the old
`displayName` and `description`), and the next upgrade only adds Helm's ownership metadata. Helm
patches from the object it last applied, and for an adopted object that is the render itself, so a
later upgrade writes a field only when the chart changes it. Today no cluster upgraded by plain Helm
has these objects: they were hand-applied on 057 only.

**The one-time ownership step**, needed only where the upgrade runs without `TakeOwnership` (a plain
`helm upgrade` by hand, or a controller older than that). Run it once, right before the upgrade. It
is harmless on 057 too, if you'd rather not depend on the controller's flag:

```sh
kubectl -n krateo-system label \
  alerts.observability.krateo.io/cmp-slo-api-p95 \
  alerts.observability.krateo.io/cmp-slo-api-error-ratio \
  alerts.observability.krateo.io/cmp-slo-metrics-overflow \
  metrics.observability.krateo.io/slo-span-calls \
  metrics.observability.krateo.io/slo-span-duration \
  app.kubernetes.io/managed-by=Helm
kubectl -n krateo-system annotate \
  alerts.observability.krateo.io/cmp-slo-api-p95 \
  alerts.observability.krateo.io/cmp-slo-api-error-ratio \
  alerts.observability.krateo.io/cmp-slo-metrics-overflow \
  metrics.observability.krateo.io/slo-span-calls \
  metrics.observability.krateo.io/slo-span-duration \
  meta.helm.sh/release-name=krateo-observability \
  meta.helm.sh/release-namespace=krateo-system
```

`kubectl label` and `annotate` refuse to change a key that's already set to something else, which is
what you want here; don't add `--overwrite`. The `managed-by=Helm` label is temporary: the chart
renders `krateo-observability-defaults`, and the controller's next reconcile sets it, as on every other
default alert.

**What the controller's reconcile changes on each live object on 057.** Compared on 2026-11-17
against the chart's render (0.1.55, with 057's values):

- **The three Alerts' `displayName` and `description`.** The objects on 057 carry the generator's
  earlier wording, which named internal identifiers; the chart ships the generator's current,
  generic text. Nothing else in their specs changes: metricRef, aggregation, labels, ratio,
  threshold, thresholdType and interval are identical. `metadata.generation` goes from 1 to 2, and
  alert-provider updates the HyperDX alert of the same CR (expected to keep `status.hyperdxAlertId`;
  the post-check confirms it).
- **The two Metrics' specs:** no change.
- **`where: ""`** on the three Alerts, the CRD's default, which the chart doesn't render, stays: an
  adoption deletes nothing it didn't last apply.
- **labels added:** `app.kubernetes.io/part-of: krateo-observability`; `app.kubernetes.io/managed-by`
  (`Helm` from the upgrade itself, then `krateo-observability-defaults` once the controller
  reconciles); the controller's `krateo.io/composition-*` labels (`composition-id`,
  `composition-installed-version`, and the rest, as on the 26 default alerts).
- **annotations added:** `meta.helm.sh/release-name: krateo-observability`,
  `meta.helm.sh/release-namespace: krateo-system`, and the controller's `krateo.io/traceparent`.
- **kept:** the hand apply's extra label, `kubectl.kubernetes.io/last-applied-configuration`, the
  finalizer `observability.krateo.io/hyperdx-cleanup` and the status. Helm's merge removes only keys
  the release applied before, and it never applied these.

After the roll, re-run the pre-check. Each object should name `krateo-observability/krateo-system`, and
each Alert's `hyperdxAlertId` should be unchanged. To drop the hand apply's leftovers (optional,
nothing selects on them), remove the extra label the pre-check showed, with
`kubectl -n krateo-system label <the five objects> <that-label-key>-`, and the last-applied annotation:

```sh
kubectl -n krateo-system annotate \
  alerts.observability.krateo.io/cmp-slo-api-p95 \
  alerts.observability.krateo.io/cmp-slo-api-error-ratio \
  alerts.observability.krateo.io/cmp-slo-metrics-overflow \
  metrics.observability.krateo.io/slo-span-calls \
  metrics.observability.krateo.io/slo-span-duration \
  kubectl.kubernetes.io/last-applied-configuration-
```

From then on, change these through the chart (or a composition's `defaultAlerts` values), never with
`kubectl apply`: the next reconcile reverts a hand edit.

**Schema and template changes this needed.** An Alert entry takes `ratio` (`labels`, rendered as
`spec.ratio`; empty labels render `ratio: {}`). `threshold` was cast to an integer, which would have
made 0.001 a 0: a whole number still renders as an integer, a fraction as itself. Its schema type moves
from `integer` to number-or-string. crdgen types a bare `number` as a string in the composition CRD,
and collapses exactly this union to `string`, so in a composition's `spec` a threshold is written
quoted (`"0.001"`). The template renders it as a number and refuses a string that isn't a plain decimal.

## `sre-credential-header-in-traces` (0.1.38)

`sre-jwt-in-traces` matches JWT-shaped **values**, so it can't see an opaque bearer token or a session
cookie. This alert matches the **key** instead: any span or span-event attribute whose key contains
`authorization` or `cookie`, case-insensitive (`http.header.Authorization`,
`http.request.header.authorization`, `proxy-authorization`, `…cookie`, `set-cookie`). otel-collector-daemonset
0.1.24 deletes those keys at the node agent whatever their value, so a match means the redaction failed or
a sender bypassed the node agents. The motivating case: upstream kagent tools copy the raw Authorization
header into spans when tracing is on. On krateo-057, 0 matching spans in the 7 days to 2026-11-09; the same
query found `http.route` keys on 83,084 spans in one day, so it reads the keys. Like the JWT detectors, it's
labelled `observability.krateo.io/analysis: human`, so no agent reads the leaked span.

## `sre-probe-failing` and rolls (0.1.32)

Every pod logs one readiness failure as it starts, so before 0.1.32 each installer roll fired this
alert and started an RCA: 17 of the 26 active 15-minute windows on krateo-057 in the 7 days to
2026-11-07, all of them rolls. It now counts a probe event only once Kubernetes has repeated it at least
3 times (`object.count >= 3`), meaning a probe that kept failing for about 30 seconds or more. Over the
same week that leaves 1 firing window: snowplow unready for 2 minutes, with 25 repeats.

## Readiness moves to `sre-container-not-ready` (0.1.35)

`sre-probe-failing` counted readiness and liveness events together in 15-minute windows. Readiness
was nearly all of it: in the 7 days to 2026-11-09 on krateo-057, 160 of the 161 repeated probe events
and all 5 firing windows. Each window was a short dip: snowplow unready for about 2 minutes in three
rolls, about a dozen krateo-system and sre-* test pods unready together at 03:53–03:58 on 2026-11-08,
and the CMP team's deliberate test. A count of events can't tell a 2-minute dip in a roll from a container
that stays out of its Service, and it can't name the container.

`sre-container-not-ready` watches the readiness metric instead, one series per namespace, pod and
container (`groupBy`, alert-provider 0.2.64). It fires when a container's highest readiness value
stays below 1 for 5 consecutive 1-minute windows (`consecutiveWindows: 5`), and HyperDX's `Group:` line
names the container. A window with no points restarts the count, so a deleted pod stops counting
rather than firing. Over the same 7 days it would have fired once, for the CMP team's test pod (10 minutes
unready), and never for a roll.

Two things make the metric usable, both from otel-collector-deployment 0.3.15:

- **Job pods are dropped.** A finished Job pod reports ready 0 for as long as it exists. Before
  0.3.15 that gave 895 five-minute unready runs in 7 days, all Job pods (audit-worm seal and verify,
  hyperdx-key-bootstrap). The collector now drops `k8s.container.ready` for pods with a
  `k8s.job.name`.
- **Check pods are dropped** (0.3.16). incident-provider's check pods in `krateo-incident-checks` are
  bare run-to-completion pods, not Jobs: a new one every 30–60s, each with a single ready-0 point.
  They could never fire the alert, but one was nearly always in the current window, so the alert sat
  in `PENDING` on a healthy cluster. The collector drops their readiness by the pod label
  `app.kubernetes.io/component=incident-check`. A stuck check pod is still caught by the image-pull,
  unschedulable and crashloop alerts.
- **30s collection.** A 1-minute window holds two points, so one late scrape doesn't leave an empty
  window that restarts the count.

`sre-probe-failing` now matches only liveness and startup probe events (`NOT startsWith(message,
'Readiness probe')`), which in that week was 1 event and 0 firing windows. A failing liveness probe
restarts the container rather than leaving it unready, so the readiness metric wouldn't show it.

## `sre-krateo-composition-reconcile-error` and rolls (0.1.33)

controller-runtime logs "Reconciler error" for two errors it retries by itself: a reconcile cut short
by shutdown (`context canceled`) and an optimistic-lock conflict (`the object has been modified`).
core-provider logs both whenever it restarts. On krateo-057 they were every match in the 2 days to
2026-11-07, firing the alert on 4 installer rolls. Config Connector's 516 matches in 4 days were
conflicts it re-queues too. 0.1.33 excludes both messages, in `err` or `error`. Over the same 4 days
what still fires is a CompositionDefinition stuck waiting for its compositions to delete, in 10 windows.

## `sre-rest-controller-vendor-5xx` (0.1.50)

A vendor's API answering 5xx to the generated REST controllers: the Deployments that run
`rest-dynamic-controller`, 34 on krateo-057 (the GitHub provider's `github-provider-kog-*-controller`
among them). When a vendor fails, every resource of the affected kind fails its resync together, and
before 0.1.50 no alert caught it: the controllers log `Cannot observe external resource`, not
`Reconciler error`.

**What it counts.** Container log lines whose JSON `service` is `rest-dynamic-controller` (written by
the binary, so it holds whatever the Deployment is called; `ServiceName` is the Deployment), whose
`msg` is `Cannot observe external resource`, and whose `err` carries a 5xx status. The controller
formats a vendor status as `unexpected status: <code>: invalid status code: <code>`, so the filter is
`match(err, 'status( code)?: 5[0-9][0-9]')`. Each failure also logs a `Performing REST call` line,
which the `msg` filter leaves out so a failure counts once. 4xx is left out on purpose: 401, 403 and
404 mean credentials, permissions or a missing object, and each resource reports those on its own
status. In the 7 days to 2026-11-16 there were no 4xx observe failures on krateo-057 at all.

**Not per kind.** Grouping is out on both sides. alert-provider's `groupBy` is for metric alerts
only: its CRD refuses it on a log alert (`!has(self.groupBy) || has(self.metricRef)`,
`helm/alert-provider-crds/templates/crd.alert.yaml`, the same rule live on krateo-057 with
alert-provider 0.2.85). And the metric a `Metric` could group,
`rest_dynamic_controller.external.request.count` (with `http.response.status_code`), is off on
krateo-057: `rest-dynamic-controller` exports it only with `OTEL_ENABLED`, and none of the 34
controllers' ConfigMaps sets that key (0.1.51). So the alert counts all kinds together, and the kind
is named in two places instead:

- the Incident's records, which alert-provider groups by `ServiceName` and pod for a container log,
  so they name the failing controller (`github-provider-kog-pullrequest-controller`);
- the runbook's query, which returns kind, status, failures and distinct resources (for the outage
  below: `PullRequest`, `503`, 321, 17) and never selects `err`, which can carry the vendor's
  identifiers.

**Threshold and window: 20 rows in each of 2 consecutive 15-minute windows.** Measured on krateo-057,
every epoch-aligned 15-minute window from 2026-11-09 17:30 to 2026-11-16 19:00 (678 windows); only 9
were non-empty:

| window (UTC) | rows | what | state after it |
|---|---|---|---|
| 11-10 16:00 | 4 | 500, 3 Repositories | OK |
| 11-10 21:15 | 2 | 502, 2 PullRequests | OK |
| 11-11 15:00 | 1 | 503, 1 PullRequest | OK |
| 11-12 13:30 | 28 | 503, one failed resync of all 17 PullRequests (13:37) | PENDING |
| 11-12 13:45 to 14:15 | 0 | | OK |
| 11-12 14:30 | 102 | 503, 17 PullRequests | PENDING |
| 11-12 14:45 | 34 | 503, 17 | **ALERT** (at 15:00) |
| 11-12 15:00 | 88 | 503, 17 | ALERT |
| 11-12 15:15 | 68 | 503, 17; the last failure at 15:25:59 | ALERT |
| 11-12 15:30 | 0 | | OK (at 15:45, 19 minutes after the last failure) |
| 11-16 06:30 | 1 | 504, 1 Repository | OK |

2026-11-13, -14 and -15 had none, out of about 16,000 `rest-dynamic-controller` lines a day.

- The isolated failures are at most 4 rows, the outage's sustained windows at least 34.
- Two windows in a row keeps a single failed resync from firing: the 13:37 burst was every
  PullRequest failing once, then 57 minutes of successes. It goes PENDING, which notifies nothing.
  It also keeps the outage to one firing instead of two.
- **The floor (corrected in 0.1.51; 0.1.50 said "about 10 rows per resource").** A resource resyncs
  every 3 minutes (`REST_CONTROLLER_RESYNC_INTERVAL`, unset on all 34 controllers on 057), so a
  window holds up to 5 failed passes per resource, fewer when the vendor fails intermittently. A
  failed pass logs 1 to 6 rows: the pass, then one per error retry (1 s base, doubling, at most 5)
  until a retry succeeds. On 057 that was 2 rows in the 11-12 outage (a retry about 1.4 s later
  failed too) and 6 rows in 31 s for one hard failure on 11-10. So one resource gives 5 to 30 rows
  a window, and 20 rows needs:

  | rows per failed pass | resources needed, every pass failing |
  |---|---|
  | 1 (the failure clears on its first retry) | at least 4 |
  | 2 (the 11-12 outage) | at least 2 |
  | 6 (hard down, every retry fails) | 1 |

  It needs more resources against an intermittent failure or a longer resync. At a 5-minute resync
  a window holds 3 passes, so it needs at least 7 resources at 1 row a pass.
  `sre-rest-controller-vendor-5xx-persistent` catches the 1-to-3-resource outage that this alert
  misses.
- A kind that resyncs less often than every 15 minutes leaves empty windows, and an empty window
  restarts the count, so this alert can't see a vendor outage on such a kind.

`tests/vendor-5xx-alert-docker.py` (workflow `check-vendor-5xx-alert`) renders both Alerts, checks their
specs, and replays these counts (`tests/fixtures/vendor-5xx-krateo-057.json`, counts only) as synthetic
rows through the rendered `where` in ClickHouse 26.10.3.7, with the 4xx, non-status, other-controller
and HyperDX-echo controls left out. Needs alert-provider 0.2.69 (`runbook`; `consecutiveWindows` on a
log alert since 0.2.64).

## `sre-rest-controller-vendor-5xx-persistent` (0.1.51)

This alert uses the same `where` as `sre-rest-controller-vendor-5xx`. It fires on **at least 4 rows in
each of 4 consecutive 15-minute windows**: a vendor 5xx that has persisted for an hour, however few
resources it hits. Diego asked for it on 2026-11-16, after the fast alert's floor turned out to be 4
resources, not 2.

- **Why 4 rows, not 5.** One resource has up to 5 failed passes a window. client-go jitters the
  resync, and an intermittent vendor fails only some passes. A threshold of 5 would let one window
  of 4 break the streak, in exactly the outage this alert is for.
- **Why 4 windows.** A failure has to last an hour. Every isolated failure on 057 sat in a single
  window. The 4 rows of 500 on 11-10 reach the threshold and go PENDING, which notifies nothing.

Replay on krateo-057's counts:

| case | fast (>= 20 x 2) | persistent (>= 4 x 4) |
|---|---|---|
| (a) 11-12 outage, 17 PullRequests, 503 | ALERT 15:00 to 15:45 | ALERT 15:30 to 15:45 (the 15:15 window is its 4th), the same outage |
| (b) isolated 500 x4, 502 x2, 503 x1, 504 x1 | OK | OK, the 500s PENDING for one window |
| (e) quiet day 11-15 | OK | OK |
| (c) one resource, 75 min, 1 or 2 rows a pass, 5 start phases | never | fires in every phase |
| (d) one resource, 45 min, 1 row a pass, 5 start phases | never | never |
| (f) one resource, 75 min, resync jittered +-20 s | never | fires |
| (g) one resource, 75 min, failing 4 of every 5 passes | never | fires (4 rows a window) |
| (h) a single 4-row window, then quiet | never | never |
| (i) one resource hard down (6 rows a pass), 45 min | fires | never (3 windows) |

**Limit.** With 2 or more rows a pass, as in the 11-12 outage, 2 failed passes reach 4 rows. A
45-minute failure that starts 7 minutes into a window touches 4 windows (6, 10, 10 and 4 rows), so it
fires. The test asserts this so the behaviour stays visible. The "an hour" in the description holds
at 1 row a pass. With more rows a pass, it's 45 to 60 minutes, depending on where the failure starts
relative to the window boundaries.

When both alerts fire, they open two incidents for one outage. The persistent alert's runbook says to
work the outage under the fast alert.
