---
type: Configuration
title: observability — the default alert set
description: The 25 Alert CRs the chart seeds, the measured evidence behind every filter and threshold, and the three rules that decide whether an Alert can fire at all — `where` is ClickHouse SQL and not the Lucene the CRD advertises, SeverityText is empty on every row, and log alerts must exclude the observability stack or they diagnose themselves.
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

**2. Never filter on `SeverityText`** — this OTel pipeline leaves it empty on every row.
Measured over 24h: **0 non-empty of 8,140,061**. Any severity predicate counts zero forever.

**3. Log alerts must exclude the observability stack; event alerts need not.** HyperDX writes
the query it is running into its own container logs, so a log alert searching for a word finds
the line in which the platform echoed that word and diagnoses itself. Container logs carry
`telemetry.source = ''` and events carry `'k8s-events'`, so scoping to events is a structural
guard; scoping to logs needs the explicit `ServiceName NOT IN (...)` exclusion.

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
| `sre-probe-failing` | core | events | above 5 | 15m | 470 |
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
| `sre-app-panic` | core | logs | above 0 | 15m | 0 |
| `sre-app-out-of-memory` | core | logs | above 0 | 15m | 0 |
| `sre-certificate-expiry-warning` | core | logs | above 0 | 1h | 7,434 |
| `sre-rbac-denied-burst` | extended | logs | above 20 | 15m | 3,240 |
| `sre-upstream-timeouts` | extended | logs | above 50 | 15m | 1,217 |
| `sre-volume-resize-issue` | extended | events | above 0 | 1h | 4 |
| `sre-krateo-external-resource-failure` | extended | events | above 200 | 15m | 57,567 |
| `sre-kubernetes-warning-burst` | extended | events | above 100 | 15m | 71,291 |

## Threshold calibration

Non-zero thresholds were checked against the share of **673 fifteen-minute windows** each
would have been ALERTing in, because a threshold that fires constantly trains people to ignore
it and one that never fires is the same as having no alert:

| alert | threshold | % of windows firing |
|---|---|---|
| `sre-probe-failing` | > 5 | 2.8% |
| `sre-volume-attach-failure` | > 3 | 0.6% |
| `sre-krateo-provider-connection-failure` | > 10 | 1.6% |
| `sre-telemetry-pipeline-stalled` | < 100 | 0.0% (observed floor: 67,081 rows/window) |

Where measurement contradicted the draft, the draft changed: `sre-loadbalancer-failure` was
written at 5, measured at **max 3 per window, mean 2.98**, and would have fired in **0% of
windows while a LoadBalancer delete was failing continuously** — hiding a leaking billed cloud
resource. It ships at 0.

A threshold of 0 with `above` means strictly greater, so it fires on the first matching row.
On a cluster with a genuine ongoing fault such an alert STAYS in `ALERT` until the fault is
fixed. That is intended, not a misconfiguration.

## What is deliberately not covered

Saturation — CPU, memory, disk and PV percentages, node capacity, certificate **days
remaining**. The Alert CRD counts rows in a logs source and has no way to address a metric, so
none of it can be expressed today, and writing it as log searches would produce exactly the
always-OK alerts rule 1 is about. Blocked on the `Metric` CRD + `metricRef` design
([alert-troubleshooter#34](https://github.com/krateo-platformops/alert-troubleshooter/issues/34)).

## Cost

Each firing alert starts a model-driven RCA and opens a `TroubleshootingReport`, **one per
alert** (observed: 3 firing alerts, exactly 3 reports). `AlertTroubleshooter`'s
`reportCooldown` bounds re-runs per alert, not the fan-out across alerts.

## Naming

Entries are prefixed `sre-` because Helm will not adopt a resource it does not already own: an
entry reusing the name of an alert someone hand-applied fails the upgrade outright with an
ownership error. A draft of this set did exactly that against a hand-applied CR on a live
cluster.
