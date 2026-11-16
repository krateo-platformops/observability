#!/usr/bin/env python3
"""sre-rest-controller-vendor-5xx: the rendered Alert, and a replay of krateo-057's vendor 5xx outage.

1. Render. Renders charts/krateo-observability and checks the Alert's spec: where, threshold,
   thresholdType, interval, consecutiveWindows, and no groupBy or metricRef (alert-provider refuses
   groupBy without metricRef, so this alert counts every kind together).

2. Replay. Starts a throwaway ClickHouse of the version krateo-057 runs, creates otel_logs from 057's
   own DDL (tests/fixtures/otel_logs.sql, from SHOW CREATE TABLE, minus its 7-day TTL: the replayed
   rows are dated 2026-11), and loads synthetic rows shaped like rest-dynamic-controller's log lines.
   The failures reproduce, minute by minute, the counts saved from 057 in
   tests/fixtures/vendor-5xx-krateo-057.json: kind, status, rows and distinct resources, nothing
   else. No real err text is used: err is rest-dynamic-controller's own format
   (internal/tools/client/validators.go, "unexpected status: %d: invalid status code: %d") with the
   status filled in, and resource names are made up.

   The RENDERED where is then counted per epoch-aligned 15-minute bucket, the way HyperDX buckets a
   tile alert, and walked through HyperDX's state rules: `above` is count >= threshold, an empty
   window counts 0, the alert fires after consecutiveWindows breaching windows in a row (PENDING
   before that, which notifies nothing) and resolves on the first window that does not breach.

   Cases:
     outage      2026-11-12, 17 PullRequests, 503: the per-window counts equal the ones measured on
                 057; PENDING after the 13:37 burst alone, ALERT from the end of the 14:45 window,
                 OK at the end of the 15:30 window (the last failure was 15:25:59)
     blips       500 x4 (11-10 16:01), 502 x2 (11-10 21:25), 503 x1 (11-11 15:10), 504 x1
                 (11-16 06:38): never PENDING, never ALERT
     quiet day   2026-11-15: only healthy lines, OK all day
     controls    2026-11-14, each sustained for two hours at outage rates, none counted: 404s, an err
                 with no status, "retry after 500ms", a composition-dynamic-controller 503, the
                 "Performing REST call" twin of every failure, and HyperDX echoing the query text
     one vs many 2026-11-13: one resource failing every resync stays below the threshold; three
                 resources of one kind failing every resync fire, and resolve when they stop

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
NAME = "sre-rest-controller-vendor-5xx"

WHERE = ("ResourceAttributes['telemetry.source'] = '' AND JSONExtractString(Body, 'service') = "
         "'rest-dynamic-controller' AND JSONExtractString(Body, 'msg') = 'Cannot observe external "
         "resource' AND match(JSONExtractString(Body, 'err'), 'status( code)?: 5[0-9][0-9]')")
SPEC = {"where": WHERE, "threshold": 20, "thresholdType": "above", "interval": "15m",
        "consecutiveWindows": 2}

UTC = dt.timezone.utc
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
    alerts = [d for d in yaml.safe_load_all(out)
              if d and d.get("kind") == "Alert" and d["metadata"]["name"] == NAME]
    check(len(alerts) == 1, f"{NAME} renders once with the default values ({len(alerts)})")
    if not alerts:
        return None
    spec = alerts[0]["spec"]
    for k, want in SPEC.items():
        check(spec.get(k) == want, f"spec.{k} = {spec.get(k)!r}" + ("" if spec.get(k) == want else f", want {want!r}"))
    for k in ("groupBy", "metricRef", "aggregation", "value", "source"):
        check(k not in spec, f"spec.{k} is not set (a log count over every kind)")
    check("4[0-9]" not in spec.get("where", ""), "where does not match 4xx")
    rb = spec.get("runbook", "")
    check(bool(rb) and "{{" not in rb and not re.search(r"(^|\s)@[A-Za-z]", rb)
          and len(spec.get("description", "")) + 2 + len(rb) <= 4096,
          f"runbook is set and HyperDX-safe ({len(rb)} characters)")
    check("'kind') AS kind" in rb and ") AS status" in rb and "'err') AS" not in rb
          and "vendorResponse'" not in rb and "Do not select err" in rb,
          "runbook's query names kind and status and never returns err itself")
    return spec


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
    """A failed resync of `resources` resources every `every_min` minutes, two rows each, as
    rest-dynamic-controller logs a failed resync on 057."""
    rows, m = [], start
    while m < end:
        for j, ts in enumerate(spread(m, 2 * resources)):
            rows.append(make(ts, j % resources))
        m += dt.timedelta(minutes=every_min)
    return rows


def build(fixture):
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
    # One resource vs. three resources of one kind, 2026-11-13.
    rows += burst(t("2026-11-13T09:00:00Z"), t("2026-11-13T11:00:00Z"), 3, 1,
                  lambda ts, i: observe_fail(ts, "Repository", 7, status_err(503),
                                             deployment="github-provider-kog-repository-controller"))
    rows += burst(t("2026-11-13T13:00:00Z"), t("2026-11-13T14:00:00Z"), 3, 3,
                  lambda ts, i: observe_fail(ts, "Branch", i, status_err(502),
                                             deployment="github-provider-kog-branch-controller"))
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


def replay(root, spec):
    fixture = json.loads((root / COUNTS).read_text())
    ddl = (root / DDL).read_text()
    ddl = re.sub(r"\nTTL [^\n]*", "", ddl)
    check("TTL" not in ddl, "otel_logs DDL without its TTL (the replayed rows are dated 2026-11)")
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
        rows = build(fixture)
        for i in range(0, len(rows), 20000):
            ch(port, "INSERT INTO otel_logs FORMAT JSONEachRow",
               "\n".join(json.dumps(r) for r in rows[i:i + 20000]))
        total = int(ch(port, "SELECT count() FROM otel_logs").strip())
        check(total == len(rows), f"{total} synthetic rows loaded")

        start, end = t(fixture["from"]), t(fixture["to"])
        series = windows(port, spec["where"], start, end)
        timeline = states(series, spec)
        synthetic = (t("2026-11-13T00:00:00Z"), t("2026-11-15T00:00:00Z"))
        replayed = {b: v for b, v in series if not synthetic[0] <= b < synthetic[1] and v}
        want = {t(k): v for k, v in fixture["windows15m"].items()}
        check(replayed == want, "per-window counts of the rendered where equal the ones measured on 057 "
              "(each failure loaded with its 'Performing REST call' twin): "
              + ", ".join(f"{b:%m-%d %H:%M} {v}" for b, v in sorted(replayed.items())))

        print("\n  the outage, 2026-11-12 (state after each window closes):")
        for b, v, s in timeline:
            if t("2026-11-12T13:15:00Z") <= b <= t("2026-11-12T16:00:00Z"):
                print(f"    {b:%H:%M}-{b + WINDOW:%H:%M}  {v:4d}  {s}")
        print()
        check(at(timeline, "2026-11-12T13:30:00Z")[2] == "PENDING",
              "13:37 burst alone (one failed resync of all 17): PENDING, no notification")
        check(at(timeline, "2026-11-12T13:45:00Z")[2] == "OK", "13:45 window empty: back to OK")
        check(at(timeline, "2026-11-12T14:30:00Z")[2] == "PENDING", "14:30 window: PENDING")
        check(at(timeline, "2026-11-12T14:45:00Z")[2] == "ALERT", "fires when the 14:45 window closes (15:00)")
        check(all(at(timeline, w)[2] == "ALERT" for w in ("2026-11-12T15:00:00Z", "2026-11-12T15:15:00Z")),
              "stays ALERT through the 15:15 window")
        last = t(fixture["lastFailure"])
        cleared = next(b + WINDOW for b, v, s in timeline if b > last - WINDOW and s == "OK")
        check(at(timeline, "2026-11-12T15:30:00Z")[2] == "OK" and cleared - last <= 2 * WINDOW,
              f"clears when the 15:30 window closes ({cleared:%H:%M}, {int((cleared - last).total_seconds() // 60)} "
              f"minutes after the last failure at {last:%H:%M:%S})")

        for w, what in (("2026-11-10T16:00:00Z", "500 x4 Repository"), ("2026-11-10T21:15:00Z", "502 x2 PullRequest"),
                        ("2026-11-11T15:00:00Z", "503 x1 PullRequest"), ("2026-11-16T06:30:00Z", "504 x1 Repository")):
            b, v, s = at(timeline, w)
            check(s == "OK" and v < spec["threshold"], f"blip {what} at {b:%m-%d %H:%M}: {v} rows, {s}")

        def day(d):
            lo = t(d)
            return [x for x in timeline if lo <= x[0] < lo + dt.timedelta(days=1)]

        q = day("2026-11-15T00:00:00Z")
        check(all(v == 0 and s == "OK" for _, v, s in q), f"quiet day 2026-11-15: 0 rows, OK in all {len(q)} windows")
        c = day("2026-11-14T00:00:00Z")
        check(all(v == 0 and s == "OK" for _, v, s in c),
              "controls 2026-11-14 (404, no status, 'retry after 500ms', composition-dynamic-controller 503, "
              "HyperDX echo): 0 rows counted")
        # The controls are live: each is excluded by the clause that should exclude it, not by luck.
        ctl = "Timestamp >= '2026-11-14 00:00:00' AND Timestamp < '2026-11-15 00:00:00'"
        status = " AND match(JSONExtractString(Body, 'err'), 'status( code)?: 5[0-9][0-9]')"
        service = " AND JSONExtractString(Body, 'service') = 'rest-dynamic-controller'"
        assert status in spec["where"] and service in spec["where"]
        for dropped, clause, want_rows in (("the status clause", status, 3 * 1360), ("the service clause", service, 1360)):
            n = int(ch(port, f"SELECT count() FROM otel_logs WHERE {ctl} AND ({spec['where'].replace(clause, '')})").strip())
            check(n == want_rows, f"without {dropped} the controls count {n} rows (want {want_rows})")
        one = [x for x in day("2026-11-13T00:00:00Z") if x[0] < t("2026-11-13T12:00:00Z")]
        peak = max(v for _, v, _ in one)
        check(0 < peak < spec["threshold"] and all(s != "ALERT" for _, _, s in one),
              f"one resource failing every resync: at most {peak} rows a window, never ALERT")
        many = [x for x in day("2026-11-13T00:00:00Z") if x[0] >= t("2026-11-13T12:00:00Z")]
        fired = [b for b, _, s in many if s == "ALERT"]
        check(bool(fired) and many[-1][2] == "OK",
              f"three resources of one kind failing every resync: {', '.join(str(v) for _, v, _ in many if v)} "
              f"rows a window, ALERT from {fired[0] + WINDOW:%H:%M}, OK after" if fired else
              "three resources of one kind failing every resync: never fired")

        alerting = sorted({b.date() for b, _, s in timeline if s == "ALERT"})
        check(alerting == [dt.date(2026, 11, 12), dt.date(2026, 11, 13)],
              f"ALERT only in the outage and the three-resource case: {[str(d) for d in alerting]}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    spec = render(root)
    if spec:
        replay(root, spec)
    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
