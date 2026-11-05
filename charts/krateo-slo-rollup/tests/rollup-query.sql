-- Error ratio and p95 from the rollup, per route and over all routes: the query the README
-- documents. FINAL because each refresh writes a day again. p95 is read off the summed buckets by
-- linear interpolation inside the bucket that holds the rank, and only when one bucket layout is
-- in scope (buckets from two layouts cannot be summed). Past the last bound it reads that bound.
SELECT route, requests, errors, error_ratio, p95_ms FROM (
    SELECT
        if(grouping(r) = 1, '(all routes)', r)                                     AS route,
        sum(calls)                                                                 AS requests,
        sumIf(calls, sli_error)                                                    AS errors,
        if(requests = 0, NULL, round(errors / requests, 5))                        AS error_ratio,
        sumForEach(bucket_counts)                                                  AS buckets,
        anyIf(bounds, notEmpty(bounds))                                            AS b,
        arrayCumSum(buckets)                                                       AS cum,
        arraySum(buckets) * 0.95                                                   AS p95_rank,
        arrayFirstIndex(x -> x >= p95_rank, cum)                                   AS i,
        if(uniqExactIf(bounds, notEmpty(bounds)) != 1, NULL,
           if(i > length(b), b[length(b)],
              round(b[i - 1] + (b[i] - b[i - 1]) * (p95_rank - cum[i - 1]) / buckets[i], 1))) AS p95_ms,
        grouping(r)                                                                AS g
    FROM (
        SELECT if(route = '', if(span_name = '', '(overflow)', span_name), route) AS r, calls, sli_error, bounds, bucket_counts
        FROM krateo_slo_daily FINAL
        WHERE service = 'snowplow'
    )
    GROUP BY GROUPING SETS ((r), ())
)
ORDER BY g DESC, route;
