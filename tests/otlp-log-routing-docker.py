#!/usr/bin/env python3
"""Functional test of where otel-collector-daemonset sends each OTLP log record (0.1.30).

The daemonset's otlp receiver feeds two log pipelines that write tables:
  * logs/audit    -> krateo_audit (365 days): only records with attribute event.name == "audit";
  * logs/gateway  -> otel_logs (7 days): only the agent gateway's access log (scope `request`
                     with a `gateway` field).
Anything else on the OTLP log stream must land in NEITHER. That matters once
core-provider.otel.logs is on: core-provider 2.13.95 then exports the whole slog stream of
core-provider and of every composition controller over OTLP (the otelslog bridge tees every
record), and those pod logs already reach otel_logs through filelog.

The chart is rendered and the two pipelines run exactly as rendered, with two changes for Docker:
the clickhouse exporters become file exporters, and k8sattributes (which needs the API server) is
left out. Records are sent over OTLP/HTTP, each tagged with a test id (attribute `t`), and the
checks run on what the collector writes:
  * a REAL gateway access-log record (tests/fixtures/gateway-access-log.otlp.jsonl) -> otel_logs;
  * audit records shaped as krateo-platformops/audit v0.13.0 emits them (LogRecord eventName AND
    attribute event.name, audit.go:372 and :457) from core-provider and from a composition
    controller, and snowplow's (attribute only, no eventName) -> krateo_audit;
  * plain slog records from core-provider and from a composition controller, and a `request`-scope
    record without a `gateway` field -> neither table.

Child mode (opentelemetry-collector.mode: deployment, tests/fixtures/collector-child-values.yaml) is
run too. It has no filelog, so a log an app sends over OTLP is in no other table: pipeline
logs/otlp-pods takes what logs/audit and logs/gateway do not, and the plain slog records, which the
daemonset sends nowhere, must each land in otel_logs exactly once, while the audit and gateway
records land only where they did.

Run: python3 tests/otlp-log-routing-docker.py [repo-root]
Needs docker and helm. Exit 0 = all passed.
"""
import copy
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from agentgateway_access_log_v16 import SCOPE_V16, convert_request  # noqa: E402

CHART = "charts/otel-collector-daemonset"
PIPES = ["logs/audit", "logs/gateway"]
CHILD_VALUES = "tests/fixtures/collector-child-values.yaml"
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def sh(*args, check=True, **kw):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, **kw)


def render(root, child):
    if not list((root / CHART / "charts").glob("opentelemetry-collector-*.tgz")):  # CI has none yet
        sh("helm", "dependency", "build", str(root / CHART))
    values = ["-f", str(root / CHILD_VALUES)] if child else []
    out = sh("helm", "template", "route", str(root / CHART), "-n", "krateo-system", *values).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    ds = next(d for d in docs if d["kind"] == ("Deployment" if child else "DaemonSet"))
    return yaml.safe_load(cm["data"]["relay"]), ds["spec"]["template"]["spec"]["containers"][0]["image"]


def test_config(relay, pipe_names):
    svc = relay["service"]["pipelines"]
    for p in pipe_names:
        if p not in svc:
            raise SystemExit(f"rendered config has no pipeline {p}")
    pipes = {p: copy.deepcopy(svc[p]) for p in pipe_names}
    for p in pipes.values():
        if p["receivers"] != ["otlp"]:
            raise SystemExit(f"expected an otlp-only logs pipeline, got receivers {p['receivers']}")
        p["processors"] = [x for x in p.get("processors", []) if x != "k8sattributes"]
        p["exporters"] = [{"clickhouse/audit": "file/audit", "clickhouse": "file/logs"}[e] for e in p["exporters"]]
    used = {x for p in pipes.values() for x in p["processors"]}
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {k: v for k, v in relay["processors"].items() if k in used},
        "exporters": {"file/audit": {"path": "/out/audit.jsonl", "flush_interval": "1s"},
                      "file/logs": {"path": "/out/logs.jsonl", "flush_interval": "1s"}},
        "service": {"pipelines": pipes},
    }


# ------------------------------------------------------------------------------------- records --
def s(v):
    return {"stringValue": v}


def kv(k, v):
    return {"key": k, "value": s(v)}


def wrap(service, scope, lr):
    return {"resourceLogs": [{"resource": {"attributes": [kv("service.name", service)]},
                              "scopeLogs": [{"scope": {"name": scope}, "logRecords": [lr]}]}]}


def now_ns():
    return str(time.time_ns())


def audit_record(tid, with_event_name):
    lr = {"timeUnixNano": now_ns(), "observedTimeUnixNano": now_ns(), "severityNumber": 9, "severityText": "INFO",
          "body": s("helm.upgrade composition"),
          "attributes": [kv("t", tid), kv("event.name", "audit"), kv("schema.version", "1"),
                         kv("krateo.audit.action", "helm.upgrade"), kv("krateo.audit.outcome", "success")]}
    if with_event_name:
        lr["eventName"] = "audit"
    return lr


def slog_record(tid, msg):
    return {"timeUnixNano": now_ns(), "observedTimeUnixNano": now_ns(), "severityNumber": 9, "severityText": "INFO",
            "body": s(msg), "attributes": [kv("t", tid), kv("logger", "controller")]}


