#!/usr/bin/env python3
"""The replicated audit collector keeps every record through a roll and a pod deletion (0.1.38).

Render first: off, the chart renders nothing of it; on, its pipelines and components equal the agents'
own, except k8sattributes' node filter, and every ${env:X} it names is set on the container.

Then the chart is installed in kind with auditCollector.enabled, beside a ClickHouse of the version the
chart family pins, and a sender posts numbered audit records over OTLP/HTTP to <release>-audit. Like
agentgateway and Go's otlploghttp, the sender does NOT retry a refused or broken connection, so a
gap in the endpoint is a lost record. Every number must reach krateo_audit, for:
  * kubectl rollout restart of the Deployment;
  * deletion of the copy that is receiving the most.

Control, so a pass means something: the same events on ONE copy (replicas 1, Recreate, no preStop)
must lose records.

Not covered: a crash (a killed copy drops what it holds), a node failure, ClickHouse being away.

Run: python3 tests/audit-collector-kind.py [repo-root] [--keep]
Needs docker, kind, kubectl, helm. Uses its own kubeconfig; never the caller's. Exit 0 = passed.
"""
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid

import yaml

CHART = "charts/otel-collector-daemonset"
NS, REL, CLUSTER = "krateo-system", "audit-test", "audit-" + uuid.uuid4().hex[:6]
CH = os.environ.get("CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:26.10.3.7")
PY = "python:3.12-alpine"
COUNT = 1200
FAILS = []
WORK = pathlib.Path(tempfile.mkdtemp(prefix="audit-kind-"))
KC = str(WORK / "kubeconfig")


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg, flush=True)
    if not ok:
        FAILS.append(msg)


def sh(*a, check=True, **k):
    r = subprocess.run(list(a), capture_output=True, text=True, **k)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(a[:8])} rc={r.returncode}\n{r.stdout[-600:]}{r.stderr[-900:]}")
    return r.stdout


def k(*a, **kw):
    return sh("kubectl", "--kubeconfig", KC, "--context", f"kind-{CLUSTER}", "-n", NS, *a, **kw)


SENDER = """
import json, sys, time, urllib.request
run, n, url = sys.argv[1], int(sys.argv[2]), sys.argv[3]
bad = 0
for i in range(1, n + 1):
    rec = {"timeUnixNano": str(time.time_ns()), "body": {"stringValue": "%s-%d" % (run, i)},
           "attributes": [{"key": "event.name", "value": {"stringValue": "audit"}}]}
    body = json.dumps({"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "audit-test"}}]},
                       "scopeLogs": [{"scope": {"name": "t"}, "logRecords": [rec]}]}]}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}), timeout=5).read()
    except Exception as e:
        bad += 1
        if bad <= 3: print(i, type(e).__name__, str(e)[:90], flush=True)
    time.sleep(0.03)
print("sent", n, "refused-or-broken", bad, flush=True)
"""


def manifests(pw):
    return f"""
apiVersion: v1
kind: Secret
metadata: {{name: otel-clickhouse-credentials}}
stringData: {{username: otel_writer, password: "{pw}"}}
---
apiVersion: v1
kind: Service
metadata: {{name: krateo-clickstack-clickhouse-clickhouse-headless}}
spec: {{selector: {{app: ch}}, ports: [{{port: 9000}}]}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: ch}}
spec:
  selector: {{matchLabels: {{app: ch}}}}
  template:
    metadata: {{labels: {{app: ch}}}}
    spec:
      containers:
        - name: ch
          image: {CH}
          imagePullPolicy: IfNotPresent
          env:
            - {{name: CLICKHOUSE_USER, value: otel_writer}}
            - {{name: CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT, value: "1"}}
            - {{name: CLICKHOUSE_PASSWORD, valueFrom: {{secretKeyRef: {{name: otel-clickhouse-credentials, key: password}}}}}}
          readinessProbe: {{tcpSocket: {{port: 9000}}, periodSeconds: 2}}
"""


def ch(q):
    return k("exec", "deploy/ch", "--", "sh", "-c", 'clickhouse-client -u otel_writer --password "$CLICKHOUSE_PASSWORD" -q "$1"', "x", q).strip()


def pods():
    items = json.loads(k("get", "pods", "-l", "component=audit-collector", "-o", "json"))["items"]
    return [p["metadata"]["name"] for p in items if not p["metadata"].get("deletionTimestamp")]


def settle(n):
    k("rollout", "status", f"deploy/{REL}-audit", "--timeout=180s")
    for _ in range(90):
        if len(json.loads(k("get", "pods", "-l", "component=audit-collector", "-o", "json"))["items"]) == n:
            break
        time.sleep(2)
    time.sleep(5)


def case(label, event, replicas):
    run = uuid.uuid4().hex[:8]
    settle(replicas)
    k("run", f"s-{run}", f"--image={PY}", "--image-pull-policy=IfNotPresent", "--restart=Never", "--command", "--",
      "python3", "-c", SENDER, run, str(COUNT), f"http://{REL}-audit.{NS}.svc.cluster.local:4318/v1/logs")
    k("wait", "--for=condition=Ready", f"pod/s-{run}", "--timeout=90s")
    time.sleep(8)
    if event == "roll":
        k("rollout", "restart", f"deploy/{REL}-audit")
    elif event == "delete":
        k("delete", "pod", pods()[0], "--wait=false")
    for _ in range(120):
        if k("get", "pod", f"s-{run}", "-o", "jsonpath={.status.phase}") in ("Succeeded", "Failed"):
            break
        time.sleep(1)
    # The batch and the exporter's own retries, then nothing new for a while.
    prev, quiet, got = -1, time.monotonic(), 0
    end = time.monotonic() + 120
    while time.monotonic() < end and time.monotonic() - quiet < 20:
        got = int(ch(f"SELECT uniqExact(Body) FROM default.krateo_audit WHERE Body LIKE '{run}-%'"))
        if got != prev:
            prev, quiet = got, time.monotonic()
        time.sleep(3)
    dup = int(ch(f"SELECT count() - uniqExact(Body) FROM default.krateo_audit WHERE Body LIKE '{run}-%'"))
    log = k("logs", f"s-{run}").strip().splitlines()
    print(f"     {label}: stored {got} of {COUNT}, duplicates {dup}; {log[-1] if log else 'no sender log'}", flush=True)
    return COUNT - got


