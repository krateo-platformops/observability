#!/usr/bin/env python3
"""Functional test of otel-collector-deployment's run-to-completion readiness filter.

k8s.container.ready is meaningless for run-to-completion pods: Job pods (k8s.job.name) and
incident-provider's check pods (pod label app.kubernetes.io/component=incident-check). The chart
drops their readiness and nothing else, then deletes the temporary label attribute. k8sattributes
needs a cluster, so this sends metrics whose resources already carry what it would add, runs the
RENDERED filter and transform, and checks what comes out.

The chart's custom image (ghcr.io/krateo-platformops/otel-collector) has no OTLP receiver and no
file exporter to drive a test through, so this runs the upstream contrib image at the SAME
component version the custom build pins for filterprocessor and transformprocessor (read from
otel-collector's builder-config.yaml, v0.118.0): the same OTTL code. hack/check-collector-components.py
separately proves both processors are in the custom image.

Run: python3 tests/run-to-completion-readiness-docker.py [repo-root]
Exit 0 = every check passed, 1 = at least one did not.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import yaml

CHART = "charts/otel-collector-deployment"
# The contrib release matching the custom image's filterprocessor/transformprocessor v0.118.0.
TEST_IMAGE = "otel/opentelemetry-collector-contrib:0.118.0"
K8SATTR = "k8sattributes/run-to-completion"
FILTER = "filter/run-to-completion-readiness"
DROP = "transform/drop-tmp-component"
TMP = "krateo.tmp.pod.component"


def render(root):
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    wl = next(d for d in docs if d["kind"] == "Deployment")
    return yaml.safe_load(cm["data"]["relay"]), wl["spec"]["template"]["spec"]["containers"][0]["image"]


def resource_metrics(pod, extra, metric_names):
    attrs = [{"key": "k8s.pod.name", "value": {"stringValue": pod}},
             {"key": "k8s.namespace.name", "value": {"stringValue": "krateo-system"}}]
    attrs += [{"key": k, "value": {"stringValue": v}} for k, v in extra.items()]
    now = str(time.time_ns())
    return {"resource": {"attributes": attrs}, "scopeMetrics": [{"scope": {"name": "t"}, "metrics": [
        {"name": m, "gauge": {"dataPoints": [{"timeUnixNano": now, "asInt": "0"}]}} for m in metric_names]}]}


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, _ = render(root)
    image = TEST_IMAGE
    pipeline = relay["service"]["pipelines"]["metrics"]["processors"]
    order = [pipeline.index(p) for p in (K8SATTR, FILTER, DROP)]
    assert order == sorted(order), f"expected {K8SATTR} -> {FILTER} -> {DROP} in the metrics pipeline: {pipeline}"
    labels = relay["processors"][K8SATTR]["extract"]["labels"]
    assert {"tag_name": TMP, "key": "app.kubernetes.io/component", "from": "pod"} in labels, labels
    cfg = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {FILTER: relay["processors"][FILTER], DROP: relay["processors"][DROP]},
        "exporters": {"file": {"path": "/out/metrics.json"}},
        "service": {"pipelines": {"metrics": {"receivers": ["otlp"], "processors": [FILTER, DROP], "exporters": ["file"]}}},
    }
    work = pathlib.Path(tempfile.mkdtemp(prefix="rtcready-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir()
    os.chmod(work / "out", 0o777)
    (work / "relay.yaml").write_text(yaml.safe_dump(cfg))
    os.chmod(work / "relay.yaml", 0o644)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"rtcready-{port}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:4318", "-e", "MY_POD_IP=0.0.0.0",
                    "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/relay.yaml"],
                   check=True, capture_output=True)
    both = ["k8s.container.ready", "k8s.container.restarts"]
    body = json.dumps({"resourceMetrics": [
        resource_metrics("job-pod", {"k8s.job.name": "seal-123"}, both),
        resource_metrics("check-pod", {TMP: "incident-check"}, both),
        resource_metrics("controller-pod", {TMP: "controller"}, both),
        resource_metrics("plain-pod", {}, both),
    ]}).encode()
    got = {}
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/metrics", data=body, headers={"Content-Type": "application/json"}), timeout=5)
                break
            except Exception:
                time.sleep(1)
        for _ in range(20):
            time.sleep(1)
            path = work / "out" / "metrics.json"
            if path.exists() and path.read_text().strip():
                got = {}
                for line in path.read_text().splitlines():
                    for rm in json.loads(line).get("resourceMetrics", []):
                        a = {x["key"]: x["value"]["stringValue"] for x in rm["resource"]["attributes"]}
                        names = [m["name"] for sm in rm["scopeMetrics"] for m in sm["metrics"]]
                        got[a["k8s.pod.name"]] = (sorted(names), a)
                if len(got) == 4:
                    break
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    failed = False

    def check(ok, msg):
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {msg}")

    def names(pod):
        return got.get(pod, ([], {}))[0]

    check(names("job-pod") == ["k8s.container.restarts"], f"Job pod: readiness dropped, other metrics kept {names('job-pod')}")
    check(names("check-pod") == ["k8s.container.restarts"], f"check pod: readiness dropped, other metrics kept {names('check-pod')}")
    check(names("controller-pod") == sorted(both), f"other component label: readiness kept {names('controller-pod')}")
    check(names("plain-pod") == sorted(both), f"plain pod: readiness kept {names('plain-pod')}")
    check(all(TMP not in a for _, a in got.values()) and len(got) == 4, f"{TMP} removed from every resource")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
