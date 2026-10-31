# Runbook: ClickHouse disk full

For the failure in [#80](https://github.com/krateo-platformops/observability/issues/80): a log flood
fills the ClickHouse data volume, and **TTL cannot reclaim space once free space reaches zero**.

Every command below was run against `krateo-057` (ClickHouse **26.8.3**) while writing this. Where a
command does **not** work with the credentials the platform ships, that is stated rather than left
for you to discover during the incident.

## Read this first: the prerequisite you probably do not have

The shipped `otel-clickhouse-credentials` principal is **`otelcollector`**, and its grants are:

```
GRANT SHOW, SELECT, INSERT, CREATE DATABASE, CREATE TABLE, CREATE VIEW, CREATE DICTIONARY
  ON default.*
```

Scoped to `default.*`. It therefore **cannot read the two system tables this incident needs**:

```
SELECT name, free_space, total_space FROM system.disks
  -> Code: 497 ACCESS_DENIED, needs SELECT ON system.disks

SELECT count() FROM system.parts WHERE active
  -> Code: 497 ACCESS_DENIED, needs SELECT ON system.parts

SELECT name, engine FROM system.tables WHERE database='default'   <- CONTROL: this WORKS
  otel_logs   MergeTree
```

The control matters: `system.tables` succeeds because the `SHOW` grant covers it. So a probe that
only touches `system.tables` will tell you your credentials are fine, and they are not fine for the
two tables that matter. **Do not infer system-table access from a successful `system.tables` query.**

**Consequence, stated plainly: with only the shipped credentials you can neither see how full the
disk is nor choose a partition to drop.** Section A (expand the volume) works without any new
principal. Sections B and C **require an ops principal** and are not actionable until one exists.

Creating that principal is a prerequisite, not a step. Since installer **0.3.482** it is
expressible through the installer rather than by hand:

```yaml
componentValues:
  krateo-observability:
    clickstack:
      clickhouse:
        cluster:
          spec:
            settings:
              extraUsersConfig:
                users:
                  chops:
                    password_sha256_hex: <sha256 of the password, 64 lowercase hex>
                    profile: default
                    grants:
                      - query: "GRANT SELECT ON system.disks"
                      - query: "GRANT SELECT ON system.parts"
                      - query: "GRANT SELECT ON system.tables"
                      - query: "GRANT ALTER DELETE, ALTER MATERIALIZE TTL ON default.*"
```

`password_sha256_hex` only — a plaintext `password` is rejected by the installer's schema by name,
because the Installer CR, etcd and the helm release Secret would all carry it in cleartext.

**Grant per table, never `ON system.*` or `ON default.*` for a human principal.** `INSERT ON
default.*` reaches `default.krateo_audit`, whose `krateo_audit_worm_mv` hashes and seals rows as
genuine — a writer with that grant can forge audit history through the tamper-evidence the trail
exists to provide. The grants above are read-only on `system` and carry no `INSERT` at all.

## Why TTL does not save you

TTL **is** configured and is not the gap. Measured on both `krateo-057` and `krateo-demo`:

```
otel_logs                           TTL TimestampTime        + toIntervalDay(7)
otel_traces                         TTL toDateTime(Timestamp) + toIntervalDay(7)
otel_metrics_*  (5 tables)          TTL toDateTime(TimeUnix)  + toIntervalDay(7)
krateo_audit                        TTL toDateTime(Timestamp) + toIntervalDay(365)

all with  ttl_only_drop_parts = 1
```

`ttl_only_drop_parts = 1` is the right setting and is also **why TTL cannot rescue a full disk**: it
drops whole parts only when every row in the part has expired. Under a flood the newest parts are
large and entirely unexpired, so there is nothing eligible to drop. Worse, the merge that would
normally consolidate and expire parts **needs free space to write its output**. At zero free space
merges fail, so the mechanism that reclaims space is the first thing the full disk disables.

So: TTL bounds steady-state growth. It does not recover from a flood, and adding more TTL during an
incident does nothing.

## A. Expand the volume — works today, no new principal

The only path fully actionable with what the platform ships. Check the StorageClass allows it:

```bash
kubectl get pvc -n krateo-system -l clickhouse.altinity.com/chi=krateo-clickstack-clickhouse \
  -o custom-columns='NAME:.metadata.name,SIZE:.spec.resources.requests.storage,SC:.spec.storageClassName'

kubectl get storageclass <SC> -o jsonpath='{.allowVolumeExpansion}{"\n"}'
```

If that prints `true`, raise the request **in the chart values, not on the PVC**. A hand-edited PVC
is reverted by the cdc reconciler, and on this platform a manual edit to a cdc-managed resource can
wedge the composition's dependency gate:

```yaml
componentValues:
  krateo-observability:
    clickstack:
      clickhouse:
        cluster:
          spec:
            dataVolumeClaimSpec:
              resources:
                requests:
                  storage: <new size>
```

Then watch the condition rather than assuming:

```bash
kubectl get pvc -n krateo-system <pvc> -o jsonpath='{.status.capacity.storage}{"\n"}'
kubectl get pvc -n krateo-system <pvc> -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}{"\n"}'
```

`FileSystemResizePending` means the volume grew but the filesystem has not; that clears when the pod
restarts. Expansion is **one-way** — you cannot shrink it back afterwards.

## B. Drop the oldest partitions — REQUIRES the ops principal

Faster than expansion and the usual choice under pressure, but destructive and unrecoverable: the
data is gone, not archived.

```sql
-- pick the target: oldest partitions of the biggest table
SELECT partition, sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts
WHERE active AND database = 'default' AND table = 'otel_logs'
GROUP BY partition ORDER BY partition ASC;

ALTER TABLE default.otel_logs DROP PARTITION '<oldest>';
```

**Never drop a partition of `krateo_audit`, `krateo_audit_seal` or `krateo_audit_worm`.** The audit
trail is sealed and tamper-evident; removing rows is indistinguishable from tampering and destroys
the property the trail exists to provide. Its TTL is 365 days deliberately. If the audit tables are
what filled the disk, that is an incident to escalate, not to reclaim.

`DROP PARTITION` on a MergeTree table frees space immediately without needing scratch space, which
is why it works at zero free where a merge does not.

## C. Confirm the disk is actually the problem — REQUIRES the ops principal

```sql
SELECT name, formatReadableSize(free_space) AS free,
       formatReadableSize(total_space) AS total,
       round(100 * (1 - free_space / total_space), 1) AS pct_used
FROM system.disks;
```

Without the ops principal, the substitute signal is the pod itself — not equivalent, but available:

```bash
kubectl describe pod -n krateo-system -l clickhouse.altinity.com/chi=krateo-clickstack-clickhouse \
  | grep -iE 'no space|evicted|disk|Warning'
kubectl logs -n krateo-system -l clickhouse.altinity.com/chi=krateo-clickstack-clickhouse --tail=200 \
  | grep -iE 'DISK_FULL|Not enough space|Cannot reserve|Code: 243'
```

`Code: 243` / `Not enough space` in the ClickHouse log is the definitive symptom. `kubectl top` tells
you nothing about disk, and a ClickHouse pod stays `Ready` with a full data volume — **a Ready pod is
not evidence of a healthy disk.**

## D. Stop the flood, or you will do this again in an hour

Reclaiming space without stopping the source buys minutes. Find the source first:

```sql
SELECT ServiceName, count() AS rows
FROM default.otel_logs
WHERE Timestamp > now() - INTERVAL 1 HOUR
GROUP BY ServiceName ORDER BY rows DESC LIMIT 10;
```

That query runs with the **shipped** `otelcollector` credentials — it is in `default.*`.

One source to rule out explicitly: **your own diagnostics**. The filelog receiver scrapes pod logs,
so query Jobs run in `krateo-system` write their own output back into `otel_logs`. If the top
`ServiceName` is a diagnostic Job, the flood is partly self-inflicted; keep probe output small and
delete Jobs when done.

## What is NOT in this runbook

- **An enforced size cap.** #80's remaining ask is a bounded-growth guarantee — a `max_size` on the
  volume or a size-based retention policy — so a flood cannot reach zero free in the first place.
  This runbook is recovery, not prevention, and recovery is the weaker half.
- **Widening `otelcollector`.** Do not add `system.*` grants to the collector principal to make
  sections B and C work. It is the ingest path, it holds `INSERT ON default.*`, and broadening a
  principal that can already write the audit tables is the wrong trade. Mint a separate ops user.
