#!/usr/bin/env python3
"""Functional test of THE SLI definitions (ops/slo-alert/sli-span-metrics.sql).

Runs every statement of the file, exactly as committed, with clickhouse-client's --param_start /
--param_end, in a throwaway ClickHouse of the version krateo-057 runs. otel_metrics_sum and
otel_metrics_histogram come from the DDL the daily rollup is tested against
(charts/krateo-slo-rollup/tests/otel-metrics-tables.sql), minus its TTL: the replayed X5 rows are
from 2026-11-10 and would otherwise expire under the test. The suite runs twice, once with that DDL
as is (TimeUnix DateTime) and once with TimeUnix/StartTimeUnix as DateTime64(9), the precision the
X5 export carries, so the queries are proven against either.

Cases (each in its own window, so no case sees another's rows):
  availability   per class, known calls/errors; a 4xx with krateo.sli.error false or absent is not
                 an error; other services and other metrics are left out
  burn           (errors / calls) / 0.001 per class
  widget         krateo.widget.fetch failure share
  boundary       rank 0.95*n equal to the cumulative count at 250 is IN (100, 250], not (250, 500]
  first bucket   p50 in (0, 25] reports lower 0
  overflow       a rank in the +Inf bucket reports upper inf, and misses the target
  ui names       only the four UI latency interactions are in the UI query (not krateo.widget.fetch)
  layout         two ExplicitBounds in one window return two rows, one per layout
  edges          TimeUnix == start is in, TimeUnix == end is out
  X5 replay      ops/perf/x5-2026-11-10's committed rows reproduce docs/SLO.md's X5 rows

Run: python3 tests/sli-span-metrics-docker.py [repo-root]
Exit 0 = every expectation held, 1 = at least one did not.
"""
import datetime as dt
import json
import math
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

IMAGE = "clickhouse/clickhouse-server:26.10.3.7"  # SELECT version() on krateo-057 (since 2026-11-11)
QUERIES = "ops/slo-alert/sli-span-metrics.sql"
DDL = "charts/krateo-slo-rollup/tests/otel-metrics-tables.sql"
X5_ROWS = "ops/perf/x5-2026-11-10/span-metrics-rows.jsonl"
STATEMENTS = ["availability", "api_latency", "ui_latency", "widget_fetch", "burn_rate"]

OLD = [25, 50, 100, 250, 500, 750, 1000, 1500, 2500, 5000, 10000]        # before 0.1.26
NEW = [25, 50, 100, 250, 500, 750, 1000, 1500, 2000, 2500, 5000, 10000]  # 0.1.26 and later
INF = math.inf
D = dt.datetime(2026, 11, 12)


def at(hour, minute=0, second=0, ns=0):
    t = D + dt.timedelta(hours=hour, minutes=minute, seconds=second)
    return t.strftime("%Y-%m-%d %H:%M:%S") + (f".{ns:09d}" if ns else "")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, sql, data=None, **settings):
    q = {"query": sql, **settings}
    url = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, data=data.encode() if data is not None else None, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def statements(text):
    """The file's statements, in order. Comments go first: one of them contains a ';'."""
    body = "\n".join(re.sub(r"--.*$", "", line) for line in text.splitlines())
    return [s.strip() for s in body.split(";") if s.strip()]


def attrs(span_name, err, code):
    a = {"service.name": "", "span.name": span_name, "status.code": "STATUS_CODE_UNSET"}
    if err is not None:  # None = the key is absent from the map
        a["krateo.sli.error"] = err
    if code is not None:
        a["http.response.status_code"] = code
    return a


def calls(t, svc, span_name, value, err="false", code="200", metric="traces.span.metrics.calls"):
    a = attrs(span_name, err, code)
    a["service.name"] = svc
    return {"ServiceName": svc, "MetricName": metric, "MetricUnit": "", "Attributes": a,
            "ResourceAttributes": {"service.name": svc}, "ScopeName": "spanmetricsconnector",
            "StartTimeUnix": t, "TimeUnix": t, "Value": value,
            "AggregationTemporality": 1, "IsMonotonic": True}


