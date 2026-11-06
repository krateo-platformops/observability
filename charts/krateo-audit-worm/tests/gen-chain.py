#!/usr/bin/env python3
"""Rebuilds krateo_audit_worm with one row per second and writes a valid seal chain over it, the
way seal.sh would have, so tests/verify-docker.sh can verify a long chain without sealing it."""
import hashlib
import subprocess
import sys

_, count, container = sys.argv
BASE = 1_700_000_000


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def client(*args, stdin=None):
    return subprocess.run(["docker", "exec", "-i", container, "clickhouse-client", "--user", "e2e", "--password", "e2e", *args],
                          input=stdin, text=True, check=True, capture_output=True).stdout


row_hash = "lower(hex(SHA256(concat(toString(Timestamp), '|', TraceId, '|', SpanId, '|', toString(SeverityText), '|', toString(ServiceName), '|', Body, '|', toString(mapSort(ResourceAttributes)), '|', toString(mapSort(LogAttributes))))))"
client("-q", f"INSERT INTO krateo_audit_worm SELECT fromUnixTimestamp64Nano(toInt64({BASE} + rowNumberInAllBlocks()) * 1000000000) AS ingested_at, "
             f"Timestamp, TraceId, SpanId, SeverityText, ServiceName, Body, ResourceAttributes, LogAttributes, {row_hash} FROM krateo_audit LIMIT {count}")
rows = client("-q", "SELECT toUnixTimestamp64Nano(ingested_at) / 1000000000, row_hash FROM krateo_audit_worm ORDER BY ingested_at FORMAT TSV").splitlines()

lines = []
prev_hash = sha("krateo-audit-genesis")
prev_end = BASE - 1
for seq, row in enumerate(rows, 1):
    end, hashed = row.split("\t")
    end = int(float(end))
    segment = sha(hashed)
    seal = sha(segment + prev_hash)
    lines.append(f"{seq}\t{prev_end}\t{end}\t1\t{segment}\t{prev_hash}\t{seal}\t{end}")
    prev_hash, prev_end = seal, end
client("--input_format_tsv_empty_as_default=1", "-q", "INSERT INTO krateo_audit_seal SELECT seal_seq, toDateTime64(window_start, 9), toDateTime64(window_end, 9), row_count, segment_hash, prev_seal_hash, seal_hash, now64(9) FROM input('seal_seq UInt64, window_start UInt32, window_end UInt32, row_count UInt64, segment_hash String, prev_seal_hash String, seal_hash String, sealed_at UInt32') FORMAT TSV",
       stdin="\n".join(lines) + "\n")
