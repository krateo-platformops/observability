#!/usr/bin/env python3
"""sre-alert-provider-stalled: the rendered Alert, and a replay of alert-provider's logs through its where.

alert-provider no longer re-stamps status.stateObservedAt on a quiet pass, so "it stopped evaluating"
is read from the logs: provider-runtime's INFO line "External resource is up to date", one per Alert
per pass, written whether or not a status write follows.

1. Render. Renders charts/krateo-observability and checks the Alert: core tier, the where, below 5,
   a 15m window, no groupBy or metricRef, and a runbook HyperDX accepts.

2. Replay. Starts a throwaway ClickHouse of the version krateo-057 runs, creates otel_logs from 057's
   own DDL (tests/fixtures/otel_logs.sql, minus its TTL), and loads synthetic rows shaped like
   alert-provider's log lines (33 Alerts, one pass a minute: 495 rows per 15m window on 057, lowest
   of 671 windows in 7 days 462). The RENDERED where is counted per epoch-aligned 15-minute bucket
   the way HyperDX buckets a tile alert, and walked through HyperDX's state rule for `below`
   (count < threshold; an empty window counts 0).

   Cases:
     healthy     33 Alerts for a day: OK in every window
     one alert   a single Alert for a day (15 rows a window): OK
     stall       the provider stops at 10:07 for 40 minutes, then resumes: ALERT when the 10:15
                 window closes, OK when the 10:45 window closes (it holds 13 minutes of rows)
     restart     a 2-minute gap (a pod replaced in a roll): OK
     throttled   one pass in ten reaches the up-to-date line (HyperDX refusing the rest): ALERT in every window
     controls    the same sentence from rest-dynamic-controller deployments keeps arriving through
                 the stall; HyperDX echoing the query, and alert-provider's other lines, continue
                 too: none of them counts, so the stall still fires. Dropping the ServiceName
                 clause makes the controls count, so the pin is what these rows test.

Run: python3 tests/alert-provider-stalled-docker.py [repo-root]
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
CHART = "charts/krateo-observability"
NAME = "sre-alert-provider-stalled"
SENTENCE = "External resource is up to date"
WHERE = ("ResourceAttributes['telemetry.source'] = '' AND ServiceName = 'krateo-alert-provider' "
         "AND JSONExtractString(Body, 'msg') = 'External resource is up to date'")
WANT = {"where": WHERE, "threshold": 5, "thresholdType": "below", "interval": "15m"}
WINDOW = dt.timedelta(minutes=15)
NS = "krateo-system"
FAILS = 0


def check(ok, msg):
    global FAILS
    print(f"{'ok  ' if ok else 'FAIL'} {msg}")
    FAILS += not ok


def t(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def render(root):
    chart = root / CHART
    subprocess.run(["helm", "dependency", "build", str(chart)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "x", str(chart), "-n", NS],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Alert"
            and d["metadata"]["name"] == NAME]
    check(len(docs) == 1, f"{NAME} renders once with the default values ({len(docs)}): the core tier")
    if not docs:
        return None
    spec = docs[0]["spec"]
    for k, want in WANT.items():
        got = spec.get(k)
        check(got == want, f"{NAME}: spec.{k} = {got!r}" + ("" if got == want else f", want {want!r}"))
    check(not any(k in spec for k in ("groupBy", "metricRef", "aggregation", "value", "source", "consecutiveWindows")),
          f"{NAME}: a plain count of log rows: no groupBy, metricRef, aggregation, value, source or consecutiveWindows")
    rb = spec.get("runbook", "")
    check(bool(rb) and "{{" not in rb and not re.search(r"(^|\s)@[A-Za-z]", rb)
          and len(spec.get("description", "")) + 2 + len(rb) <= 4096,
          f"{NAME}: runbook is set and HyperDX-safe ({len(rb)} characters)")
    check(SENTENCE in WHERE and "ServiceName = 'krateo-alert-provider'" in spec["where"],
          f"{NAME}: the where pins the service and the sentence")
    return spec


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ch(port, sql, data=None):
    url = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode({"query": sql, "session_timezone": "UTC"})
    req = urllib.request.Request(url, data=data.encode() if data is not None else None, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def row(ts, msg, service, source="", extra=None):
    """A container log line as the daemonset's filelog writes it: the JSON line in Body, the
    Deployment as ServiceName. alert-provider's own line is the shape measured on krateo-057."""
    body = {"timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "level": "INFO", "msg": msg,
            "service.name": service, "service": service, **(extra or {})}
    res = {"k8s.deployment.name": service, "k8s.namespace.name": NS,
           "k8s.pod.name": f"{service}-5d8f7c9b6-abcde", "service.name": service,
           "telemetry.source": source}
    return {"Timestamp": ts.strftime("%Y-%m-%d %H:%M:%S.%f"), "ServiceName": service, "Body": json.dumps(body),
            "ResourceAttributes": res, "SeverityText": "", "LogAttributes": {}}


