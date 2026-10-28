# krateo-audit-worm

Tamper-evidence (WORM) for the Krateo audit trail.

The OTel collector already writes the canonical audit records (`event.name="audit"`)
to the ClickHouse `krateo_audit` table with 365-day retention. That table is a
plain MergeTree, so its rows can be edited or deleted with no trace. This chart
adds the missing property: a **hash-chained seal** that makes any retroactive
edit or delete **detectable**.

It adds **no new ingest path**. It reads `krateo_audit`, mirrors each row into an
append-only WORM table stamped with its **ingestion time**, and periodically seals
windows into a hash chain that a verifier recomputes.

## What it deploys

| object | purpose |
|---|---|
| `…-scripts` ConfigMap | the schema + seal + verify logic |
| `…-seal` CronJob | every `seal.schedule`: applies the schema idempotently (WORM table, ingestion-stamping materialized view, seal table), then seals the window `(prev, now()-lagSeconds]` and chains `seal_hash = SHA256(segment_hash ‖ prev_seal_hash)` |
| `…-verify` CronJob | every `verify.schedule`, recomputes the chain and emits one OTel-shaped JSON line (`status` `ok`\|`tampered`\|`unprotected`\|`stale`); **exits non-zero** unless `ok` |

There is **no Helm hook**. The seal CronJob owns the schema, so the objects
appear on the first run after install and are re-asserted every run. This keeps
the chart identical under `helm template` and a live install, and avoids an
immutable hook Job on upgrade. Until the collector has created `krateo_audit`,
the seal Job fails with a clear message and the next run retries. The audit
trail is mirrored from the first seal run onward.

## When mirroring is switched off

The materialized view is what copies each audit row into the WORM table. If it
is dropped after install:
- the verifier reports `unprotected` (ERROR) while it is missing;
- the next seal run restores it, still seals, then emits `mirroring_restored`
  (ERROR) and **fails the Job** — audit rows written while it was missing were
  never mirrored, so the chain cannot vouch for them.

## Why sealing runs on ingestion time, not event time

Sealing uses the materialized view's `ingested_at` (insert wall-clock), not the
event `Timestamp`. A late or out-of-order audit row (collector batching, exporter
retries, post-outage backlog) still gets a *current* `ingested_at`, so it lands
**ahead** of the watermark and never inside an already-sealed window. The
verifier therefore has a defined answer: a row found inside a sealed window is
tampering, never a legitimate late arrival.

## Detecting a stopped sealer

A sealing job that silently stops leaves rows unsealed — invisible in exactly the
dimension the mechanism protects. The verifier guards this two ways: it marks the
chain `stale` if the newest seal is older than `verify.staleAfterSeconds`, and it
**fails the Job** (so a reconciled deployment surfaces it as not-Ready) rather
than exiting 0. Alert in HyperDX on `krateo.audit.status != ok` **and** on the
absence of the verifier's line.

## When the seal chain is dropped

If the seal table is dropped or truncated while the WORM table still holds rows,
the next seal run does **not** restart from the canonical genesis. It starts a
new chain from a marked *reset* genesis, emits `chain_reset` (ERROR) and fails
the Job. Because the new chain never links to the canonical genesis, the
verifier reports `tampered` (`1:linkage`) on every run from then on: the lost
history can never verify green again.

## Limits (read before relying on it)

This is tamper-**evidence** held in the same ClickHouse it protects, not
tamper-resistance:
- **Rows written while the MV is missing are never mirrored.** The gap is
  reported (`unprotected`, then `mirroring_restored`), but those rows are not in
  the chain.
- **A user who drops all three objects (WORM table, MV, seal table) resets to a
  fresh install.** A chain cannot detect its own complete erasure. Every verifier
  line carries `krateo.audit.head_seal_seq` / `head_seal_hash`, so this shows up
  as the head going backwards in the integrity-line history. Alert on that in
  HyperDX, together with ClickHouse's own query log of who ran the `DROP`.
- Physical prevention needs an external WORM anchor (object-lock storage), which
  the platform does not hold today.

## Values

See `values.yaml`. Key knobs: `clickhouse.*` (connection + the credentials
Secret), `sourceTable`/`wormTable`/`sealTable`, `seal.{schedule,lagSeconds}`,
`verify.{schedule,staleAfterSeconds}`.

## Scope

Tamper-**evidence** (detects edits/deletes after the fact), not tamper-resistance.
An object-lock/retention-lock cold tier would add physical prevention, but needs a
WORM-capable bucket and credentials the platform deliberately does not hold today.
