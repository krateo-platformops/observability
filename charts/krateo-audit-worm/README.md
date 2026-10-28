# krateo-audit-worm

Tamper-evidence (WORM) for the Krateo audit trail — **C8** of the CMP program (D19).

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
| bootstrap `Job` (Helm hook) | creates the WORM table, the ingestion-stamping materialized view and the seal table (idempotent, `IF NOT EXISTS`) |
| `…-seal` CronJob | every `seal.schedule`, seals the window `(prev, now()-lagSeconds]` and chains `seal_hash = SHA256(segment_hash ‖ prev_seal_hash)` |
| `…-verify` CronJob | every `verify.schedule`, recomputes the chain and emits one OTel-shaped JSON line (`status` `ok`\|`tampered`\|`stale`); **exits non-zero** on a breach |

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

## Values

See `values.yaml`. Key knobs: `clickhouse.*` (connection + the credentials
Secret), `sourceTable`/`wormTable`/`sealTable`, `seal.{schedule,lagSeconds}`,
`verify.{schedule,staleAfterSeconds}`.

## Scope

Tamper-**evidence** (detects edits/deletes after the fact), not tamper-resistance.
An object-lock/retention-lock cold tier would add physical prevention, but needs a
WORM-capable bucket and credentials the platform deliberately does not hold today.
