#!/usr/bin/env python3
"""Functional test of otel-collector-daemonset's transform/browser-resource (installer#405).

Browser spans reach the node agent from snowplow's rum-ingest, so k8sattributes stamps snowplow's
pod metadata on them. The chart's transform/browser-resource must strip that from krateo-frontend
resources and leave every other resource alone. k8sattributes needs a cluster, so this sends
resources that already carry what it would have added, runs the RENDERED processor in the pinned
image, and checks what comes out.

Run: python3 tests/browser-resource-docker.py [repo-root]
Exit 0 = both checks passed, 1 = at least one did not.
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

CHART = "charts/otel-collector-daemonset"
PROC = "transform/browser-resource"

# What k8sattributes adds for snowplow's pod, as stored on krateo-057 (synthetic values).
SNOWPLOW_POD = {
    "k8s.pod.name": "snowplow-78ddb75758-abcde", "k8s.deployment.name": "snowplow",
    "k8s.namespace.name": "krateo-system", "service.version": "1.12.67",
    "app.kubernetes.io/name": "snowplow", "pod-template-hash": "78ddb75758",
}


def render(root):
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    return yaml.safe_load(cm["data"]["relay"]), ds["spec"]["template"]["spec"]["containers"][0]["image"]


def resource_spans(service, extra):
    attrs = [{"key": "service.name", "value": {"stringValue": service}}]
    attrs += [{"key": k, "value": {"stringValue": v}} for k, v in extra.items()]
    now = time.time_ns()
    return {"resource": {"attributes": attrs}, "scopeSpans": [{"scope": {"name": "t"}, "spans": [{
        "traceId": "%032x" % (now & ((1 << 128) - 1)), "spanId": "%016x" % (now & ((1 << 64) - 1)),
        "name": f"{service} span", "kind": 2, "startTimeUnixNano": str(now - 1000), "endTimeUnixNano": str(now)}]}]}


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, image = render(root)
    traces = relay["service"]["pipelines"]["traces"]["processors"]
    assert PROC in traces and traces.index(PROC) > traces.index("k8sattributes"), \
        f"{PROC} must run after k8sattributes in the traces pipeline: {traces}"
    cfg = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {PROC: relay["processors"][PROC]},
        "exporters": {"file": {"path": "/out/traces.json"}},
        "service": {"pipelines": {"traces": {"receivers": ["otlp"], "processors": [PROC], "exporters": ["file"]}}},
    }
    work = pathlib.Path(tempfile.mkdtemp(prefix="browserres-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir()
    os.chmod(work / "out", 0o777)
    (work / "relay.yaml").write_text(yaml.safe_dump(cfg))
    os.chmod(work / "relay.yaml", 0o644)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"browserres-{port}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:4318", "-e", "MY_POD_IP=0.0.0.0",
                    "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/relay.yaml"],
                   check=True, capture_output=True)
    body = json.dumps({"resourceSpans": [resource_spans("krateo-frontend", SNOWPLOW_POD),
                                         resource_spans("snowplow", SNOWPLOW_POD)]}).encode()
    got = {}
    try:
        for _ in range(60):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/traces", data=body,
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5)
                break
            except Exception:
                time.sleep(1)
        for _ in range(20):
            time.sleep(1)
            path = work / "out" / "traces.json"
            if path.exists():
                for line in path.read_text().splitlines():
                    for rs in json.loads(line).get("resourceSpans", []):
                        a = {x["key"]: x["value"]["stringValue"] for x in rs["resource"]["attributes"]}
                        got[a.get("service.name")] = a
            if len(got) == 2:
                break
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    failed = False
    fe, sp = got.get("krateo-frontend"), got.get("snowplow")
    ok = fe == {"service.name": "krateo-frontend"}
    failed |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} krateo-frontend resource keeps only service.name: {fe}")
    ok = sp == {"service.name": "snowplow", **SNOWPLOW_POD}
    failed |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} snowplow resource untouched: {sorted(sp) if sp else sp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
