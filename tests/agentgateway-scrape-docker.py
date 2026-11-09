#!/usr/bin/env python3
"""Functional test of otel-collector-daemonset's agentgateway scrape job (OTEL-14(a), installer#406).

Kubernetes service discovery can't run in Docker, so this renders the chart, keeps the job's
relabel_configs exactly as rendered, and swaps only the discovery: file_sd targets that carry the
same __meta_kubernetes_* labels the pod role would. A stand-in gateway serves agentgateway-shaped
Prometheus text. The collector writes what it scraped to a file, and the test checks:
  - the annotated gateway is scraped, at its annotation's port and path;
  - a target whose prometheus.io/scrape is not "true" is dropped;
  - service.name is the pod's app.kubernetes.io/name, as on the gateway's traces;
  - every point's resource carries the pod's namespace and name (k8s.namespace.name, k8s.pod.name);
  - request counters arrive under their Prometheus names.

Run: python3 tests/agentgateway-scrape-docker.py [repo-root]
Exit 0 = every check passed, 1 = at least one did not.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import yaml

CHART = "charts/otel-collector-daemonset"
RECEIVER = "prometheus/agentgateway"

# agentgateway-shaped exposition: names, types and label keys as agentgateway v1.5 serves them,
# with synthetic values.
METRICS = """# HELP agentgateway_requests The total number of HTTP requests sent.
# TYPE agentgateway_requests counter
agentgateway_requests_total{backend="snowplow.krateo-system.svc.cluster.local:8081",protocol="http",method="GET",status="200",reason="Upstream",gateway="krateo-system/test-gateway",listener="http",route="krateo-system/test-route",route_rule="unknown"} 42
agentgateway_requests_total{backend="snowplow.krateo-system.svc.cluster.local:8081",protocol="http",method="GET",status="503",reason="Upstream",gateway="krateo-system/test-gateway",listener="http",route="krateo-system/test-route",route_rule="unknown"} 3
# HELP agentgateway_request_duration_seconds Duration of HTTP requests.
# TYPE agentgateway_request_duration_seconds histogram
agentgateway_request_duration_seconds_bucket{gateway="krateo-system/test-gateway",route="krateo-system/test-route",le="0.1"} 40
agentgateway_request_duration_seconds_bucket{gateway="krateo-system/test-gateway",route="krateo-system/test-route",le="+Inf"} 45
agentgateway_request_duration_seconds_sum{gateway="krateo-system/test-gateway",route="krateo-system/test-route"} 2.5
agentgateway_request_duration_seconds_count{gateway="krateo-system/test-gateway",route="krateo-system/test-route"} 45
# HELP agentgateway_config_synchronized Whether the config is synchronized.
# TYPE agentgateway_config_synchronized gauge
agentgateway_config_synchronized 1
"""


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def render(root):
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    return yaml.safe_load(cm["data"]["relay"]), ds["spec"]["template"]["spec"]["containers"][0]["image"]


def target(host, scrape, name):
    """A file_sd target carrying the labels Kubernetes pod discovery would give a gateway pod."""
    return {
        "targets": [f"{host}:15020"],
        "labels": {
            "__meta_kubernetes_pod_ip": host,
            "__meta_kubernetes_pod_annotation_prometheus_io_scrape": scrape,
            "__meta_kubernetes_pod_annotation_prometheus_io_port": "15020",
            "__meta_kubernetes_pod_annotation_prometheus_io_path": "/metrics",
            "__meta_kubernetes_pod_label_app_kubernetes_io_name": name,
            "__meta_kubernetes_namespace": "krateo-system",
            "__meta_kubernetes_pod_name": f"{name}-abc12",
        },
    }


def test_config(relay):
    """The chart's scrape job untouched except its discovery; a file exporter instead of ClickHouse."""
    job = relay["receivers"][RECEIVER]["config"]["scrape_configs"][0]
    assert job["kubernetes_sd_configs"][0]["selectors"][0]["label"] == \
        "gateway.networking.k8s.io/gateway-class-name=agentgateway", "the job must select the agentgateway class"
    job.pop("kubernetes_sd_configs")
    job["file_sd_configs"] = [{"files": ["/cfg/targets.json"]}]
    job["scrape_interval"] = "2s"
    pipeline = relay["service"]["pipelines"]["metrics/agentgateway"]
    assert pipeline["receivers"] == [RECEIVER], pipeline
    return {
        "receivers": {RECEIVER: relay["receivers"][RECEIVER]},
        "processors": {p: relay["processors"][p] for p in pipeline["processors"]},
        "exporters": {"file": {"path": "/out/metrics.json"}},
        "service": {"pipelines": {"metrics/agentgateway": {
            "receivers": [RECEIVER], "processors": pipeline["processors"], "exporters": ["file"]}}},
    }