def gateway_record(root, tid):
    for line in (root / "tests/fixtures/gateway-access-log.otlp.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        req = json.loads(line)
        rl = req["resourceLogs"][0]
        sl = rl["scopeLogs"][0]
        lr = copy.deepcopy(sl["logRecords"][0])
        lr["attributes"] = [a for a in lr["attributes"] if a["key"] != "t"] + [kv("t", tid)]
        if sl["scope"].get("name") != "request" or not any(a["key"] == "gateway" for a in lr["attributes"]):
            raise SystemExit("fixture's first record is not a gateway access-log record")
        return {"resourceLogs": [{"resource": rl["resource"], "scopeLogs": [{"scope": sl["scope"], "logRecords": [lr]}]}]}
    raise SystemExit("empty fixture")


def build_cases(root):
    cp, cdc = "github.com/krateo-platformops/core-provider", "github.com/krateo-platformops/composition-dynamic-controller"
    no_gw = slog_record("request-scope-no-gateway", "GET /healthz 200")
    return [
        ("gateway-access-log", gateway_record(root, "gateway-access-log"), "logs"),
        ("core-provider-audit", wrap("core-provider", cp + "/audit", audit_record("core-provider-audit", True)), "audit"),
        ("cdc-audit", wrap("composition-dynamic-controller", cdc + "/audit", audit_record("cdc-audit", True)), "audit"),
        ("snowplow-audit-attr-only", wrap("snowplow", "github.com/krateo-platformops/snowplow/audit",
                                         audit_record("snowplow-audit-attr-only", False)), "audit"),
        ("core-provider-slog", wrap("core-provider", cp, slog_record("core-provider-slog", "reconciling CompositionDefinition")), None),
        ("cdc-slog", wrap("composition-dynamic-controller", cdc, slog_record("cdc-slog", "helm upgrade done")), None),
        ("request-scope-no-gateway", wrap("some-service", "request", no_gw), None),
        # agentgateway >= 1.6: scope agentgateway.access, semconv HTTP keys. Lands in otel_logs AS
        # SENT (checked below); the same scope without a `gateway` field lands nowhere.
        ("gateway-access-log-1.6", convert_request(gateway_record(root, "gateway-access-log-1.6")), "logs"),
        ("access-scope-no-gateway", wrap("some-service", SCOPE_V16, slog_record("access-scope-no-gateway", "GET /healthz 200")), None),
    ]


def read_out(path):
    recs = []
    if not path.exists():
        return recs
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        for rl in json.loads(line).get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    a = {x["key"]: next(iter(x["value"].values()), None) for x in lr.get("attributes", [])}
                    recs.append(a.get("t"))
    return recs


def read_full(path, tid):
    """The scope name and attribute keys of the record tagged tid, as the collector wrote it."""
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        for rl in json.loads(line).get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    keys = {x["key"]: next(iter(x["value"].values()), None) for x in lr.get("attributes", [])}
                    if keys.get("t") == tid:
                        return sl.get("scope", {}).get("name"), keys
    return None


def run(root, child):
    print("--- " + ("child (deployment)" if child else "parent (daemonset)"))
    relay, image = render(root, child)
    cfg = test_config(relay, PIPES + (["logs/otlp-pods"] if child else []))
    # In a child the records the daemonset sends nowhere (pods' own logs come by filelog there) land
    # in otel_logs, once. Audit and gateway records are unchanged.
    cases = [(t, r, want or ("logs" if child else None)) for t, r, want in build_cases(root)]
    work = pathlib.Path(tempfile.mkdtemp(prefix="otlproute-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir(); os.chmod(work / "out", 0o777)
    (work / "otel.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False)); os.chmod(work / "otel.yaml", 0o644)
    name = "otlproute-" + uuid.uuid4().hex[:8]
    s_ = socket.socket(); s_.bind(("127.0.0.1", 0)); port = s_.getsockname()[1]; s_.close()
    try:
        sh("docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:4318",
           "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/otel.yaml")
        up = False
        for _ in range(120):
            try:
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/logs", data=b"{}",
                                       headers={"content-type": "application/json"}), timeout=2).read()
                up = True; break
            except urllib.error.HTTPError:
                up = True; break
            except OSError:
                time.sleep(0.5)
        if not up:
            raise SystemExit("collector never answered:\n" + sh("docker", "logs", "--tail", "60", name, check=False).stderr[-4000:])
        for tid, req, _ in cases:
            r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/logs", data=json.dumps(req).encode(),
                                       headers={"content-type": "application/json"}, method="POST")
            urllib.request.urlopen(r, timeout=10).read()
        time.sleep(8)
        audit = read_out(work / "out" / "audit.jsonl")
        logs = read_out(work / "out" / "logs.jsonl")
        for tid, _, want in cases:
            got = [d for d, out in (("audit", audit), ("logs", logs)) if tid in out]
            check(got == ([want] if want else []), f"{tid}: lands in {got or 'neither table'}, want {want or 'neither table'}")
        check(len(audit) == sum(1 for c in cases if c[2] == "audit"), f"krateo_audit got exactly the audit records ({len(audit)})")
        check(len(logs) == sum(1 for c in cases if c[2] == "logs"),
              f"otel_logs got exactly the records meant for it ({len(logs)}: " + ("gateway and app logs, each once" if child else "gateway") + ")")
        new = read_full(work / "out" / "logs.jsonl", "gateway-access-log-1.6")
        check(new is not None and new[0] == SCOPE_V16 and "client.address" in new[1] and "http.request.method" in new[1]
              and "src.addr" not in new[1] and "http.method" not in new[1],
              f"the 1.6+ access-log record reaches otel_logs AS SENT: its scope and semconv keys, not renamed back "
              f"({new[0] if new else None})")
    finally:
        if os.environ.get("SHOW_LOGS") == "1":
            print(sh("docker", "logs", "--tail", "80", name, check=False).stderr[-6000:])
        sh("docker", "rm", "-f", name, check=False)
        shutil.rmtree(work, ignore_errors=True)


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    for child in (False, True):
        run(root, child)
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
