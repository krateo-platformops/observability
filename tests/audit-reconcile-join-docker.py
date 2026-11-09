#!/usr/bin/env python3
"""Functional test of the OTEL-8 audit-to-reconcile join (ops/audit-reconcile-join/query.sql).

Runs the query in a throwaway ClickHouse of the version krateo-057 runs, against krateo_audit and
otel_traces created from krateo-057's own DDL (tests/fixtures/*.sql, from SHOW CREATE TABLE). It
loads audit writes shaped like snowplow's records and reconcile spans shaped like
unstructured-runtime's StartReconcileSpan, then checks the query returns exactly the expected rows:
the matches, every negative control left out, and the ambiguous case flagged rather than resolved.

Run: python3 tests/audit-reconcile-join-docker.py [repo-root]
Exit 0 = every expectation held, 1 = at least one did not.
"""
import datetime as dt
import json
import pathlib
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

IMAGE = "clickhouse/clickhouse-server:26.8.36.3"  # SELECT version() on krateo-057
QUERY = "ops/audit-reconcile-join/query.sql"
WINDOW_S = 600
T0 = dt.datetime(2026, 11, 9, 10, 0, 0)


def ts(t, seconds=0):
    return (t + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S.%f")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ch(port, sql, params=None, data=None):
    q = {"query": sql}
    for k, v in (params or {}).items():
        q[f"param_{k}"] = str(v)
    url = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, data=data.encode() if data else None, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def audit(t, seconds, actor, method, resource, version, name, namespace,
          group="composition.krateo.io", action="call", outcome="success"):
    """A krateo_audit row as snowplow writes it: the object reference lives in LogAttributes."""
    attrs = {
        "event.name": "audit", "krateo.action": action, "outcome": outcome,
        "user.name": actor, "session.id": f"sess-{actor}",
        "http.request.method": method, "http.response.status_code": "200",
        "k8s.resource.group": group, "k8s.resource.version": version,
        "k8s.resource.resource": resource, "k8s.resource.name": name,
        "k8s.namespace.name": namespace,
    }
    return {"Timestamp": ts(t, seconds), "TraceId": f"{abs(hash((actor, seconds, name))):032x}"[:32],
            "SpanId": "", "ServiceName": "snowplow", "SeverityText": "INFO", "Body": "audit",
            "LogAttributes": attrs, "ResourceAttributes": {"service.name": "snowplow"}}


def span(t, seconds, controller, kind, name, namespace, status="Unset", span_name="reconcile",
         span_kind="Server", sid=None):
    """A reconcile span as unstructured-runtime emits it from a composition controller."""
    return {"Timestamp": ts(t, seconds), "TraceId": f"{sid:032x}", "SpanId": f"{sid:016x}",
            "ParentSpanId": "", "SpanName": span_name, "SpanKind": span_kind,
            "ServiceName": "composition-dynamic-controller",
            "ResourceAttributes": {"service.name": "composition-dynamic-controller",
                                   "k8s.deployment.name": controller},
            "SpanAttributes": {"k8s.object.kind": kind, "k8s.object.name": name,
                               "k8s.object.namespace": namespace},
            "Duration": 5_000_000, "StatusCode": status, "StatusMessage": ""}


AF = ("acmefeedbacks", "v0-1-0")
AF_CTRL = "acmefeedbacks-v0-1-0-controller"
NS = "krateo-system"
T1 = T0 + dt.timedelta(hours=1)
T2 = T0 + dt.timedelta(hours=2)
T3 = T0 + dt.timedelta(hours=3)

WRITES = [
    audit(T0, 0, "alice", "PATCH", *AF, "fb1", NS),                     # W1: two reconciles, one failed
    audit(T1, 0, "alice", "PATCH", *AF, "fb3", NS),                     # W2: ambiguous with W3
    audit(T1, 60, "bob", "PATCH", *AF, "fb3", NS),                      # W3
    audit(T2, 0, "carol", "PATCH", "clusterthings", "v1-0-0", "ct1", ""),  # W4: cluster-scoped
    audit(T3, 0, "dave", "DELETE", *AF, "fb9", NS),                     # W5: nothing followed
    # Excluded writes: never a row.
    audit(T3, 10, "erin", "POST", *AF, "fb9", NS, action="call.dryRun"),
    audit(T3, 20, "erin", "PATCH", *AF, "fb9", NS, outcome="failure"),
    audit(T3, 30, "erin", "POST", "auditrecords", "v1alpha1", "-", NS, group="audit.krateo.io"),
]

SPANS = [
    span(T0, 2, AF_CTRL, "AcmeFeedback", "fb1", NS, sid=1),                 # W1 match, first
    span(T0, 30, AF_CTRL, "AcmeFeedback", "fb1", NS, status="Error", sid=2),  # W1 match, error
    span(T0, 700, AF_CTRL, "AcmeFeedback", "fb1", NS, sid=3),               # outside the window
    span(T0, -5, AF_CTRL, "AcmeFeedback", "fb1", NS, sid=4),                # before the write
    span(T0, 5, AF_CTRL, "AcmeFeedback", "fb2", NS, sid=5),                 # other name
    span(T0, 5, AF_CTRL, "AcmeFeedback", "fb1", "other-ns", sid=6),         # other namespace
    span(T0, 5, "agentikoes-v0-8-0-controller", "Agentiko", "fb1", NS, sid=7),  # other resource
    span(T0, 6, AF_CTRL, "AcmeFeedback", "fb1", NS, span_kind="Internal", sid=8),  # not the root
    span(T0, 7, AF_CTRL, "AcmeFeedback", "fb1", NS, span_name="apply", sid=9),    # not a reconcile
    span(T1, 10, AF_CTRL, "AcmeFeedback", "fb3", NS, sid=10),               # W2 only
    span(T1, 90, AF_CTRL, "AcmeFeedback", "fb3", NS, sid=11),               # W2 and W3: ambiguous
    span(T2, 3, "clusterthings-v1-0-0-controller", "ClusterThing", "ct1", "", sid=12),  # W4
    span(T2, 3, "clusterthings-v1-0-0-controller", "ClusterThing", "ct1", "ns-x", sid=13),  # namespaced twin
]

# write (actor, object) -> expected (reconciles, first_reconcile_after_ms, errors, ambiguous, other_actors)
EXPECTED = {
    ("alice", "fb1"): (2, 2000, 1, 0, []),
    ("alice", "fb3"): (2, 10000, 0, 1, ["bob"]),
    ("bob", "fb3"): (1, 30000, 0, 1, ["alice"]),
    ("carol", "ct1"): (1, 3000, 0, 0, []),
    ("dave", "fb9"): (0, None, 0, 0, []),
}


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    query = (root / QUERY).read_text()
    port = free_port()
    name = f"audit-join-test-{port}"
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
            print("FAIL ClickHouse never answered")
            return 1
        for table in ("krateo_audit", "otel_traces"):
            ch(port, (root / "tests" / "fixtures" / f"{table}.sql").read_text())
        ch(port, "INSERT INTO krateo_audit FORMAT JSONEachRow", data="\n".join(json.dumps(r) for r in WRITES))
        ch(port, "INSERT INTO otel_traces FORMAT JSONEachRow", data="\n".join(json.dumps(r) for r in SPANS))
        out = ch(port, query.rstrip().rstrip(";") + "\nFORMAT JSON",
                 {"from": ts(T0, -3600), "to": ts(T3, 3600), "window_s": WINDOW_S})
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    rows = json.loads(out)["data"]
    failed = False
    got = {}
    for r in rows:
        key = (r["actor"], r["obj_name"])
        latency = r["first_reconcile_after_ms"]
        got[key] = (int(r["reconciles"]), None if latency is None else int(latency),
                    int(r["error_reconciles"]), int(r["ambiguous_reconciles"]), sorted(r["other_actors"]))
    for key, want in EXPECTED.items():
        have = got.get(key)
        ok = have == want
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} write {key}: got {have}, expected {want}")
    extra = sorted(set(got) - set(EXPECTED))
    failed |= bool(extra)
    print(f"{'ok  ' if not extra else 'FAIL'} no unexpected rows (dry run, failed write, AuditRecord): {extra}")
    kinds = {r["obj_name"]: r["obj_kind"] for r in rows if int(r["reconciles"])}
    ok = kinds.get("fb1") == "AcmeFeedback" and kinds.get("ct1") == "ClusterThing"
    failed |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} the matched span's kind is reported: {kinds}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