def hist(t, svc, span_name, buckets, bounds=NEW, err="false", code="200"):
    assert len(buckets) == len(bounds) + 1, (span_name, len(buckets), len(bounds))
    a = attrs(span_name, err, code)
    a["service.name"] = svc
    return {"ServiceName": svc, "MetricName": "traces.span.metrics.duration", "MetricUnit": "ms",
            "Attributes": a, "ResourceAttributes": {"service.name": svc},
            "ScopeName": "spanmetricsconnector", "StartTimeUnix": t, "TimeUnix": t,
            "Count": sum(buckets), "Sum": 0.0, "BucketCounts": buckets, "ExplicitBounds": bounds,
            "Min": 0.0, "Max": 0.0, "AggregationTemporality": 1}


def b(pairs, bounds=NEW):
    """Bucket counts from {bucket index: count}; index len(bounds) is the +Inf bucket."""
    out = [0] * (len(bounds) + 1)
    for i, n in pairs.items():
        out[i] = n
    return out


def lat(n, p50, p95, ok, bounds=NEW, interaction=None):
    row = {"bounds_ms": bounds, "n": n, "p50": p50, "p95": p95, "within": ok}
    if interaction:
        row["interaction"] = interaction
    return row


SP, FE = "snowplow", "krateo-frontend"

