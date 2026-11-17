# Performance test runs, krateo-057, 2026-11-10/11

The raw inputs behind the rows in [docs/SLO.md → Performance results](../../../docs/SLO.md#performance-results).

- `span-metrics-rows.jsonl` holds every `traces.span.metrics.duration` row for `snowplow` and `krateo-frontend` in
  2026-11-10 13:50–15:50Z and 2026-11-11 09:25–10:05Z, exported read-only from ClickHouse on 2026-11-16 before the
  7-day TTL removed them. Columns: ServiceName, span.name, krateo.sli.error, http.response.status_code, TimeUnix,
  Count, BucketCounts, ExplicitBounds (delta temporality).
- `compute.py` sums buckets per window and reports calls, SLI errors, 5xx, availability, and the p50/p95 bucket.
- `results.tsv` is its output.

Run details (scenario, VUs, steps, load generator) are the CMP team's.