def passes(start, end, alerts, only=lambda k: True):
    """alert-provider's up-to-date lines: `alerts` Alerts a pass, one pass a minute, within about
    1.2 s of each other as measured; only(k) says whether minute k's pass reaches the line."""
    rows, m, k = [], start, 0
    while m < end:
        if only(k):
            for i in range(alerts):
                rows.append(row(m + dt.timedelta(seconds=0.04 * i), SENTENCE, "krateo-alert-provider",
                                extra={"controller": "managed/alert.observability.krateo.io",
                                       "request": {"Namespace": NS, "Name": f"alert-{i:02d}"}}))
        m += dt.timedelta(minutes=1)
        k += 1
    return rows


def windows(port, where, start, end):
    out = ch(port, f"SELECT toUnixTimestamp(toStartOfInterval(Timestamp, INTERVAL 15 MINUTE)) AS b, "
                   f"count() FROM otel_logs WHERE ({where}) GROUP BY b ORDER BY b FORMAT TSV")
    got = {int(b): int(n) for b, n in (l.split("\t") for l in out.split("\n") if l)}
    series, b = [], start
    while b < end:
        series.append((b, got.get(int(b.timestamp()), 0)))
        b += WINDOW
    return series


def states(series, threshold):
    """HyperDX's state after each window closes: below is count < threshold."""
    return [(b, v, "ALERT" if v < threshold else "OK") for b, v in series]


def at(timeline, when):
    return next(x for x in timeline if x[0] == t(when))


def day_of(timeline, d):
    lo = t(d)
    return [x for x in timeline if lo <= x[0] < lo + dt.timedelta(days=1)]


def build():
    rows = []
    d = t("2026-11-20T00:00:00Z")
    # healthy: 33 Alerts, all day
    rows += passes(d, d + dt.timedelta(days=1), 33)
    # one alert, all day
    day_one = t("2026-11-21T00:00:00Z")
    rows += passes(day_one, day_one + dt.timedelta(days=1), 1)
    # stall 10:07-10:47, with the controls arriving throughout the day
    day_stall = t("2026-11-22T00:00:00Z")
    rows += passes(day_stall, day_stall + dt.timedelta(hours=10, minutes=7), 33)
    rows += passes(day_stall + dt.timedelta(hours=10, minutes=47), day_stall + dt.timedelta(days=1), 33)
    # restart: a 2-minute gap
    day_roll = t("2026-11-23T00:00:00Z")
    rows += passes(day_roll, day_roll + dt.timedelta(hours=10), 33)
    rows += passes(day_roll + dt.timedelta(hours=10, minutes=2), day_roll + dt.timedelta(days=1), 33)
    # throttled: one pass in ten reaches the line (3 rows a window)
    day_throttled = t("2026-11-24T00:00:00Z")
    rows += passes(day_throttled, day_throttled + dt.timedelta(days=1), 1, only=lambda k: k % 10 == 0)
    # controls, on the stall day and all day on the throttled one: the same sentence from other
    # services, the query text echoed by HyperDX, alert-provider's other lines, a container-less source
    for day in (day_stall, day_throttled):
        m = day
        while m < day + dt.timedelta(days=1):
            for i in range(17):
                rows.append(row(m + dt.timedelta(seconds=i), SENTENCE, "github-provider-kog-pullrequest-controller"))
            rows.append(row(m, f"executing alert query: SELECT count() FROM otel_logs WHERE {WHERE}",
                            "krateo-clickstack-app"))
            rows.append(row(m, "[reconciler] synced Alert krateo-system/x -> hyperdx 1 (OK)", "krateo-alert-provider"))
            rows.append(row(m, SENTENCE, "krateo-alert-provider", source="k8s-events"))
            m += dt.timedelta(minutes=1)
    return rows