# Each case: name -> (start, end, sum rows, histogram rows, {statement: expected rows}).
# Availability / burn / widget rows are compared exactly on counts and to 1e-12 on ratios.
CASES = {
    "availability, 4xx, burn, widget share": (
        at(0), at(1),
        [
            calls(at(0, 0, 43), SP, "GET /call", 600),
            calls(at(0, 1, 43), SP, "GET /call", 2, err="true", code="500"),
            calls(at(0, 2, 43), SP, "GET /call", 300, code="404"),             # 4xx, false
            calls(at(0, 3, 43), SP, "GET /call", 98, err=None, code="404"),    # 4xx, key absent
            calls(at(0, 4, 43), FE, "krateo.widget.fetch", 380),
            calls(at(0, 5, 43), FE, "krateo.widget.fetch", 5, err="true", code=None),  # no response
            calls(at(0, 6, 43), FE, "krateo.widget.fetch", 15, code="404"),
            calls(at(0, 7, 43), FE, "krateo.page.load", 100, code=None),
            calls(at(0, 8, 43), "sse-proxy", "GET /events", 50, err="true", code="500"),  # other service
            calls(at(0, 9, 43), SP, "GET /call", 7, err="true", code="500",
                  metric="http.server.request.count"),                         # other metric
        ],
        [],
        {
            "availability": [
                {"sli_service": FE, "calls": 500, "sli_errors": 5, "availability": 0.99},
                {"sli_service": SP, "calls": 1000, "sli_errors": 2, "availability": 0.998},
            ],
            "burn_rate": [
                {"sli_service": FE, "calls": 500, "sli_errors": 5, "burn_rate": 10.0},
                {"sli_service": SP, "calls": 1000, "sli_errors": 2, "burn_rate": 2.0},
            ],
            "widget_fetch": [{"fetches": 400, "failed": 5, "failure_share": 0.0125}],
        },
    ),
    "boundary: rank == cumulative count at 250": (
        # 100 spans: 50 at <= 100, 45 more at <= 250, 5 at <= 500. Rank 95 is the cumulative count
        # at 250, so p95 is (100, 250]; rank 50 is the count at 100, so p50 is (50, 100].
        at(2), at(3),
        [],
        [
            hist(at(2, 0, 43), SP, "GET /call", b({2: 30, 3: 25, 4: 3})),
            hist(at(2, 1, 43), SP, "GET /call", b({2: 20, 3: 20, 4: 2})),
            hist(at(2, 2, 43), FE, "krateo.form.submit", b({2: 10, 3: 9, 4: 1})),  # n = 20, rank 19
        ],
        {
            "api_latency": [lat(100, (50, 100), (100, 250), 1)],
            "ui_latency": [lat(20, (50, 100), (100, 250), 1, interaction="krateo.form.submit")],
        },
    ),
    "first bucket, overflow, UI names only": (
        at(4), at(5),
        [],
        [
            hist(at(4, 0, 43), SP, "GET /call", b({0: 100})),                    # all in (0, 25]
            hist(at(4, 0, 43), FE, "krateo.page.load", b({0: 60})),
            hist(at(4, 1, 43), FE, "krateo.page.load", b({12: 40})),             # 40 in (10000, +Inf)
            hist(at(4, 2, 43), FE, "krateo.composition.open", b({4: 10})),
            hist(at(4, 3, 43), FE, "krateo.widget.fetch", b({11: 500})),         # not a latency SLI
            hist(at(4, 4, 43), FE, "GET /somewhere", b({11: 500})),              # not an interaction
        ],
        {
            "api_latency": [lat(100, (0, 25), (0, 25), 1)],
            "ui_latency": [
                lat(10, (250, 500), (250, 500), 1, interaction="krateo.composition.open"),
                lat(100, (0, 25), (10000, INF), 0, interaction="krateo.page.load"),
            ],
        },
    ),
    "layout: two ExplicitBounds in one window": (
        at(6), at(7),
        [],
        [
            hist(at(6, 0, 43), SP, "GET /call", b({3: 10}, OLD), bounds=OLD),
            hist(at(6, 1, 43), SP, "GET /call", b({8: 20}), bounds=NEW),        # (1500, 2000]
            hist(at(6, 2, 43), FE, "krateo.page.load", b({8: 4}, OLD), bounds=OLD),  # (1500, 2500]
            hist(at(6, 3, 43), FE, "krateo.page.load", b({8: 6}), bounds=NEW),
        ],
        {
            "api_latency": [lat(10, (100, 250), (100, 250), 1, bounds=OLD),
                            lat(20, (1500, 2000), (1500, 2000), 0, bounds=NEW)],
            "ui_latency": [lat(4, (1500, 2500), (1500, 2500), 0, bounds=OLD, interaction="krateo.page.load"),
                           lat(6, (1500, 2000), (1500, 2000), 1, bounds=NEW, interaction="krateo.page.load")],
        },
    ),
    "edges: start in, end out": (
        at(8), at(9),
        [
            calls(at(7, 59, 59), SP, "GET /call", 10000, err="true", code="500"),  # before start
            calls(at(8), SP, "GET /call", 7),                                       # == start: in
            calls(at(8, 59, 59, 999999999), SP, "GET /call", 1, err="true", code="500"),  # just in
            calls(at(9), SP, "GET /call", 1000, err="true", code="500"),            # == end: out
        ],
        [
            hist(at(7, 59, 59), SP, "GET /call", b({12: 1000})),
            hist(at(8), SP, "GET /call", b({1: 7})),
            hist(at(8, 59, 59, 999999999), SP, "GET /call", b({1: 1})),
            hist(at(9), SP, "GET /call", b({12: 1000})),
        ],
        {
            "availability": [{"sli_service": SP, "calls": 8, "sli_errors": 1, "availability": 1 - 1 / 8}],
            "api_latency": [lat(8, (25, 50), (25, 50), 1)],
            "burn_rate": [{"sli_service": SP, "calls": 8, "sli_errors": 1, "burn_rate": (1 / 8) / (1 - 0.999)}],
        },
    ),
}

