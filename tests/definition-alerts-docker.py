#!/usr/bin/env python3
"""The three CompositionDefinition alerts: the rendered Alerts, and their where run in ClickHouse.

  sre-krateo-definition-not-ready          a Ready=False whose reason is not a not-failing one: >= 2 rows
                                           in a 15m window (a window holds 3 pulls of one definition)
  sre-krateo-definition-stuck-converging   generation != observedGeneration on the same definition in
                                           at least 6 of the last 35 minutes' pulls, and equal in none

otel-collector-deployment reads every CompositionDefinition every 5 minutes (k8sobjects/definitions)
and writes one otel_logs row each, telemetry.source = 'k8s-objects', Body the object trimmed to
metadata.name, namespace, generation and status.observedGeneration, conditions.

1. Render. charts/krateo-observability's Alerts: where, threshold, thresholdType, interval, no groupBy
   or metricRef (alert-provider refuses groupBy on a log alert), a runbook HyperDX takes.

2. Run. A throwaway ClickHouse of the version krateo-057 runs, otel_logs from 057's own DDL
   (tests/fixtures/otel_logs.sql), synthetic rows shaped like the receiver's, dated from now() because
   the stuck alert reads now(). Each case is its own table state. An alert FIRES when the rendered
   where counts at least `threshold` rows in the last 15 minutes: `above` is count >= threshold
   (HyperDX checkAlerts doesExceedThreshold), the window is the alert's interval.

   not Ready
     (a) a Ready=False ValuesSchemaMissing row fires; so does each of the six reasons failed at
         core-provider 2.13.136; one pull alone (1 row) does not, three pulls do
     (b) a Ready=False reason no table lists fires
     (c) Creating, Deleting (Ready=False), NotObserved (Unknown), Available, ScaledToZero and
         UpdateWithheldByPolicy (True) do not
     (d) a Ready=True row does not, with other conditions False beside it
     controls  the same failed Ready under telemetry.source 'k8s-events' and '', and another resource,
               do not fire
   stuck
     (e) 30 minutes of generation != observedGeneration fires, 10 minutes does not; 30 minutes with a
         missed pull fires; equal at the latest pull does not; a rolling set of definitions each
         unequal for one pull does not; no observedGeneration does not; one definition's stuck rows
         do not carry another's
   converging too long (sre-krateo-definition-converging-too-long; bounds from
         defaultAlerts.definitionConvergenceBounds: Creating 30m, Deleting 120m, NotObserved 15m)
     (f) Creating since 31 minutes fires, since 29 and 10 do not; Deleting since 121 fires, since 119
         and 10 do not; NotObserved (Unknown) since 16 fires, since 5 does not
         a wrong status for the reason, a healthy reason, a failed reason and another source do not
         never both: Creating for 40 minutes with generation = observedGeneration is this alert's and
         not the stuck alert's; with generation lagging for 30 minutes it is the stuck alert's and not
         this one's
         one place: raising Creating's bound in the values changes that term of the where and no other
   leak  every other Alert's where (both tiers), over the rows of 79 definitions (some failed), counts 0
         (but the ones that count every row, `where: ''`)
   runbooks  both runbooks' first query runs and names the definition

Run: python3 tests/definition-alerts-docker.py [repo-root]
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
NOT_READY = "sre-krateo-definition-not-ready"
STUCK = "sre-krateo-definition-stuck-converging"
TOO_LONG = "sre-krateo-definition-converging-too-long"
NS = "krateo-system"
WINDOW = "INTERVAL 15 MINUTE"
# 2026-11-28: the render with the extended tier has 30 log Alerts other than these three;
# fewer means the render lost its alerts and the leak case examined nothing.
MIN_LEAK_ALERTS = 24
FAILED_AT_2_13_136 = ("Unavailable", "RemoteChartUnreachable", "SchemaDefaultContradiction",
                      "ValuesSchemaMissing", "DerivedNameTooLong", "CreateWithheldByPolicy")
FAILS = 0


def check(ok, msg):
    global FAILS
    print(f"{'ok  ' if ok else 'FAIL'} {msg}")
    FAILS += not ok


def render(root, *extra):
    chart = root / CHART
    subprocess.run(["helm", "dependency", "build", str(chart)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "x", str(chart), "-n", NS, *extra],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d and d.get("kind") == "Alert"]
    return {d["metadata"]["name"]: d["spec"] for d in docs}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ch(port, sql, data=None):
    url = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode({"query": sql, "session_timezone": "UTC"})
    req = urllib.request.Request(url, data=data.encode() if data is not None else None, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:600])


def cond(ctype, status, reason, message=""):
    c = {"type": ctype, "status": status, "reason": reason, "lastTransitionTime": "2026-11-28T10:00:00Z"}
    if message:
        c["message"] = message
    return c


def row(ts, name, conditions, generation=1, observed=1, source="k8s-objects", resource="compositiondefinitions"):
    """What the receiver and its transform write for one CompositionDefinition at one pull."""
    status = {"conditions": conditions}
    if observed is not None:
        status["observedGeneration"] = observed
    res = {"k8s.namespace.name": NS}
    if source is not None:
        res["telemetry.source"] = source
    return {"Timestamp": ts.strftime("%Y-%m-%d %H:%M:%S.%f"), "ServiceName": "", "SeverityText": "",
            "Body": json.dumps({"metadata": {"name": name, "namespace": NS, "generation": generation}, "status": status}),
            "ResourceAttributes": res, "LogAttributes": {"k8s.resource.name": resource}}


def ready(status, reason):
    return [cond("Ready", status, reason), cond("Synced", "True", "ReconcileSuccess")]


def ready_since(status, reason, now, minutes):
    """Ready in `reason` since `minutes` before now, by the condition's own lastTransitionTime."""
    c = cond("Ready", status, reason)
    c["lastTransitionTime"] = (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [c, cond("Synced", "True", "ReconcileSuccess")]


def pulls(now, minutes, every=5, skip=()):
    """Pull instants over the last `minutes`, 5 minutes apart, the latest 2.5 minutes ago."""
    out, k = [], 0
    while k * every <= minutes:
        if k not in skip:
            out.append(now - dt.timedelta(minutes=2.5 + every * k))
        k += 1
    return out


class DB:
    def __init__(self, root):
        self.ddl = (root / DDL).read_text()
        self.port = free_port()
        self.name = f"definition-alerts-test-{self.port}"

    def __enter__(self):
        subprocess.run(["docker", "run", "-d", "--name", self.name, "-p", f"127.0.0.1:{self.port}:8123",
                        "-e", "CLICKHOUSE_SKIP_USER_SETUP=1", IMAGE], check=True, capture_output=True)
        for _ in range(90):
            try:
                if ch(self.port, "SELECT 1").strip() == "1":
                    break
            except Exception:
                time.sleep(1)
        else:
            raise SystemExit("ClickHouse never answered")
        ch(self.port, self.ddl)
        return self

    def __exit__(self, *a):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def load(self, rows):
        ch(self.port, "TRUNCATE TABLE otel_logs")
        for i in range(0, len(rows), 20000):
            ch(self.port, "INSERT INTO otel_logs FORMAT JSONEachRow", "\n".join(json.dumps(r) for r in rows[i:i + 20000]))

    def count(self, where, windowed=True):
        extra = f" AND Timestamp >= now() - {WINDOW}" if windowed else ""
        return int(ch(self.port, f"SELECT count() FROM otel_logs WHERE ({where}){extra}").strip())


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    alerts = render(root)
    everything = render(root, "--set", "defaultAlerts.extended=true")   # the leak case reads both tiers
    specs = {n: alerts.get(n) for n in (NOT_READY, STUCK, TOO_LONG)}
    for n, want in ((NOT_READY, {"threshold": 2, "thresholdType": "above", "interval": "15m"}),
                    (STUCK, {"threshold": 1, "thresholdType": "above", "interval": "15m"}),
                    (TOO_LONG, {"threshold": 1, "thresholdType": "above", "interval": "15m"})):
        spec = specs[n]
        check(spec is not None, f"{n} renders with the default values")
        if spec is None:
            continue
        for k, v in want.items():
            check(spec.get(k) == v, f"{n}: spec.{k} = {spec.get(k)!r}, want {v!r}")
        check(not any(k in spec for k in ("groupBy", "metricRef", "aggregation", "source")),
              f"{n}: a log count (no groupBy, metricRef, aggregation or source)")
        rb = spec.get("runbook", "")
        check(bool(rb) and "{{" not in rb and not re.search(r"(^|\s)@[A-Za-z]", rb)
              and len(spec.get("description", "")) + 2 + len(rb) <= 4096, f"{n}: runbook is set and HyperDX-safe ({len(rb)} characters)")
        check("telemetry.source'] = 'k8s-objects'" in spec["where"] and "k8s-events" not in spec["where"],
              f"{n}: selects its rows by telemetry.source = 'k8s-objects'")
    if not all(specs.values()):
        print(f"\n{FAILS} failure(s)")
        sys.exit(1)
    nr, st, tl = specs[NOT_READY]["where"], specs[STUCK]["where"], specs[TOO_LONG]["where"]
    # One place: the bound is a value, and the where follows it, one term only.
    moved = render(root, "--set", "defaultAlerts.definitionConvergenceBounds.Creating.minutes=45")[TOO_LONG]["where"]
    check(tl.count("INTERVAL 30 MINUTE") == 1 and moved == tl.replace("INTERVAL 30 MINUTE", "INTERVAL 45 MINUTE"),
          "(f) the Creating bound is a value: 30 to 45 changes that term of the where and no other")
    check(all(f"= '{r}' AND" in tl for r in ("Creating", "Deleting", "NotObserved")) and "INTERVAL 120 MINUTE" in tl
          and "INTERVAL 15 MINUTE" in tl, "(f) the where has a term for Creating, Deleting and NotObserved at 30, 120 and 15 minutes")
    thr = specs[NOT_READY]["threshold"]
    with DB(root) as db:
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        three = pulls(now, 10)            # 3 pulls in the last 12.5 minutes
        check(len(three) == 3, f"3 pulls in 10 minutes ({len(three)})")

        def fires_nr(rows, label, want, name=NOT_READY):
            db.load(rows)
            n = db.count(nr)
            check((n >= thr) == want, f"{label}: {n} rows, {'fires' if n >= thr else 'does not fire'}"
                  + ("" if (n >= thr) == want else f", want {'fires' if want else 'silent'}"))

        # (a) the six failed reasons, one definition, three pulls
        for r in FAILED_AT_2_13_136:
            fires_nr([row(t, "alpha", ready("False", r)) for t in three], f"(a) Ready=False {r}, 3 pulls", True)
        fires_nr([row(three[0], "alpha", ready("False", "ValuesSchemaMissing"))], "(a) ValuesSchemaMissing, 1 pull only", False)
        fires_nr([row(t, "alpha", ready("False", "ValuesSchemaMissing")) for t in three[:2]], "(a) ValuesSchemaMissing, 2 pulls", True)
        fires_nr([row(three[0], "alpha", ready("False", "Unavailable")), row(three[0], "beta", ready("False", "Unavailable"))],
                 "(a) two definitions failed at one pull", True)
        # (b) an unrecognised reason
        fires_nr([row(t, "alpha", ready("False", "SomeReasonAddedIn2_14")) for t in three], "(b) Ready=False, a reason no table lists", True)
        # (c) not failing
        for status, r in (("False", "Creating"), ("False", "Deleting"), ("Unknown", "NotObserved"),
                          ("True", "Available"), ("True", "ScaledToZero"), ("True", "UpdateWithheldByPolicy")):
            fires_nr([row(t, "alpha", ready(status, r)) for t in three], f"(c) Ready={status} {r}, 3 pulls", False)
        # (d) Ready=True with the other conditions failed
        beside = [cond("Ready", "True", "Available"), cond("Synced", "False", "ReconcileError"),
                  cond("SchemaFullyEnforced", "False", "ConstraintsNotEnforced"), cond("ControllerActive", "False", "Unavailable")]
        fires_nr([row(t, "alpha", beside) for t in three], "(d) Ready=True beside Synced, SchemaFullyEnforced and ControllerActive False", False)
        # controls
        failed = ready("False", "ValuesSchemaMissing")
        fires_nr([row(t, "alpha", failed, source="k8s-events") for t in three], "control: the failed Ready under telemetry.source 'k8s-events'", False)
        fires_nr([row(t, "alpha", failed, source="") for t in three], "control: the failed Ready under telemetry.source ''", False)
        fires_nr([row(t, "alpha", failed, source=None) for t in three], "control: the failed Ready with no telemetry.source", False)
        fires_nr([row(t, "alpha", failed, resource="events") for t in three], "control: another k8s.resource.name", False)
        fires_nr([row(now - dt.timedelta(minutes=40 + 5 * k), "alpha", failed) for k in range(3)],
                 "a failed Ready more than 15 minutes ago is outside the window", False)

        # (e) stuck
        def stuck_case(rows, label, want):
            db.load(rows)
            n = db.count(st)
            check((n >= 1) == want, f"{label}: {n} rows, {'fires' if n else 'does not fire'}"
                  + ("" if (n >= 1) == want else f", want {'fires' if want else 'silent'}"))

        ok = ready("True", "Available")
        unequal = lambda ts, name="alpha": row(ts, name, ok, generation=3, observed=2)
        stuck_case([unequal(t) for t in pulls(now, 30)], "(e) generation 3 != observed 2 for 30 minutes (7 pulls)", True)
        stuck_case([unequal(t) for t in pulls(now, 10)], "(e) the same for 10 minutes (3 pulls)", False)
        stuck_case([unequal(t) for t in pulls(now, 25)], "(e) the same for 25 minutes (6 pulls)", True)
        stuck_case([unequal(t) for t in pulls(now, 20)], "(e) the same for 20 minutes (5 pulls)", False)
        stuck_case([unequal(t) for t in pulls(now, 30, skip=(3,))], "(e) 30 minutes with one pull missed", True)
        stuck_case([unequal(t) for t in pulls(now, 30, skip=(2, 3))], "(e) 30 minutes with two pulls missed", False)
        ts = pulls(now, 30)
        stuck_case([unequal(t) for t in ts[1:]] + [row(ts[0], "alpha", ok, generation=3, observed=3)],
                   "(e) 30 minutes unequal, equal at the latest pull (recovered)", False)
        stuck_case([row(t, "alpha", ok, generation=3, observed=3 if t == ts[4] else 2) for t in ts],
                   "(e) 30 minutes unequal with one equal pull in the middle", False)
        rolling = [unequal(t, f"roll-{i}") for i, t in enumerate(pulls(now, 30)) if True]
        stuck_case(rolling, "(e) seven definitions each unequal for one pull, one after another", False)
        stuck_case([row(t, "alpha", ok, generation=3, observed=None) for t in pulls(now, 30)], "(e) no status.observedGeneration", False)
        stuck_case([row(t, "alpha", ok, generation=1, observed=1) for t in pulls(now, 30)], "(e) generation = observedGeneration", False)
        stuck_case([unequal(t, "alpha") for t in pulls(now, 10)] + [row(t, "beta", ok) for t in pulls(now, 30)],
                   "(e) one definition unequal for 10 minutes beside one equal for 30", False)
        mixed = [unequal(t, "alpha") for t in pulls(now, 30)] + [unequal(t, "beta") for t in pulls(now, 10)]
        db.load(mixed)
        names = ch(db.port, f"SELECT DISTINCT JSONExtractString(Body, 'metadata', 'name') FROM otel_logs WHERE ({st}) AND Timestamp >= now() - {WINDOW}").split()
        check(names == ["alpha"], f"(e) alpha unequal for 30 minutes and beta for 10: only {names} counted")
        stuck_case([unequal(t, "alpha") for t in pulls(now, 30)] , "(e) a Ready=True definition stuck behind its generation", True)
        stuck_case([row(t, "alpha", ready("False", "Creating"), generation=2, observed=1) for t in pulls(now, 30)], "(e) stuck while Creating", True)
        stuck_case([row(t, "alpha", ready("False", "Creating"), generation=2, observed=1, source="k8s-events") for t in pulls(now, 30)], "control: stuck rows under 'k8s-events'", False)

        # (f) converging too long
        def tl_case(rows, label, want, window_where=tl):
            db.load(rows)
            n = db.count(window_where)
            check((n >= 1) == want, f"{label}: {n} rows, {'fires' if n else 'does not fire'}"
                  + ("" if (n >= 1) == want else f", want {'fires' if want else 'silent'}"))

        def held(status, reason, minutes, **kw):
            return [row(t, "alpha", ready_since(status, reason, now, minutes), **kw) for t in pulls(now, 10)]

        for status, reason, over, under in (("False", "Creating", 31, 10), ("False", "Deleting", 121, 10),
                                            ("Unknown", "NotObserved", 16, 5)):
            tl_case(held(status, reason, over), f"(f) Ready={status} {reason} since {over} minutes", True)
            tl_case(held(status, reason, under), f"(f) Ready={status} {reason} since {under} minutes", False)
        tl_case(held("False", "Creating", 29), "(f) Creating since 29 minutes (bound 30)", False)
        tl_case(held("False", "Deleting", 119), "(f) Deleting since 119 minutes (bound 120)", False)
        tl_case(held("Unknown", "NotObserved", 14), "(f) NotObserved since 14 minutes (bound 15)", False)
        tl_case(held("True", "Creating", 300), "(f) Creating with status True, long", False)
        tl_case(held("Unknown", "Creating", 300), "(f) Creating with status Unknown, long", False)
        tl_case(held("False", "NotObserved", 300), "(f) NotObserved with status False, long", False)
        tl_case(held("True", "Available", 5000), "(f) Ready=True Available since 5000 minutes", False)
        tl_case(held("False", "ValuesSchemaMissing", 5000), "(f) a failed reason since 5000 minutes (the not-Ready alert's)", False)
        tl_case(held("False", "Creating", 300, source="k8s-events"), "(f) control: Creating since 300 minutes under 'k8s-events'", False)
        tl_case([row(t, "alpha", [dict(cond("Ready", "False", "Creating"), lastTransitionTime="not a time")]) for t in pulls(now, 10)],
                "(f) an unparseable lastTransitionTime", False)
        # Never both: the same definition, Creating 40 minutes.
        agree = [row(t, "alpha", ready_since("False", "Creating", now, 40), generation=2, observed=2) for t in pulls(now, 30)]
        db.load(agree)
        a, b = db.count(tl), db.count(st)
        check(a >= 1 and b == 0, f"(f) Creating 40 minutes, generation = observedGeneration: converging-too-long {a} rows, stuck {b}")
        lag = [row(t, "alpha", ready_since("False", "Creating", now, 40), generation=3, observed=2) for t in pulls(now, 30)]
        db.load(lag)
        a, b = db.count(tl), db.count(st)
        check(a == 0 and b >= 1, f"(f) Creating 40 minutes, generation lagging for 30: converging-too-long {a} rows, stuck {b}")
        # Neither pair fires on one definition's rows twice over: every case above, and the other
        # alerts, count a definition's row in one alert only.
        db.load(agree)
        check(db.count(nr) == 0, "(f) the not-Ready alert does not count a Creating definition either")

        # leak: the rows of 79 definitions, some failed and some stuck, against every other log Alert
        rows = []
        for t in pulls(now, 30):
            for i in range(79):
                kind = ready("False", "ValuesSchemaMissing") if i < 3 else ready_since("False", "Creating", now, 180) if i == 7 else ok
                rows.append(row(t, f"def-{i:02d}", kind, generation=2 if i == 5 else 1, observed=1))
        db.load(rows)
        check(db.count(nr) > 0 and db.count(st) > 0 and db.count(tl) > 0, f"79 definitions x 7 pulls: all three alerts count rows ({db.count(nr)}, {db.count(st)}, {db.count(tl)})")
        others = {n: s for n, s in everything.items() if n not in specs and s.get("where") and "metricRef" not in s and not s.get("source")}
        check(len(others) >= MIN_LEAK_ALERTS, f"{len(others)} other log Alerts examined, at least {MIN_LEAK_ALERTS}")
        leaks = {}
        for n, s in sorted(others.items()):
            try:
                c = db.count(s["where"], windowed=False)
            except Exception as e:
                c = f"error {e}"
            if c != 0:
                leaks[n] = c
        check(not leaks, f"no other log Alert counts a definition row ({len(rows)} rows): {leaks or 'all 0'}")
        # The control: the same rows tagged as events are counted by the events alerts, so the leak
        # case above would have shown a leak had the rows carried 'k8s-events'.
        db.load([dict(r, ResourceAttributes={**r["ResourceAttributes"], "telemetry.source": "k8s-events"}) for r in rows])
        leaked = sorted(n for n, s in others.items() if db.count(s["where"], windowed=False) > 0)
        check("sre-kubernetes-events-missing" in others and "sre-kubernetes-events-missing" in leaked,
              f"control: the same rows tagged 'k8s-events' are counted by {leaked}")
        db.load(rows)

        # runbooks
        db.load([row(t, "alpha", ready("False", "ValuesSchemaMissing")) for t in pulls(now, 30)])
        q = re.search(r"SELECT .*? GROUP BY namespace, definition, reason", specs[NOT_READY]["runbook"]).group(0)
        out = ch(db.port, q + " FORMAT TSV").split()
        check(out[:2] == [NS, "alpha"] and "ValuesSchemaMissing" in out, f"not-Ready runbook query names the definition and reason: {out}")
        db.load([unequal(t, "alpha") for t in pulls(now, 30)])
        q = re.search(r"SELECT .*? HAVING generation != observed", specs[STUCK]["runbook"]).group(0)
        out = ch(db.port, q + " FORMAT TSV").split()
        check(out[:2] == [NS, "alpha"] and out[2:4] == ["3", "2"], f"stuck runbook query names the definition, generation and observed: {out}")
    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