def replay(root, spec):
    ddl = re.sub(r"\nTTL [^\n]*", "", (root / DDL).read_text())
    check("TTL" not in ddl, "otel_logs DDL without its TTL (the replayed rows are dated 2026-11)")
    where = spec["where"]
    port = free_port()
    name = f"alert-provider-stalled-test-{port}"
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
        rows = build()
        for i in range(0, len(rows), 20000):
            ch(port, "INSERT INTO otel_logs FORMAT JSONEachRow",
               "\n".join(json.dumps(r) for r in rows[i:i + 20000]))
        total = int(ch(port, "SELECT count() FROM otel_logs").strip())
        check(total == len(rows), f"{total} synthetic rows loaded")

        thr = spec["threshold"]
        start, end = t("2026-11-20T00:00:00Z"), t("2026-11-25T00:00:00Z")
        tl = states(windows(port, where, start, end), thr)

        healthy = day_of(tl, "2026-11-20T00:00:00Z")
        check(all(s == "OK" for _, _, s in healthy[1:]) and {v for _, v, _ in healthy[1:]} == {495},
              f"healthy: 33 Alerts give 495 rows in every 15m window, OK in all {len(healthy) - 1}")
        one = day_of(tl, "2026-11-21T00:00:00Z")
        check(all(s == "OK" for _, _, s in one[1:]) and {v for _, v, _ in one[1:]} == {15},
              "one Alert: 15 rows a window, OK all day (the threshold sits below a lone Alert)")

        print("\n  stall, 2026-11-22 (state after each window closes):")
        for b, v, s in day_of(tl, "2026-11-22T00:00:00Z"):
            if t("2026-11-22T09:45:00Z") <= b <= t("2026-11-22T11:00:00Z"):
                print(f"    {b:%H:%M}-{b + WINDOW:%H:%M}  {v:4d}  {s}")
        print()
        check(at(tl, "2026-11-22T10:00:00Z")[2] == "OK",
              "stall: the 10:00 window holds 7 minutes of rows, OK")
        check(at(tl, "2026-11-22T10:15:00Z") == (t("2026-11-22T10:15:00Z"), 0, "ALERT")
              and at(tl, "2026-11-22T10:30:00Z") == (t("2026-11-22T10:30:00Z"), 0, "ALERT"),
              "stall: ALERT when the 10:15 window closes (10:30), 0 rows, and again at 10:45, though the controls kept arriving")
        check(at(tl, "2026-11-22T10:45:00Z")[2] == "OK" and at(tl, "2026-11-22T10:45:00Z")[1] == 13 * 33,
              "stall: OK when the 10:45 window closes (11:00), 13 minutes of rows back")
        day2 = day_of(tl, "2026-11-22T00:00:00Z")
        check([b for b, _, s in day2 if s == "ALERT"] == [t("2026-11-22T10:15:00Z"), t("2026-11-22T10:30:00Z")],
              "stall: ALERT in those two windows and no other that day")

        check(all(s == "OK" for _, _, s in day_of(tl, "2026-11-23T00:00:00Z")[1:]),
              "restart: a 2-minute gap in a roll stays OK in every window")

        thr_day = day_of(tl, "2026-11-24T00:00:00Z")
        check(all(s == "ALERT" for _, _, s in thr_day[1:]) and {v for _, v, _ in thr_day[1:]} <= {1, 2},
              "throttled: 1 pass in 10 reaches the line (1 or 2 rows a window), ALERT all day")

        # controls: dropping the ServiceName clause lets the other services and the echo count
        pinned = "ServiceName = 'krateo-alert-provider' AND "
        assert pinned in where
        loose = where.replace(pinned, "")
        n = int(ch(port, "SELECT count() FROM otel_logs WHERE Timestamp >= '2026-11-22 10:15:00' "
                         f"AND Timestamp < '2026-11-22 10:45:00' AND ({loose})").strip())
        check(n == 30 * 17,
              f"control: without the ServiceName clause the other services' lines count {n} rows through the stall "
              "(want 510), so the pin is what makes the stall visible")
        msg = "AND JSONExtractString(Body, 'msg') = 'External resource is up to date'"
        assert msg in where
        e = int(ch(port, "SELECT count() FROM otel_logs WHERE Timestamp >= '2026-11-22 10:15:00' "
                         f"AND Timestamp < '2026-11-22 10:45:00' AND ({where.replace(msg, '')})").strip())
        check(e == 30,
              f"control: without the msg clause alert-provider's other lines count {e} rows through the stall "
              "(want 30 'synced' lines; its up-to-date row from the events source is out by telemetry.source), "
              "so the exact sentence matters")
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