def render(root):
    def docs(*a):
        return [d for d in yaml.safe_load_all(sh("helm", "template", REL, str(root / CHART), "-n", NS, *a)) if d]
    off, on = docs(), docs("--set", "auditCollector.enabled=true")
    extra = [d for d in on if d not in off]
    check(sorted(d["kind"] for d in extra) == ["ConfigMap", "Deployment", "PodDisruptionBudget", "Service"]
          and all(d in on for d in off), "on adds exactly its four objects and changes no other; off renders none of them")
    cm = {d["metadata"]["name"]: yaml.safe_load(d["data"]["relay"]) for d in on if d["kind"] == "ConfigMap" and "relay" in d["data"]}
    audit = cm.pop(f"{REL}-audit")
    agent = next(iter(cm.values()))
    check(set(audit["service"]["pipelines"]) == {"logs/audit", "logs/gateway", "logs/gateway-audit", "logs/gateway-audit-excluded", "metrics/gateway-audit-excluded"}
          and all(audit["service"]["pipelines"][n] == agent["service"]["pipelines"][n] for n in audit["service"]["pipelines"]), "its pipelines are the agents' own")
    diff = [(kind, n) for kind in ("receivers", "processors", "exporters", "connectors") for n in audit[kind] if audit[kind][n] != agent[kind][n]]
    check(diff == [("processors", "k8sattributes")] and "filter" not in audit["processors"]["k8sattributes"]
          and {k_: v for k_, v in agent["processors"]["k8sattributes"].items() if k_ != "filter"} == audit["processors"]["k8sattributes"],
          f"its components are the agents' own, less k8sattributes' node filter ({diff})")
    spec = next(d for d in extra if d["kind"] == "Deployment")["spec"]["template"]["spec"]
    c = spec["containers"][0]
    refs = set(re.findall(r"\$\{env:(\w+)", yaml.safe_dump(audit)))
    check(refs <= {e["name"] for e in c["env"]}, f"every environment variable it names is set ({sorted(refs - {e['name'] for e in c['env']})})")
    check(c["securityContext"]["runAsNonRoot"] is True and not any("hostPath" in v for v in spec["volumes"]), "not root, no hostPath")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = pathlib.Path(args[0] if args else ".").resolve()
    if not list((root / CHART / "charts").glob("*")):
        sh("helm", "dependency", "build", str(root / CHART))
    img = "otel/opentelemetry-collector-contrib:0.158.0"
    render(root)
    out = sh("helm", "template", REL, str(root / CHART), "-n", NS, "--set", "auditCollector.enabled=true")
    check(f'image: "{img}"' in out, f"the audit collector runs the agents' image ({img})")
    for i in (img, CH, PY):
        if subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode:
            sh("docker", "pull", "-q", i)
    try:
        node = os.environ.get("KIND_NODE_IMAGE")
        sh("kind", "create", "cluster", "--name", CLUSTER, "--kubeconfig", KC, "--wait", "120s", *(["--image", node] if node else []))
        sh("kind", "load", "docker-image", "--name", CLUSTER, img, CH, PY)
        sh("kubectl", "--kubeconfig", KC, "--context", f"kind-{CLUSTER}", "create", "ns", NS)
        k("apply", "-f", "-", input=manifests(secrets.token_hex(12)))
        k("rollout", "status", "deploy/ch", "--timeout=180s")
        # Only the audit collector is under test: the agents need a node's log and host paths.
        sh("helm", "--kubeconfig", KC, "--kube-context", f"kind-{CLUSTER}", "-n", NS, "install", REL, str(root / CHART),
           "--set", "auditCollector.enabled=true", "--set", "opentelemetry-collector.nodeSelector.none=none")
        k("rollout", "status", f"deploy/{REL}-audit", "--timeout=180s")
        check(len(pods()) == 2, "two copies are up")
        for ev in ("none", "roll", "delete"):
            lost = case(f"two copies, {ev}", ev, 2)
            check(lost == 0, f"two copies, {ev}: no record lost ({lost})")
        k("patch", f"deploy/{REL}-audit", "--type=json", "-p", json.dumps([
            {"op": "replace", "path": "/spec/replicas", "value": 1},
            {"op": "replace", "path": "/spec/strategy", "value": {"type": "Recreate"}},
            {"op": "remove", "path": "/spec/template/spec/containers/0/lifecycle"}]))
        for ev in ("roll", "delete"):
            lost = case(f"CONTROL one copy, {ev}", ev, 1)
            check(lost > 0, f"CONTROL one copy, {ev}: records are lost ({lost})")
        check(k("get", "pods", "-l", "component=audit-collector", "-o", "jsonpath={.items[*].status.containerStatuses[0].restartCount}").strip() == "0", "no collector restarted on its own")
    finally:
        if "--keep" not in sys.argv:
            sh("kind", "delete", "cluster", "--name", CLUSTER, "--kubeconfig", KC, check=False)
            for f in WORK.iterdir():
                f.unlink()
            WORK.rmdir()
    print(f"{len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
