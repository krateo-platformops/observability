#!/usr/bin/env python3
"""Functional test of otel-collector-daemonset's ClickHouse disk scrape (0.1.39), parent and child.

sre-clickhouse-disk-headroom reads ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes, which
this chart's collector scrapes from ClickHouse's own Prometheus endpoint. Kubernetes discovery
can't run in Docker, so this renders the chart, keeps the job's relabel_configs and
metric_relabel_configs exactly as rendered, and swaps only the discovery: file_sd targets that
carry the labels the pod role would. A stand-in ClickHouse serves the real family names. The
collector writes what it scraped to a file, and the test checks:
  - the free-bytes metric arrives under exactly the name the Metric resource in krateo-observability
    names, with the stand-in's value, and so does the total;
  - nothing else from that endpoint is exported (the keep relabel): a ClickHouse endpoint serves
    thousands of families, and they must not be written into the ClickHouse it monitors;
  - a pod whose port is not named `prometheus` is not scraped;
  - the rendered selector, in the daemonset (parent): pins the node with ${env:K8S_NODE_NAME}; in
    deployment mode (child): has the label and no node field, and no ${env:K8S_NODE_NAME} is left
    in the config.
It runs for both renders, so a parent and a child scrape by the same rules.

Run: python3 tests/clickhouse-disk-scrape-docker.py [repo-root]
Needs docker and helm. Exit 0 = every check passed, 1 = at least one did not.
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
RECEIVER = "prometheus/clickhouse"
PIPELINE = "metrics/clickhouse"
WANTED = {
    "ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes": 7689277440,
    "ClickHouseAsyncMetrics_FilesystemMainPathTotalBytes": 15744311296,
}
# The alert's Metric resource is read from krateo-observability's values, so a rename on either side
# fails here.
OBSERVABILITY_VALUES = "charts/krateo-observability/values.yaml"
CHILD_VALUES = {"opentelemetry-collector": {
    "mode": "deployment",
    "presets": {"logsCollection": {"enabled": False}, "hostMetrics": {"enabled": False},
                "kubeletMetrics": {"enabled": False}},
    "clusterRole": {"rules": []}}}

# What a ClickHouse :9363/metrics serves, in shape: the two wanted families, a Disk* row with a
# disk label, and families that must never be exported.
METRICS = """# HELP ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes Filesystem available bytes.
# TYPE ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes gauge
ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes 7689277440
# HELP ClickHouseAsyncMetrics_FilesystemMainPathTotalBytes Filesystem total bytes.
# TYPE ClickHouseAsyncMetrics_FilesystemMainPathTotalBytes gauge
ClickHouseAsyncMetrics_FilesystemMainPathTotalBytes 15744311296
# HELP ClickHouseAsyncMetrics_FilesystemMainPathUsedBytes Filesystem used bytes.
# TYPE ClickHouseAsyncMetrics_FilesystemMainPathUsedBytes gauge
ClickHouseAsyncMetrics_FilesystemMainPathUsedBytes 8055033856
# HELP ClickHouseAsyncMetrics_DiskAvailable Disk available.
# TYPE ClickHouseAsyncMetrics_DiskAvailable gauge
ClickHouseAsyncMetrics_DiskAvailable{disk="default"} 7689277440
# HELP ClickHouseProfileEvents_Query Queries.
# TYPE ClickHouseProfileEvents_Query counter
ClickHouseProfileEvents_Query_total 123456
# HELP ClickHouseMetrics_Query Running queries.
# TYPE ClickHouseMetrics_Query gauge
ClickHouseMetrics_Query 3
"""


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def render(root, child):
    if not list((root / CHART / "charts").glob("opentelemetry-collector-*.tgz")):  # CI has none yet
        subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    args = ["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"]
    tmp = None
    if child:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump(CHILD_VALUES, tmp)
        tmp.close()
        args += ["-f", tmp.name]
    try:
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    finally:
        if tmp:
            os.unlink(tmp.name)
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    wl = next(d for d in docs if d["kind"] == ("Deployment" if child else "DaemonSet"))
    return cm["data"]["relay"], wl["spec"]["template"]["spec"]["containers"][0]["image"]


def target(host, port_name):
    """A file_sd target carrying the labels pod discovery gives a pod's container port."""
    return {
        "targets": [f"{host}:9363"],
        "labels": {
            "__meta_kubernetes_pod_ip": host,
            "__meta_kubernetes_pod_container_port_name": port_name,
            "__meta_kubernetes_namespace": "krateo-system",
            "__meta_kubernetes_pod_name": f"{host}-0",
        },
    }


def test_config(relay):
    """The chart's job untouched except its discovery; a file exporter instead of ClickHouse."""
    job = relay["receivers"][RECEIVER]["config"]["scrape_configs"][0]
    job.pop("kubernetes_sd_configs")
    job["file_sd_configs"] = [{"files": ["/cfg/targets.json"]}]
    job["scrape_interval"] = "2s"
    pipeline = relay["service"]["pipelines"][PIPELINE]
    assert pipeline["receivers"] == [RECEIVER], pipeline
    return {
        "receivers": {RECEIVER: relay["receivers"][RECEIVER]},
        "processors": {p: relay["processors"][p] for p in pipeline["processors"]},
        "exporters": {"file": {"path": "/out/metrics.json"}},
        "service": {"pipelines": {PIPELINE: {
            "receivers": [RECEIVER], "processors": pipeline["processors"], "exporters": ["file"]}}},
    }