# docs/SLO.md "Performance results", from the committed X5 rows (ops/perf/x5-2026-11-10). Calls rows
# are the histogram rows' Count: the connector derives both metrics from the same spans, so a row's
# calls Value equals its Count. Windows are compute.py's; none ends on a flush, so [start, end)
# selects the same rows as its inclusive range.
X5 = [
    ("A steps 1-3, API baseline", "2026-11-10 13:56:52", "2026-11-10 14:17:50",
     {"availability:snowplow": (129939, 0, "100.000"), "api_latency": [((0, 25), (0, 25), 1)]}),
    ("A all steps, API stress", "2026-11-10 13:56:52", "2026-11-10 14:28:00",
     {"availability:snowplow": (288271, 114, "99.960"), "api_latency": [((0, 25), (0, 25), 1)]}),
    ("A step 4", "2026-11-10 14:18:15", "2026-11-10 14:28:00",
     {"availability:snowplow": (158332, 114, "99.928")}),
    ("B UI key actions", "2026-11-10 15:12:00", "2026-11-10 15:41:00",
     # The window also holds one krateo.page.load span (the row does not report page load).
     {"ui_latency": {"krateo.composition.open": (60, (250, 500), (250, 500)),
                     "krateo.form.submit": (60, (50, 100), (100, 250)),
                     "krateo.page.load": (1, (750, 1000), (750, 1000))},
      "widget_fetch": (4715, 0)}),
    ("page load after 0.1.26", "2026-11-11 09:25:00", "2026-11-11 10:05:00",
     {"ui_latency": {"krateo.page.load": (60, (500, 750), (500, 750))}}),
]


def num(v):
    return INF if v == "inf" else v


def norm_lat(r):
    out = {"bounds_ms": [num(x) for x in r["bounds_ms"]], "n": int(r["n"]),
           "p50": (num(r["p50_lower_ms"]), num(r["p50_upper_ms"])),
           "p95": (num(r["p95_lower_ms"]), num(r["p95_upper_ms"])),
           "within": int(r["p95_within_target"])}
    if "interaction" in r:
        out["interaction"] = r["interaction"]
    return out


def same(have, want):
    """Exact on everything but float ratios, which are held to 1e-12."""
    if isinstance(want, float) and isinstance(have, (int, float)):
        return math.isclose(have, want, rel_tol=1e-12, abs_tol=0) or have == want
    if isinstance(want, dict):
        return isinstance(have, dict) and have.keys() == want.keys() and all(same(have[k], want[k]) for k in want)
    if isinstance(want, (list, tuple)):
        return isinstance(have, (list, tuple)) and len(have) == len(want) and all(map(same, have, want))
    return have == want


class Runner:
    def __init__(self, container, db, stmts):
        self.container, self.db, self.stmts = container, db, stmts

    def run(self, which, start, end):
        """One statement, run the way docs/SLO.md tells a reader to: clickhouse-client --param_*."""
        sql = self.stmts[STATEMENTS.index(which)]
        out = subprocess.run(
            ["docker", "exec", self.container, "clickhouse-client", "--database", self.db,
             f"--param_start={start}", f"--param_end={end}", "--format", "JSONEachRow",
             "--output_format_json_quote_64bit_integers=0", "--output_format_json_quote_denormals=1",
             "--query", sql],
            check=True, capture_output=True, text=True).stdout
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        if which in ("api_latency", "ui_latency"):
            # The API query has no ORDER BY and the UI one orders by interaction only, so rows of two
            # layouts come back in either order: compare by (interaction, number of bounds).
            return sorted((norm_lat(r) for r in rows),
                          key=lambda x: (x.get("interaction", ""), len(x["bounds_ms"])))
        return [{k: (int(v) if k in ("calls", "sli_errors", "fetches", "failed") else v) for k, v in r.items()}
                for r in rows]


def check_cases(r, label):
    fails = 0
    for name, (start, end, _, _, expect) in CASES.items():
        for which, want in expect.items():
            have = r.run(which, start, end)
            ok = same(have, want)
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} [{label}] {name} / {which}"
                  + ("" if ok else f"\n       got      {have}\n       expected {want}"))
    return fails


