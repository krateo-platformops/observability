#!/usr/bin/env python3
"""Child mode runs where the baseline Pod Security Standard is enforced; the daemonset does not (0.1.39).

A vcluster child's syncer enforces `policies.podSecurityStandard: baseline`, which refuses hostPath
volumes and hostPorts. `helm template` can show the child render has none (tests/collector-child-render.py);
only an API server shows the pod is admitted, becomes Ready and does its job there.

In a kind cluster, in a namespace labelled pod-security.kubernetes.io/enforce=baseline, beside a real
ClickHouse (the version the chart family pins):
  1. the chart installed with tests/fixtures/collector-child-values.yaml (mode deployment) becomes
     Ready, and the live pod has no hostPath volume and no hostPort;
  2. the <release>-local Service has an endpoint (its selector matches the Deployment's pod), which is
     what the apps' OTLP exporters dial;
  3. ClickHouse has table krateo_audit, which only this chart's clickhouse/audit exporter creates:
     the audit card of a child needs it;
  4. no pod of the Deployment was refused.
CONTROL, so a pass is not a namespace that enforces nothing: the chart installed with its defaults
(the daemonset) in the same namespace creates no pod, and the DaemonSet's events carry the API
server's refusal, naming the baseline standard and hostPath.

Not covered: vcluster itself (its syncer, DNS, fake nodes). Run once on a real child after the pin.

Run: python3 tests/collector-child-pss-kind.py [repo-root] [--keep]
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

CHART = "charts/otel-collector-daemonset"
CHILD_VALUES = "tests/fixtures/collector-child-values.yaml"
NS, REL, CTRL, CLUSTER = "krateo-system", "child", "control", "child-" + uuid.uuid4().hex[:6]
CH = os.environ.get("CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:26.10.3.7")
IMG = "otel/opentelemetry-collector-contrib:0.158.0"
FAILS = []
WORK = pathlib.Path(tempfile.mkdtemp(prefix="child-pss-kind-"))
KC = str(WORK / "kubeconfig")
# The enforcement the control must show. A refusal that names something else is not the proof.
REFUSAL = re.compile(r'violates PodSecurity "baseline:[^"]*".*hostPath', re.S)


def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg, flush=True)
    if not ok:
        FAILS.append(msg)


def sh(*a, check=True, **k):
    r = subprocess.run(list(a), capture_output=True, text=True, **k)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(a[:8])} rc={r.returncode}\n{r.stdout[-600:]}{r.stderr[-900:]}")
    return r.stdout


def kc(*a, ns=NS, **kw):
    return sh("kubectl", "--kubeconfig", KC, "--context", f"kind-{CLUSTER}", "-n", ns, *a, **kw)


def helm(*a, **kw):
    return sh("helm", "--kubeconfig", KC, "--kube-context", f"kind-{CLUSTER}", "-n", NS, *a, **kw)


def manifests(pw):
    # A ClickHouse the chart's exporters can reach by the name they are configured with, and a
    # stand-in for the tail sampler's headless Service, which the traces exporter resolves at start.
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
apiVersion: v1
kind: Service
metadata: {{name: otel-collector-tailsampling-opentelemetry-collector}}
spec: {{clusterIP: None, selector: {{app: ch}}, ports: [{{port: 4317}}]}}
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
    return kc("exec", "deploy/ch", "--", "sh", "-c", 'clickhouse-client -u otel_writer --password "$CLICKHOUSE_PASSWORD" -q "$1"', "x", q).strip()


def collector_pods():
    items = json.loads(kc("get", "pods", "-l", f"app.kubernetes.io/instance={REL}", "-o", "json"))["items"]
    return [p for p in items if p["metadata"]["labels"].get("component") == "standalone-collector"]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = pathlib.Path(args[0] if args else ".").resolve()
    if not list((root / CHART / "charts").glob("*")):
        sh("helm", "dependency", "build", str(root / CHART))
    # Both of the chart's own pull-ables, then only one platform each into kind: `kind load
    # docker-image` imports every platform an index names, and fails on those never pulled.
    for i in (IMG, CH):
        if subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode:
            sh("docker", "pull", "-q", i)
    try:
        node = os.environ.get("KIND_NODE_IMAGE")
        sh("kind", "create", "cluster", "--name", CLUSTER, "--kubeconfig", KC, "--wait", "120s", *(["--image", node] if node else []))
        for n, i in enumerate((IMG, CH)):
            tar = str(WORK / f"image-{n}.tar")
            plat = sh("docker", "image", "inspect", "-f", "{{.Os}}/{{.Architecture}}{{with .Variant}}/{{.}}{{end}}", i).strip()
            sh("docker", "save", "--platform", plat, "-o", tar, i)
            sh("kind", "load", "image-archive", "--name", CLUSTER, tar)
        sh("kubectl", "--kubeconfig", KC, "--context", f"kind-{CLUSTER}", "create", "ns", NS)
        # The standard a vcluster child enforces. latest, as a child's syncer asks for the current one.
        kc("label", "ns", NS, "pod-security.kubernetes.io/enforce=baseline", "pod-security.kubernetes.io/enforce-version=latest",
           ns="default")
        labels = kc("get", "ns", NS, "-o", "jsonpath={.metadata.labels}", ns="default")
        check("pod-security.kubernetes.io/enforce" in labels and "baseline" in labels, f"namespace {NS} enforces baseline ({labels})")
        # The ClickHouse must itself be admitted: it is not under test, and a refusal here would read as the chart's.
        kc("apply", "-f", "-", input=manifests(secrets.token_hex(12)))
        kc("rollout", "status", "deploy/ch", "--timeout=180s")

        # 1-4. The child.
        helm("install", REL, str(root / CHART), "-f", str(root / CHILD_VALUES))
        try:
            kc("rollout", "status", f"deploy/{REL}-opentelemetry-collector", "--timeout=240s")
            ready = True
        except SystemExit as e:
            ready = False
            print(str(e)[-800:])
            print(kc("logs", "-l", f"app.kubernetes.io/instance={REL}", "--tail=60", check=False))
            print(kc("get", "events", "--sort-by=.lastTimestamp", check=False)[-3000:])
        pods = collector_pods()
        check(ready and len(pods) == 1 and all(c["ready"] for c in pods[0]["status"]["containerStatuses"]),
              f"child mode under baseline: the collector pod is admitted and Ready ({len(pods)} pod)")
        if pods:
            spec = pods[0]["spec"]
            check(not any("hostPath" in v for v in spec.get("volumes") or [])
                  and not any(p.get("hostPort") for c in spec["containers"] for p in c.get("ports") or [])
                  and not spec.get("hostNetwork"), "the live pod spec has no hostPath, hostPort or hostNetwork")
        events = kc("get", "events", "--field-selector", "reason=FailedCreate", "-o", "json")
        refused = [e["message"] for e in json.loads(events)["items"] if REL in e["involvedObject"]["name"]]
        check(not refused, f"no pod of the child was refused ({refused[:1]})")
        eps = json.loads(kc("get", "endpoints", f"{REL}-local", "-o", "json"))
        addrs = [a for s in eps.get("subsets") or [] for a in s.get("addresses") or []]
        check(len(addrs) >= 1, f"the {REL}-local Service has {len(addrs)} endpoint(s): the apps' OTLP exporters have a target")
        tables = ch("SHOW TABLES FROM default").split()
        check("krateo_audit" in tables, f"the clickhouse/audit exporter created krateo_audit in the child's ClickHouse ({sorted(tables)})")

        # Control: the daemonset, in the same namespace.
        helm("install", CTRL, str(root / CHART))
        msg, ds_ready = "", None
        for _ in range(60):
            ev = json.loads(kc("get", "events", "--field-selector", "reason=FailedCreate", "-o", "json"))["items"]
            msg = next((e["message"] for e in ev if CTRL in e["involvedObject"]["name"] and REFUSAL.search(e["message"])), "")
            if msg:
                break
            time.sleep(2)
        ds = json.loads(kc("get", "ds", "-l", f"app.kubernetes.io/instance={CTRL}", "-o", "json"))["items"]
        ds_ready = sum(d["status"].get("numberReady", 0) + d["status"].get("currentNumberScheduled", 0) for d in ds)
        check(len(ds) == 1 and ds_ready == 0, f"CONTROL: the daemonset has no pod under baseline ({len(ds)} DaemonSet, {ds_ready} scheduled or ready)")
        check(bool(msg), f"CONTROL: the API server refuses it, naming the baseline standard and hostPath ({msg[:200] or 'no such event'})")
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
