#!/usr/bin/env python3
"""End-to-end test of trace routing from the node agents to the tail sampler (observability#118).

Tail sampling is only correct if every span of a trace reaches the same sampler replica, whichever
node produced it. This runs, on one Docker network:
  - two stand-in node agents, each with otel-collector-daemonset's RENDERED `loadbalancing`
    exporter, hostname untouched;
  - two tail-sampler replicas from otel-collector-tailsampling's rendered config, both answering
    that hostname as a network alias, which is what the headless Service's DNS does in a cluster.
Each trace's gateway span goes to agent A and its snowplow span to agent B. The test checks that
every kept trace lands complete on exactly one replica, that error traces are all kept, and that
plain traces are kept at about 5%.

Run: python3 tests/tailsampling-route-docker.py [repo-root]
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import yaml

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ts", HERE / "tailsampling-docker.py")
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)

DAEMONSET = "charts/otel-collector-daemonset"


def rendered_relay(root, chart, kind):
    subprocess.run(["helm", "dependency", "build", str(root / chart)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "gate", str(root / chart), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    wl = next(d for d in docs if d["kind"] == kind)
    return yaml.safe_load(cm["data"]["relay"]), wl["spec"]["template"]["spec"]["containers"][0]["image"]


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def write_cfg(work, name, relay):
    d = work / name
    d.mkdir()
    os.chmod(d, 0o755)
    (d / "out").mkdir()
    os.chmod(d / "out", 0o777)
    (d / "relay.yaml").write_text(yaml.safe_dump(relay))
    os.chmod(d / "relay.yaml", 0o644)
    return d


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    ds, ds_image = rendered_relay(root, DAEMONSET, "DaemonSet")
    lb = ds["exporters"]["loadbalancing"]
    assert ds["service"]["pipelines"]["traces"]["exporters"] == ["loadbalancing"], \
        "the daemonset's traces pipeline must export through loadbalancing"
    assert "span_metrics" in ds["service"]["pipelines"]["traces/slo"]["exporters"], \
        "the SLI pipeline must stay on span_metrics, unsampled"
    hostname = lb["resolver"]["dns"]["hostname"]

    tier, tier_image = ts.render(root)
    tier = ts.test_config(tier)
    tier["receivers"] = {"otlp": {"protocols": {"grpc": {"endpoint": "0.0.0.0:4317"}}}}

    agent = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "exporters": {"loadbalancing": lb},
        "service": {"pipelines": {"traces": {"receivers": ["otlp"], "exporters": ["loadbalancing"]}}},
    }

    work = pathlib.Path(tempfile.mkdtemp(prefix="tsroute-"))
    os.chmod(work, 0o755)
    net = f"tsroute-{os.getpid()}"
    docker("network", "create", net)
    names = []
    try:
        replicas = []
        for i in (1, 2):
            d = write_cfg(work, f"tier{i}", tier)
            n = f"{net}-tier{i}"
            docker("run", "-d", "--name", n, "--network", net, "--network-alias", hostname,
                   "-e", "MY_POD_IP=0.0.0.0", "-v", f"{d}:/cfg:ro", "-v", f"{d / 'out'}:/out",
                   tier_image, "--config=/cfg/relay.yaml")
            names.append(n)
            replicas.append(d / "out")
        ports = []
        for i in ("a", "b"):
            d = write_cfg(work, f"agent{i}", agent)
            p = ts.free_port()
            n = f"{net}-agent{i}"
            docker("run", "-d", "--name", n, "--network", net, "-p", f"127.0.0.1:{p}:4318",
                   "-e", "MY_POD_IP=0.0.0.0", "-v", f"{d}:/cfg:ro", ds_image, "--config=/cfg/relay.yaml")
            names.append(n)
            ports.append(p)

        for p in ports:
            for _ in range(60):
                try:
                    ts.post(p, [])
                    break
                except Exception:
                    time.sleep(1)
            else:
                for n in names:
                    print(n, docker("logs", n, check=False).stderr[-1500:])
                print("FAIL an agent never accepted OTLP/HTTP")
                return 1
        time.sleep(3)  # let the agents resolve the replicas

        err_ids, ok_ids = [], []
        for i in range(60):
            t = ts.tid()
            err_ids.append(t)
            gw, sp = ts.portal_trace(t, gw_status="500", sp_status=500, status=2)
            ts.post(ports[0], [gw])
            ts.post(ports[1], [sp])
        for i in range(400):
            t = ts.tid()
            ok_ids.append(t)
            gw, sp = ts.portal_trace(t)
            ts.post(ports[0], [gw])
            ts.post(ports[1], [sp])
        time.sleep(ts.DECISION_WAIT_S + 8)
        per_replica = [ts.kept_traces(r) for r in replicas]
    finally:
        for n in names:
            docker("rm", "-f", n, check=False)
        docker("network", "rm", net, check=False)

    failed = False
    where = {}
    for idx, kept in enumerate(per_replica):
        for t, spans in kept.items():
            where.setdefault(t, []).append((idx, {svc for svc, _ in spans}))
    split = [t for t, w in where.items() if len(w) > 1]
    incomplete = [t for t, w in where.items()
                  if not any({"krateo-agent-gateway-platform", "snowplow"} <= svcs for _, svcs in w)]
    used = [len(k) for k in per_replica]
    print(f"{'ok  ' if all(used) else 'FAIL'} both replicas received traces: {used}")
    failed |= not all(used)
    print(f"{'ok  ' if not split else 'FAIL'} traces split across replicas: {len(split)}")
    failed |= bool(split)
    print(f"{'ok  ' if not incomplete else 'FAIL'} kept traces missing a span: {len(incomplete)}")
    failed |= bool(incomplete)
    n_err = sum(1 for t in err_ids if t in where)
    print(f"{'ok  ' if n_err == len(err_ids) else 'FAIL'} error traces kept: {n_err}/{len(err_ids)}")
    failed |= n_err != len(err_ids)
    n_ok = sum(1 for t in ok_ids if t in where)
    within = 6 <= n_ok <= 38
    print(f"{'ok  ' if within else 'FAIL'} plain traces kept: {n_ok}/{len(ok_ids)}, expected about 5%")
    failed |= not within
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