def check_x5(r, label):
    fails = 0
    for name, start, end, expect in X5:
        for which, want in expect.items():
            if which.startswith("availability:"):
                svc = which.split(":", 1)[1]
                rows = {x["sli_service"]: x for x in r.run("availability", start, end)}
                x = rows.get(svc)
                have = None if x is None else (x["calls"], x["sli_errors"], f"{100 * x['availability']:.3f}")
            elif which == "api_latency":
                have = [(x["p50"], x["p95"], x["within"]) for x in r.run(which, start, end)]
            elif which == "ui_latency":
                have = {x["interaction"]: (x["n"], x["p50"], x["p95"]) for x in r.run(which, start, end)}
            else:
                have = [(x["fetches"], x["failed"]) for x in r.run(which, start, end)]
                want = [want]
            ok = same(have, want)
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} [{label}] X5 {name} / {which}: got {have}"
                  + ("" if ok else f", expected {want}"))
    return fails


def x5_rows(root):
    sums, hists = [], []
    for line in (root / X5_ROWS).read_text().splitlines():
        x = json.loads(line)
        buckets = [int(v) for v in x["BucketCounts"]]
        hists.append(hist(x["ts"], x["ServiceName"], x["sn"], buckets, bounds=x["ExplicitBounds"],
                          err=x["err"], code=x["sc"] or None))
        sums.append(calls(x["ts"], x["ServiceName"], x["sn"], int(x["Count"]), err=x["err"], code=x["sc"] or None))
    return sums, hists


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    stmts = statements((root / QUERIES).read_text())
    if len(stmts) != len(STATEMENTS):
        print(f"FAIL {QUERIES} has {len(stmts)} statements, the test knows {len(STATEMENTS)}: {STATEMENTS}")
        return 1
    ddl = (root / DDL).read_text()
    ddl = re.sub(r"^TTL .*\n", "", ddl, flags=re.M)  # replayed X5 rows are older than the TTL
    if "TTL" in ddl:
        print(f"FAIL could not strip the TTL from {DDL}")
        return 1
    schemas = {
        "rollup_ddl": ddl,
        "dt64": ddl.replace("`StartTimeUnix` DateTime CODEC(Delta(4),", "`StartTimeUnix` DateTime64(9) CODEC(Delta(8),")
                   .replace("`TimeUnix` DateTime CODEC(Delta(4),", "`TimeUnix` DateTime64(9) CODEC(Delta(8),"),
    }
    if schemas["dt64"].count("DateTime64(9)") != 4:
        print("FAIL could not derive the DateTime64(9) schema from the rollup DDL")
        return 1

    sums = [s for c in CASES.values() for s in c[2]]
    hists = [h for c in CASES.values() for h in c[3]]
    xs, xh = x5_rows(root)

    port = free_port()
    name = f"sli-span-metrics-test-{port}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8123",
                    "-e", "CLICKHOUSE_SKIP_USER_SETUP=1", IMAGE], check=True, capture_output=True)
    fails = 0
    try:
        for _ in range(90):
            try:
                if http(port, "SELECT 1").strip() == "1":
                    break
            except Exception:
                time.sleep(1)
        else:
            print("FAIL ClickHouse never answered")
            return 1
        for db, schema in schemas.items():
            http(port, f"CREATE DATABASE {db}")
            for stmt in (s for s in schema.split(";") if s.strip() and "CREATE TABLE" in s):
                http(port, stmt, database=db)
            for table, rows in (("otel_metrics_sum", sums + xs), ("otel_metrics_histogram", hists + xh)):
                http(port, f"INSERT INTO {table} FORMAT JSONEachRow", "\n".join(json.dumps(x) for x in rows),
                     database=db, date_time_input_format="best_effort")
            types = http(port, "SELECT DISTINCT type FROM system.columns WHERE database = currentDatabase() "
                               "AND name = 'TimeUnix'", database=db).split()
            print(f"--- {db}: TimeUnix {types}, {len(sums) + len(xs)} calls rows, {len(hists) + len(xh)} histogram rows")
            r = Runner(name, db, stmts)
            fails += check_cases(r, db)
            fails += check_x5(r, db)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    print(f"{'PASS' if not fails else 'FAIL'}: {fails} failed expectation(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
