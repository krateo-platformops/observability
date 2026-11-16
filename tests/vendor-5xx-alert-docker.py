#!/usr/bin/env python3
"""The two vendor 5xx alerts: the rendered Alerts, and a replay of krateo-057's vendor 5xx outage.

  fast        sre-rest-controller-vendor-5xx             >= 20 rows in 2 consecutive 15m windows
  persistent  sre-rest-controller-vendor-5xx-persistent  >= 4 rows in 4 consecutive 15m windows

1. Render. Renders charts/krateo-observability and checks each Alert's spec: where (the same for
   both), threshold, thresholdType, interval, consecutiveWindows, and no groupBy or metricRef
   (alert-provider refuses groupBy without metricRef, so both count every kind together).

2. Replay. Starts a throwaway ClickHouse of the version krateo-057 runs, creates otel_logs from 057's
   own DDL (tests/fixtures/otel_logs.sql, from SHOW CREATE TABLE, minus its 7-day TTL: the replayed
   rows are dated 2026-11), and loads synthetic rows shaped like rest-dynamic-controller's log lines.
   The 057 failures reproduce, minute by minute, the counts saved in
   tests/fixtures/vendor-5xx-krateo-057.json: kind, status, rows and distinct resources, nothing
   else. No real err text is used: err is rest-dynamic-controller's own format
   (internal/tools/client/validators.go, "unexpected status: %d: invalid status code: %d") with the
   status filled in, and resource names are made up.

   The RENDERED where is counted per epoch-aligned 15-minute bucket, the way HyperDX buckets a tile
   alert, and walked through HyperDX's state rules: `above` is count >= threshold, an empty window
   counts 0, an alert fires after consecutiveWindows breaching windows in a row (PENDING before
   that, which notifies nothing) and resolves on the first window that does not breach.

   ROWS PER FAILED PASS. A failed observe logs one line, and the runtime's error retries (1s base,
   doubling, at most 5: REST_CONTROLLER_MIN_ERROR_RETRY_INTERVAL / _MAX_ERROR_RETRIES) log one more
   each until one succeeds. So one resource's failed resync is 1 to 6 rows. On 057: 2 per resource
   per pass in the 2026-11-12 503 outage (the retry ~1.4 s later failed too), 6 in 31 s for one
   resource on 2026-11-10 (retries at 1, 2, 4, 8, 16 s). The single-resource cases run the models
   ONE (1 row a pass, the fewest), TWO (the outage's shape) and SIX (hard down).

   Cases:
     (a) outage    2026-11-12, 17 PullRequests, 503: per-window counts equal 057's. fast: PENDING
                   after the 13:37 burst alone, ALERT when the 14:45 window closes (15:00), OK when
                   the 15:30 window closes (15:45; last failure 15:25:59). persistent: ALERT when
                   the 15:15 window closes (15:30), OK at 15:45
     (b) blips     500 x4, 502 x2, 503 x1, 504 x1: both OK
     (e) quiet     2026-11-15, only healthy lines: both OK all day
     controls      2026-11-14, two hours each at outage rates, none counted: 404s, an err with no
                   status, "retry after 500ms", a composition-dynamic-controller 503, HyperDX
                   echoing the query; and with the status or the service clause removed they count
     many          2026-11-13, three resources of one kind at TWO: both fire, both resolve
     (c) 75 min    one resource failing every 3-minute resync, at ONE and at TWO, five start phases
                   each: persistent fires, fast never
     (d) 45 min    one resource at ONE, five start phases: neither fires. At TWO the persistent
                   alert stays silent from an aligned start and fires from a 7-minute offset, when
                   the 45 minutes touch 4 windows (a limit, asserted so it stays visible)
     (f) jitter    75 min at ONE, each resync shifted by up to +-20 s: persistent fires
     (g) 4 of 5    75 min at ONE, failing 4 of every 5 passes: persistent fires
     (h) 4 rows    one window of 4 rows, then quiet: neither fires
     (i) hard down one resource for 45 min at SIX, from an aligned start: fast fires on its own

Run: python3 tests/vendor-5xx-alert-docker.py [repo-root]
Exit 0 = every expectation held, 1 = at least one did not.
"""
import datetime as dt
import json
import pathlib
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import yaml