def read_points(path):
    """metric name -> list of (value, resource attrs, point attrs)."""
    got = {}
    if not path.exists():
        return got
    for line in path.read_text().splitlines():
        for rm in json.loads(line).get("resourceMetrics", []):
            res = {a["key"]: next(iter(a["value"].values())) for a in rm["resource"].get("attributes", [])}
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    for kind in ("sum", "gauge"):
                        for dp in m.get(kind, {}).get("dataPoints", []):
                            attrs = {a["key"]: next(iter(a["value"].values())) for a in dp.get("attributes", [])}
                            value = dp.get("asInt", dp.get("asDouble"))
                            got.setdefault(m["name"], []).append((float(value), res, attrs))
    return got


failed = False


def check(ok, msg):
    global failed
    failed |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {msg}")


def run(root, child):
    label = "child (deployment)" if child else "parent (daemonset)"
    print(f"--- {label}")
    relay_text, image = render(root, child)
    relay = yaml.safe_load(relay_text)
    selector = relay["receivers"][RECEIVER]["config"]["scrape_configs"][0]["kubernetes_sd_configs"][0]["selectors"][0]
    check(selector["label"] == "clickhouse.com/role=clickhouse-server", "selects the operator's server role label")
    if child:
        check("field" not in selector, "no node field selector in a child")
        check("${env:K8S_NODE_NAME}" not in relay_text, "no ${env:K8S_NODE_NAME} left anywhere in the child config")
    else:
        check(selector.get("field") == "spec.nodeName=${env:K8S_NODE_NAME}",
              "the parent pins the node, so one agent scrapes")
    work = pathlib.Path(tempfile.mkdtemp(prefix="chscrape-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir()
    os.chmod(work / "out", 0o777)
    (work / "www").mkdir()
    (work / "www" / "metrics").write_text(METRICS)
    os.chmod(work / "www", 0o755)
    os.chmod(work / "www" / "metrics", 0o644)
    net = f"chscrape-{os.getpid()}-{int(child)}"
    scraped, dropped = f"{net}-ch", f"{net}-other"
    (work / "targets.json").write_text(json.dumps([target(scraped, "prometheus"), target(dropped, "http")]))
    (work / "relay.yaml").write_text(yaml.safe_dump(test_config(relay)))
    for f in ("targets.json", "relay.yaml"):
        os.chmod(work / f, 0o644)
    docker("network", "create", net)
    names = []
    got = {}
    try:
        for n in (scraped, dropped):
            docker("run", "-d", "--name", n, "--network", net, "-v", f"{work / 'www'}:/srv:ro",
                   "python:3.12-alpine", "python3", "-m", "http.server", "9363", "--directory", "/srv")
            names.append(n)
        col = f"{net}-collector"
        docker("run", "-d", "--name", col, "--network", net, "-e", "MY_POD_IP=0.0.0.0",
               "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/relay.yaml")
        names.append(col)
        for _ in range(45):
            time.sleep(1)
            if docker("inspect", "-f", "{{.State.Running}}", col, check=False).stdout.strip() != "true":
                check(False, "collector exited:\n" + docker("logs", col, check=False).stderr[-3000:])
                return
            got = read_points(work / "out" / "metrics.json")
            if all(n in got for n in WANTED):
                time.sleep(4)
                got = read_points(work / "out" / "metrics.json")
                break
    finally:
        for n in names:
            docker("rm", "-f", n, check=False)
        docker("network", "rm", net, check=False)
    print(f"info metric names exported: {sorted(got)}")
    for name, value in WANTED.items():
        pts = got.get(name, [])
        check(bool(pts) and all(v == value for v, _, _ in pts), f"{name} arrives with the stand-in's value {value}")
    avail = got.get("ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes", [])
    if avail:
        print(f"info resource: {sorted(avail[0][1].items())}; point attributes: {sorted(avail[0][2])}")
        check(all(r.get("k8s.pod.name") == f"{scraped}-0" for _, r, _ in avail),
              "only the pod whose port is named prometheus was scraped")
    extra = sorted(n for n in got if n not in WANTED and not (n == "up" or n.startswith("scrape_")))
    check(not extra, f"nothing else from the endpoint is exported (found: {extra})")


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    values = yaml.safe_load((root / OBSERVABILITY_VALUES).read_text())
    refs = {m["name"]: m["metricName"] for m in values["defaultMetrics"]}
    alert = next(a for a in values["defaultAlerts"]["alerts"] if a["name"] == "sre-clickhouse-disk-headroom")
    check(refs.get(alert["metricRef"]) == "ClickHouseAsyncMetrics_FilesystemMainPathAvailableBytes",
          f"the disk alert's metricRef {alert['metricRef']!r} names the metric this chart scrapes")
    for child in (False, True):
        run(root, child)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
