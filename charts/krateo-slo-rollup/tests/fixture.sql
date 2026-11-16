-- Delta rows for two days (today-3, today-2): snowplow (the API SLI class) on two routes and
-- krateo-frontend (the UI SLI class) on three span names, some with errors, in the bucket layout the
-- collector had before otel-collector-daemonset 0.1.26 (11 bounds, 12 buckets; 0.1.26 added 2000 ms).
-- e2e-kind.sh reads the rollup with the 30-day statements of ops/slo-alert/sli-span-metrics.sql and
-- works out what they must return by hand. The other rows are traps the rollup must leave out or
-- keep apart: cumulative rows, other metrics in the same tables, and a service whose bucket layout
-- changes mid-day; krateo.widget.fetch is in the rollup but is not a UI latency interaction.
INSERT INTO otel_metrics_sum (ServiceName, MetricName, Attributes, StartTimeUnix, TimeUnix, Value, AggregationTemporality, IsMonotonic)
SELECT service, metric, attrs, t - 60, t, v, temporality, true FROM (
  SELECT 'snowplow' AS service, 'traces.span.metrics.calls' AS metric, map('span.name','GET /call','http.route','/call','krateo.sli.error','false') AS attrs, toDateTime(today() - 3) + 3600 AS t, 600 AS v, 1 AS temporality
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.calls', map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 3) + 7200, 300, 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.calls', map('span.name','GET /call','http.route','/call','krateo.sli.error','true'),  toDateTime(today() - 3) + 7200, 10, 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.calls', map('span.name','GET /list','http.route','/list','krateo.sli.error','true'),  toDateTime(today() - 3) + 3600, 90, 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.calls', map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, 500, 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.calls', map('span.name','GET /list','http.route','/list','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, 1000, 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.calls', map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, 99999, 2
  UNION ALL SELECT 'snowplow', 'http.server.requests',      map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, 77777, 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.calls', map('span.name','krateo.page.load','http.route','/dashboard','krateo.sli.error','false'),   toDateTime(today() - 3) + 3600, 39, 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.calls', map('span.name','krateo.page.load','http.route','/dashboard','krateo.sli.error','true'),    toDateTime(today() - 2) + 3600, 1, 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.calls', map('span.name','krateo.form.submit','http.route','/dashboard','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, 2, 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.calls', map('span.name','krateo.widget.fetch','http.route','/call','krateo.sli.error','false'),    toDateTime(today() - 2) + 3600, 58, 1
);

INSERT INTO otel_metrics_histogram (ServiceName, MetricName, Attributes, StartTimeUnix, TimeUnix, Count, Sum, BucketCounts, ExplicitBounds, AggregationTemporality)
SELECT service, metric, attrs, t - 60, t, arraySum(b), 0, b, bounds, temporality FROM (
  SELECT 'snowplow' AS service, 'traces.span.metrics.duration' AS metric, map('span.name','GET /call','http.route','/call','krateo.sli.error','false') AS attrs, toDateTime(today() - 3) + 3600 AS t, [0,0,100,500,0,0,0,0,0,0,0,0]::Array(UInt64) AS b, [25,50,100,250,500,750,1000,1500,2500,5000,10000]::Array(Float64) AS bounds, 1 AS temporality
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.duration', map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 3) + 7200, [0,0,100,100,100,0,0,0,0,0,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.duration', map('span.name','GET /call','http.route','/call','krateo.sli.error','true'),  toDateTime(today() - 3) + 7200, [0,0,0,0,0,0,10,0,0,0,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.duration', map('span.name','GET /list','http.route','/list','krateo.sli.error','true'),  toDateTime(today() - 3) + 3600, [0,0,0,0,0,0,0,0,40,50,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.duration', map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, [0,0,0,300,200,0,0,0,0,0,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.duration', map('span.name','GET /list','http.route','/list','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, [0,500,400,100,0,0,0,0,0,0,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'snowplow', 'traces.span.metrics.duration', map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, [0,0,0,0,0,0,0,0,0,0,0,99999], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 2
  UNION ALL SELECT 'snowplow', 'http.server.duration',         map('span.name','GET /call','http.route','/call','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, [0,0,0,0,0,0,0,0,0,0,0,77777], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.duration', map('span.name','krateo.page.load','http.route','/dashboard','krateo.sli.error','false'),   toDateTime(today() - 3) + 3600, [0,0,0,0,0,20,10,9,0,0,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.duration', map('span.name','krateo.page.load','http.route','/dashboard','krateo.sli.error','true'),    toDateTime(today() - 2) + 3600, [0,0,0,0,0,0,0,0,0,1,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.duration', map('span.name','krateo.form.submit','http.route','/dashboard','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, [0,0,0,0,0,0,0,0,0,0,0,2], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'krateo-frontend', 'traces.span.metrics.duration', map('span.name','krateo.widget.fetch','http.route','/call','krateo.sli.error','false'),    toDateTime(today() - 2) + 3600, [0,58,0,0,0,0,0,0,0,0,0,0], [25,50,100,250,500,750,1000,1500,2500,5000,10000], 1
  UNION ALL SELECT 'bounds-changed', 'traces.span.metrics.duration', map('span.name','GET /b','http.route','/b','krateo.sli.error','false'), toDateTime(today() - 2) + 3600, [1,2,0], [100,200], 1
  UNION ALL SELECT 'bounds-changed', 'traces.span.metrics.duration', map('span.name','GET /b','http.route','/b','krateo.sli.error','false'), toDateTime(today() - 2) + 7200, [1,1,1,1], [100,200,300], 1
);