IMAGE = "clickhouse/clickhouse-server:26.10.3.7"  # SELECT version() on krateo-057 (since 2026-11-11)
DDL = "tests/fixtures/otel_logs.sql"
COUNTS = "tests/fixtures/vendor-5xx-krateo-057.json"
CHART = "charts/krateo-observability"
FAST = "sre-rest-controller-vendor-5xx"
PERSISTENT = "sre-rest-controller-vendor-5xx-persistent"

WHERE = ("ResourceAttributes['telemetry.source'] = '' AND JSONExtractString(Body, 'service') = "
         "'rest-dynamic-controller' AND JSONExtractString(Body, 'msg') = 'Cannot observe external "
         "resource' AND match(JSONExtractString(Body, 'err'), 'status( code)?: 5[0-9][0-9]')")
SPECS = {
    FAST: {"where": WHERE, "threshold": 20, "thresholdType": "above", "interval": "15m",
           "consecutiveWindows": 2},
    PERSISTENT: {"where": WHERE, "threshold": 4, "thresholdType": "above", "interval": "15m",
                 "consecutiveWindows": 4},
}

# Retry offsets (seconds after the pass) of the rows one failed resync logs for one resource.
ONE = (0,)
TWO = (0, 1.4)                   # 2026-11-12 outage: the pass, and its first retry ~1.4 s later
SIX = (0, 1, 3, 7, 15, 31)       # 2026-11-10: the pass and 5 retries at 1, 2, 4, 8, 16 s
RESYNC = 180                     # REST_CONTROLLER_RESYNC_INTERVAL default, unset on all 34 on 057

WINDOW = dt.timedelta(minutes=15)
NS = "krateo-system"
FAILS = 0


def check(ok, msg):
    global FAILS
    print(f"{'ok  ' if ok else 'FAIL'} {msg}")
    FAILS += not ok


def t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------------------------
# 1. Render
# ---------------------------------------------------------------------------------------------