def read_points(path):
    """service.name -> list of (metric name, resource attrs, point attrs)."""
    got = {}
    if not path.exists():
        return got
    for line in path.read_text().splitlines():
        for rm in json.loads(line).get("resourceMetrics", []):
            res = {a["key"]: next(iter(a["value"].values())) for a in rm["resource"].get("attributes", [])}
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    for kind in ("sum", "gauge", "histogram"):
                        for dp in m.get(kind, {}).get("dataPoints", []):
                            attrs = {a["key"]: next(iter(a["value"].values())) for a in dp.get("attributes", [])}
                            got.setdefault(res.get("service.name", ""), []).append((m["name"], res, attrs))
    return got


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, image = render(root)
    work = pathlib.Path(tempfile.mkdtemp(prefix="gwscrape-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir()
    os.chmod(work / "out", 0o777)
    (work / "www").mkdir()
    (work / "www" / "metrics").write_text(METRICS)
    for p in (work / "www", work / "www" / "metrics"):
        os.chmod(p, 0o755 if p.is_dir() else 0o644)
    net = f"gwscrape-{os.getpid()}"
    scraped, dropped = f"{net}-gw", f"{net}-off"
    (work / "targets.json").write_text(json.dumps([
        target(scraped, "true", "krateo-agent-gateway-platform"),
        target(dropped, "false", "not-scraped-gateway"),
    ]))
    (work / "relay.yaml").write_text(yaml.safe_dump(test_config(relay)))
    for f in ("targets.json", "relay.yaml"):
        os.chmod(work / f, 0o644)

    docker("network", "create", net)
    names = []
    try:
        for n in (scraped, dropped):
            docker("run", "-d", "--name", n, "--network", net, "-v", f"{work / 'www'}:/srv:ro",
                   "python:3.12-alpine", "python3", "-m", "http.server", "15020", "--directory", "/srv")
            names.append(n)
        col = f"{net}-collector"
        docker("run", "-d", "--name", col, "--network", net, "-e", "MY_POD_IP=0.0.0.0",
               "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/relay.yaml")
        names.append(col)
        got = {}
        for _ in range(45):
            time.sleep(1)
            if docker("inspect", "-f", "{{.State.Running}}", col, check=False).stdout.strip() != "true":
                print("FAIL collector exited:\n" + docker("logs", col, check=False).stderr[-3000:])
                return 1
            got = read_points(work / "out" / "metrics.json")
            if "krateo-agent-gateway-platform" in got:
                time.sleep(4)
                got = read_points(work / "out" / "metrics.json")
                break
    finally:
        for n in names:
            docker("rm", "-f", n, check=False)
        docker("network", "rm", net, check=False)

    failed = False

    def check(ok, msg):
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {msg}")

    pts = got.get("krateo-agent-gateway-platform", [])
    check(bool(pts), f"annotated gateway scraped: {len(pts)} points under service.name krateo-agent-gateway-platform")
    check("not-scraped-gateway" not in got, "target with prometheus.io/scrape=false dropped")
    names_seen = sorted({m for m, _, _ in pts})
    print(f"info metric names: {names_seen}")
    check(any(m.startswith("agentgateway_requests") for m in names_seen), "request counter arrives")
    check(any(m.startswith("agentgateway_request_duration_seconds") for m in names_seen), "latency histogram arrives")
    if pts:
        _, res, attrs = next(p for p in pts if p[0].startswith("agentgateway_requests"))
        print(f"info resource: {sorted(res.items())}")
        print(f"info point attributes: {sorted(attrs)}")
        ns_ok = all(r.get("k8s.namespace.name") == "krateo-system" for _, r, _ in pts)
        pod_ok = all(r.get("k8s.pod.name", "").startswith("krateo-agent-gateway-platform-") for _, r, _ in pts)
        check(ns_ok, "every point's resource names the pod's namespace (k8s.namespace.name)")
        check(pod_ok, "every point's resource names the pod (k8s.pod.name)")
        check(not any(k.startswith("k8s_") for _, _, a in pts for k in a), "no duplicate k8s_* point attributes")
        check(attrs.get("status") in ("200", "503") and attrs.get("route") == "krateo-system/test-route",
              "request labels (route, status) kept as point attributes")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