def render(root):
    chart = root / CHART
    subprocess.run(["helm", "dependency", "build", str(chart)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "x", str(chart), "-n", NS],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Alert"]
    specs = {}
    for name, want_spec in SPECS.items():
        alerts = [d for d in docs if d["metadata"]["name"] == name]
        check(len(alerts) == 1, f"{name} renders once with the default values ({len(alerts)})")
        if not alerts:
            continue
        spec = specs[name] = alerts[0]["spec"]
        for k, want in want_spec.items():
            got = spec.get(k)
            check(got == want, f"{name}: spec.{k} = {got!r}" + ("" if got == want else f", want {want!r}"))
        check(not any(k in spec for k in ("groupBy", "metricRef", "aggregation", "value", "source")),
              f"{name}: no groupBy, metricRef, aggregation, value or source (a log count over every kind)")
        rb = spec.get("runbook", "")
        check(bool(rb) and "{{" not in rb and not re.search(r"(^|\s)@[A-Za-z]", rb)
              and len(spec.get("description", "")) + 2 + len(rb) <= 4096,
              f"{name}: runbook is set and HyperDX-safe ({len(rb)} characters)")
        check("'kind') AS kind" in rb and ") AS status" in rb and "'err') AS" not in rb
              and "vendorResponse'" not in rb and "Do not select err" in rb,
              f"{name}: runbook's query names kind and status and never returns err itself")
        check("10 rows per resource" not in rb, f"{name}: runbook does not claim 10 rows per resource")
    return specs if len(specs) == len(SPECS) else None


# ---------------------------------------------------------------------------------------------
# 2. Replay
# ---------------------------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ch(port, sql, data=None):
    url = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode({"query": sql, "session_timezone": "UTC"})
    req = urllib.request.Request(url, data=data.encode() if data is not None else None, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def status_err(code):
    return f"unexpected status: {code}: invalid status code: {code}"


def row(ts, body, service="rest-dynamic-controller", deployment=None, source=""):
    """A container log line as the daemonset's filelog writes it: the JSON line in Body, the
    Deployment as ServiceName and k8s.deployment.name."""
    dep = deployment or "github-provider-kog-pullrequest-controller"
    res = {"k8s.deployment.name": dep, "k8s.namespace.name": NS,
           "k8s.pod.name": f"{dep}-5d8f7c9b6-abcde", "service.name": dep}
    if source is not None:
        res["telemetry.source"] = source
    if isinstance(body, dict):
        body = json.dumps({"level": body.pop("level", "error"), "service": service, **body})
    return {"Timestamp": ts.strftime("%Y-%m-%d %H:%M:%S.%f"), "ServiceName": dep, "Body": body,
            "ResourceAttributes": res, "SeverityText": "", "LogAttributes": {}}


def observe_fail(ts, kind, i, err, msg="Cannot observe external resource", **kw):
    return row(ts, {"msg": msg, "kind": kind, "op": "observe", "name": f"{kind.lower()}-{i:02d}",
                    "namespace": "demo", "err": err}, **kw)


def healthy(ts, kind, i, **kw):
    return row(ts, {"level": "info", "msg": "External resource is up to date", "kind": kind,
                    "name": f"{kind.lower()}-{i:02d}", "namespace": "demo"}, **kw)


def spread(minute, n):
    """n instants inside one minute, the last at :59, as 057's last failure was 15:25:59."""
    return [minute + dt.timedelta(seconds=59 * (j + 1) / n) for j in range(n)]


def burst(start, end, every_min, resources, make):
    """A failed resync of `resources` resources every `every_min` minutes, two rows each (TWO)."""
    rows, m = [], start
    while m < end:
        for j, ts in enumerate(spread(m, 2 * resources)):
            rows.append(make(ts, j % resources))
        m += dt.timedelta(minutes=every_min)
    return rows


def single(start, minutes, model, jitter=0, fails=lambda k: True, kind="Repository"):
    """One resource whose resyncs, every RESYNC seconds from start for `minutes`, fail with 503 (pass
    k fails when fails(k)), each logging the rows of `model`. jitter shifts pass k by a fixed
    pseudo-random amount in [-jitter, +jitter] seconds, as client-go's resync does."""
    rows, k = [], 0
    while True:
        base = start + dt.timedelta(seconds=k * RESYNC)
        if base >= start + dt.timedelta(minutes=minutes):
            return rows
        shift = ((k * 7919) % (2 * jitter + 1)) - jitter if jitter else 0
        if fails(k):
            for off in model:
                ts = base + dt.timedelta(seconds=shift + off)
                rows.append(observe_fail(ts, kind, 7, status_err(503),
                                         deployment=f"github-provider-kog-{kind.lower()}-controller"))
        k += 1


PHASES = (0, 3, 7, 9, 12)    # minutes after a window boundary that an outage starts


def cases():
    """Single-resource cases: (label, day, [(start, minutes, model, kwargs)], expectations).
    Each case has its own day; within a day, runs are 3 hours apart, so each starts from OK."""
    out = []
    day = t("2026-11-20T00:00:00Z")

    def runs(minutes, model, **kw):
        return [(day + dt.timedelta(hours=1 + 3 * i, minutes=p), minutes, model, kw)
                for i, p in enumerate(PHASES)]

    spec = [
        ("(c) one resource, 75 min, 1 row a pass", lambda: runs(75, ONE), {PERSISTENT: True, FAST: False}),
        ("(c) one resource, 75 min, 2 rows a pass", lambda: runs(75, TWO), {PERSISTENT: True, FAST: False}),
        ("(d) one resource, 45 min, 1 row a pass", lambda: runs(45, ONE), {PERSISTENT: False, FAST: False}),
        ("(d) one resource, 45 min, 2 rows a pass, aligned start",
         lambda: [(day + dt.timedelta(hours=1), 45, TWO, {})], {PERSISTENT: False, FAST: False}),
        ("(d) LIMIT: one resource, 45 min, 2 rows a pass, starting 7 min into a window",
         lambda: [(day + dt.timedelta(hours=1, minutes=7), 45, TWO, {})], {PERSISTENT: True, FAST: False}),
        ("(f) one resource, 75 min, 1 row a pass, resync jittered +-20 s",
         lambda: runs(75, ONE, jitter=20), {PERSISTENT: True, FAST: False}),
        ("(g) one resource, 75 min, 1 row a pass, failing 4 of every 5 passes",
         lambda: runs(75, ONE, fails=lambda k: k % 5 != 4), {PERSISTENT: True, FAST: False}),
        ("(i) one resource hard down, 45 min, 6 rows a pass, aligned start",
         lambda: [(day + dt.timedelta(hours=1), 45, SIX, {})], {FAST: True, PERSISTENT: False}),
    ]
    for label, mk, want in spec:
        out.append((label, day, mk(), want))
        day += dt.timedelta(days=1)
    # (h): a single 4-row window, then quiet.
    four = [observe_fail(day + dt.timedelta(hours=1, minutes=2 + 3 * j), "Repository", j, status_err(503),
                         deployment="github-provider-kog-repository-controller") for j in range(4)]
    out.append(("(h) a single 4-row window, then quiet", day, four, {PERSISTENT: False, FAST: False}))
    return out


def build(fixture, single_cases):
    rows = []
    # The 057 failures, minute by minute, each with its "Performing REST call" twin.
    for e in fixture["minutes"]:
        m = t(e["minute"])
        for j, ts in enumerate(spread(m, e["rows"])):
            dep = f"github-provider-kog-{e['kind'].lower()}-controller"
            rows.append(observe_fail(ts, e["kind"], j % e["resources"], status_err(e["status"]), deployment=dep))
            rows.append(observe_fail(ts, e["kind"], j % e["resources"], status_err(e["status"]),
                                     msg="Performing REST call", deployment=dep))
    # Healthy resyncs of the 17 PullRequests around the outage, and all of the quiet day.
    for start, end in ((t("2026-11-12T12:00:00Z"), t("2026-11-12T17:00:00Z")),
                       (t("2026-11-15T00:00:00Z"), t("2026-11-16T00:00:00Z"))):
        m = start
        while m < end:
            rows += [healthy(m + dt.timedelta(seconds=i), "PullRequest", i) for i in range(17)]
            m += dt.timedelta(minutes=3)
    # Controls on 2026-11-14, each two hours at the outage's rate: none may count.
    d = t("2026-11-14T10:00:00Z")
    two = d + dt.timedelta(hours=2)
    rows += burst(d, two, 3, 17, lambda ts, i: observe_fail(ts, "PullRequest", i, status_err(404)))
    rows += burst(d, two, 3, 17, lambda ts, i: observe_fail(
        ts, "Repository", i, 'Get "https://api.example.invalid/repos/x": context deadline exceeded',
        deployment="github-provider-kog-repository-controller"))
    rows += burst(d, two, 3, 17, lambda ts, i: observe_fail(
        ts, "Repository", i, "rate limited, retry after 500ms",
        deployment="github-provider-kog-repository-controller"))
    rows += burst(d, two, 3, 17, lambda ts, i: observe_fail(
        ts, "Installer", i, status_err(503), service="composition-dynamic-controller",
        deployment="installers-v0-3-668-controller"))
    rows += burst(d, two, 3, 17, lambda ts, i: row(
        ts, f"executing alert query: SELECT count() FROM otel_logs WHERE {WHERE}",
        deployment="krateo-clickstack-app"))
    # Three resources of one kind failing every resync, 2026-11-13.
    rows += burst(t("2026-11-13T13:00:00Z"), t("2026-11-13T14:00:00Z"), 3, 3,
                  lambda ts, i: observe_fail(ts, "Branch", i, status_err(502),
                                             deployment="github-provider-kog-branch-controller"))
    for _, _, runs_or_rows, _ in single_cases:
        for r in runs_or_rows:
            if isinstance(r, tuple):
                start, minutes, model, kw = r
                rows += single(start, minutes, model, **kw)
            else:
                rows.append(r)
    return rows


def windows(port, where, start, end):
    """The alert's value per epoch-aligned 15-minute bucket, every bucket from start to end, an
    empty one 0 (HyperDX evaluates an empty window of an ungrouped count as 0)."""
    out = ch(port, f"SELECT toUnixTimestamp(toStartOfInterval(Timestamp, INTERVAL 15 MINUTE)) AS b, "
                   f"count() FROM otel_logs WHERE ({where}) GROUP BY b ORDER BY b FORMAT TSV")
    got = {int(b): int(n) for b, n in (l.split("\t") for l in out.split("\n") if l)}
    series, b = [], start
    while b < end:
        series.append((b, got.get(int(b.timestamp()), 0)))
        b += WINDOW
    return series


def states(series, spec):
    """HyperDX's state after each window closes: (window start, count, state)."""
    thr, n = spec["threshold"], spec["consecutiveWindows"]
    assert spec["thresholdType"] == "above"  # count >= threshold (checkAlerts doesExceedThreshold)
    out, streak = [], 0
    for b, v in series:
        streak = streak + 1 if v >= thr else 0
        out.append((b, v, "ALERT" if streak >= n else "PENDING" if streak else "OK"))
    return out


def at(timeline, when):
    return next(x for x in timeline if x[0] == t(when))


def short(name):
    return "persistent" if name == PERSISTENT else "fast"


def replay(root, specs):
    fixture = json.loads((root / COUNTS).read_text())
    ddl = (root / DDL).read_text()
    ddl = re.sub(r"\nTTL [^\n]*", "", ddl)
    check("TTL" not in ddl, "otel_logs DDL without its TTL (the replayed rows are dated 2026-11)")
    where = specs[FAST]["where"]
    single_cases = cases()
    port = free_port()
    name = f"vendor-5xx-test-{port}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8123",
                    "-e", "CLICKHOUSE_SKIP_USER_SETUP=1", IMAGE], check=True, capture_output=True)
    try:
        for _ in range(90):
            try:
                if ch(port, "SELECT 1").strip() == "1":
                    break
            except Exception:
                time.sleep(1)
        else:
            check(False, "ClickHouse never answered")
            return
        ch(port, ddl)
        rows = build(fixture, single_cases)
        for i in range(0, len(rows), 20000):
            ch(port, "INSERT INTO otel_logs FORMAT JSONEachRow",
               "\n".join(json.dumps(r) for r in rows[i:i + 20000]))
        total = int(ch(port, "SELECT count() FROM otel_logs").strip())
        check(total == len(rows), f"{total} synthetic rows loaded")

        start, end = t(fixture["from"]), t(fixture["to"])
        series = windows(port, where, start, end)
        tl = {n: states(series, s) for n, s in specs.items()}
        synthetic = (t("2026-11-13T00:00:00Z"), t("2026-11-15T00:00:00Z"))
        replayed = {b: v for b, v in series if not synthetic[0] <= b < synthetic[1] and v}
        want = {t(k): v for k, v in fixture["windows15m"].items()}
        check(replayed == want, "per-window counts of the rendered where equal the ones measured on 057 "
              "(each failure loaded with its 'Performing REST call' twin): "
              + ", ".join(f"{b:%m-%d %H:%M} {v}" for b, v in sorted(replayed.items())))

        # (a) the outage
        print("\n  (a) the outage, 2026-11-12 (state after each window closes):   fast  persistent")
        for (b, v, s), (_, _, p) in zip(tl[FAST], tl[PERSISTENT]):
            if t("2026-11-12T13:15:00Z") <= b <= t("2026-11-12T16:00:00Z"):
                print(f"    {b:%H:%M}-{b + WINDOW:%H:%M}  {v:4d}  {s:7s}  {p}")
        print()
        f, p = tl[FAST], tl[PERSISTENT]
        check(at(f, "2026-11-12T13:30:00Z")[2] == "PENDING" and at(f, "2026-11-12T13:45:00Z")[2] == "OK",
              "(a) fast: the 13:37 burst alone is PENDING, then OK")
        check(at(f, "2026-11-12T14:30:00Z")[2] == "PENDING" and at(f, "2026-11-12T14:45:00Z")[2] == "ALERT",
              "(a) fast: fires when the 14:45 window closes (15:00)")
        check(all(at(f, w)[2] == "ALERT" for w in ("2026-11-12T15:00:00Z", "2026-11-12T15:15:00Z")),
              "(a) fast: stays ALERT through the 15:15 window")
        last = t(fixture["lastFailure"])
        check(at(f, "2026-11-12T15:30:00Z")[2] == "OK",
              f"(a) fast: clears when the 15:30 window closes (15:45, 19 minutes after the last failure at {last:%H:%M:%S})")
        p_alert = [b for b, _, s in p if s == "ALERT" and b.date() == dt.date(2026, 11, 12)]
        check(p_alert == [t("2026-11-12T15:15:00Z")] and at(p, "2026-11-12T15:30:00Z")[2] == "OK",
              "(a) persistent: ALERT only when the 15:15 window closes (15:30), OK at 15:45: "
              f"{[f'{b + WINDOW:%H:%M}' for b in p_alert]}")

        # (b) blips
        for w, what in (("2026-11-10T16:00:00Z", "500 x4 Repository"), ("2026-11-10T21:15:00Z", "502 x2 PullRequest"),
                        ("2026-11-11T15:00:00Z", "503 x1 PullRequest"), ("2026-11-16T06:30:00Z", "504 x1 Repository")):
            fv, ps = at(f, w), at(p, w)
            check(fv[2] == "OK" and ps[2] in ("OK", "PENDING") and
                  all(s != "ALERT" for b, _, s in p if t(w) <= b < t(w) + 4 * WINDOW),
                  f"(b) blip {what} at {t(w):%m-%d %H:%M}: {fv[1]} rows; fast {fv[2]}, persistent {ps[2]}, never ALERT")

        def day(timeline, d):
            lo = t(d)
            return [x for x in timeline if lo <= x[0] < lo + dt.timedelta(days=1)]

        # (e) quiet day
        for n in specs:
            q = day(tl[n], "2026-11-15T00:00:00Z")
            check(all(v == 0 and s == "OK" for _, v, s in q), f"(e) quiet day 2026-11-15, {short(n)}: 0 rows, OK in all {len(q)} windows")
        c = day(f, "2026-11-14T00:00:00Z")
        check(all(v == 0 for _, v, _ in c),
              "controls 2026-11-14 (404, no status, 'retry after 500ms', composition-dynamic-controller 503, "
              "HyperDX echo): 0 rows counted")
        ctl = "Timestamp >= '2026-11-14 00:00:00' AND Timestamp < '2026-11-15 00:00:00'"
        status = " AND match(JSONExtractString(Body, 'err'), 'status( code)?: 5[0-9][0-9]')"
        service = " AND JSONExtractString(Body, 'service') = 'rest-dynamic-controller'"
        assert status in where and service in where
        for dropped, clause, want_rows in (("the status clause", status, 3 * 1360), ("the service clause", service, 1360)):
            n = int(ch(port, f"SELECT count() FROM otel_logs WHERE {ctl} AND ({where.replace(clause, '')})").strip())
            check(n == want_rows, f"without {dropped} the controls count {n} rows (want {want_rows})")
        for n in specs:
            many = day(tl[n], "2026-11-13T00:00:00Z")
            fired = [b for b, _, s in many if s == "ALERT"]
            check(bool(fired) and many[-1][2] == "OK",
                  f"three resources of one kind at 2 rows a pass, {short(n)}: "
                  f"{', '.join(str(v) for _, v, _ in many if v)} rows a window, "
                  + (f"ALERT from {fired[0] + WINDOW:%H:%M}, OK after" if fired else "never fired"))
        alerting = sorted({b.date() for b, _, s in f if s == "ALERT"} | {b.date() for b, _, s in p if s == "ALERT"})
        check(alerting == [dt.date(2026, 11, 12), dt.date(2026, 11, 13)],
              f"in 057's range, ALERT only in the outage and the three-resource case: {[str(d) for d in alerting]}")

        # (c), (d), (f), (g), (h), (i): one resource
        print()
        for label, d, runs_or_rows, want in single_cases:
            s = windows(port, where, d, d + dt.timedelta(days=1))
            res, peaks = {}, max(v for _, v in s)
            for n in specs:
                timeline = states(s, specs[n])
                res[n] = [b for b, _, st in timeline if st == "ALERT"]
                ok_end = timeline[-1][2] == "OK"
                if want[n]:
                    starts = [r[0] for r in runs_or_rows] if isinstance(runs_or_rows[0], tuple) else [d]
                    # each run must fire within its own 3 hours
                    each = all(any(r <= b < r + dt.timedelta(hours=3) for b in res[n]) for r in starts)
                    check(each and ok_end, f"{label}: {short(n)} fires"
                          + (f" in each of {len(starts)} start phases" if len(starts) > 1 else "")
                          + f" (peak {peaks} rows a window)")
                else:
                    check(not res[n], f"{label}: {short(n)} never fires (peak {peaks} rows a window)")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    specs = render(root)
    if specs:
        replay(root, specs)
    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
