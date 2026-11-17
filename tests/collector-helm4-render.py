#!/usr/bin/env python3
"""The collector charts render the same under Helm 3 and Helm 4.

The platform renders charts with Helm 3 (3.20). Helm 4.2 and later drop a null in a parent chart's
own values.yaml before the subchart's defaults merge in (upstream Helm change 31979), so a null no
longer deletes the subchart's key. otel-collector-deployment 0.3.20 and otel-collector-tailsampling
0.1.15 left out upstream opentelemetry-collector's default jaeger, otlp, prometheus and zipkin
receivers and default pipelines that way, and under Helm 4.3.2 they came back into the collector
config. Since 0.3.21 / 0.1.16 each wrapper renders the collector's ConfigMap itself and no null is
left in its values.yaml; otel-collector-daemonset does the same since 0.1.34, to leave out
upstream's jaeger and zipkin receivers, and since 0.1.35 its prometheus self-scrape.

For each chart, with its default values and again with .github/ci/injected-global.yaml (the
`global` block the platform injects), this renders with both binaries and checks:
  1. both renders succeed;
  2. the two are the same object for object, field for field; the ConfigMap's `relay` key (the
     collector config) is compared as parsed YAML;
  3. no upstream default the chart does not run is in the collector config (FORBIDDEN below:
     upstream's jaeger/zipkin receivers, its prometheus self-scrape, and so on);
  4. the workload mounts a ConfigMap of the render that holds `relay`, with --config=/conf/relay.yaml;
  5. the pods roll on a config change: a pod-template checksum/config annotation is there, is not
     the hash of an empty template, and changes when one config value is overridden;
  6. ports and listeners match, both ways: every receiver a pipeline starts that listens has its
     container port, and a Service port when the chart renders a Service; and every rendered
     container port, hostPort and Service port has something listening behind it (a started
     receiver, or the health_check extension or the collector's own telemetry, which need no port
     but may have one). A receiver only declared under `receivers:` is never started, so it counts
     as no listener. A port with nothing behind it refuses every connection; a listener with no
     port is unreachable;
  7. nothing is declared that does not run, and nothing runs that is not declared: every receiver
     under `receivers:` is named by a pipeline and every extension under `extensions:` is in
     `service.extensions`, and the other way round. A declared receiver no pipeline names is never
     started, so it only reads as if it were.
Then, for each wrapper, with one of upstream's default receivers named by a pipeline and its config
left alone (NAMED_DEFAULTS below), under both binaries:
  8. the receiver is in the config with upstream's default value, and check 7 holds: a wrapper leaves
     a default receiver out only while no pipeline names it, so a pipeline never names a receiver
     that is not declared, which the collector refuses at startup.

Run: python3 tests/collector-helm4-render.py <repo-root> <helm3-binary> <helm4-binary>
Each chart's dependency must already be built (helm dependency build charts/<chart>).
Exit 0 = every check passed, 1 = at least one did not.
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys

import yaml

INJECTED = ".github/ci/injected-global.yaml"
NAMESPACE = "krateo-system"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
# Override used to prove the pods roll: one config value every chart has.
CHANGE = "opentelemetry-collector.config.processors.memory_limiter.check_interval=7s"

# chart -> (workload kind, {path into the collector config: why it must not be there}).
CHARTS = {
    "otel-collector-deployment": ("Deployment", {
        ("receivers", "jaeger"): "upstream default receiver",
        ("receivers", "zipkin"): "upstream default receiver",
        ("receivers", "otlp"): "upstream default receiver; this collector receives nothing",
        ("receivers", "prometheus"): "not compiled into the krateo collector image",
        ("service", "pipelines", "traces"): "upstream default pipeline",
    }),
    "otel-collector-tailsampling": ("Deployment", {
        ("receivers", "jaeger"): "upstream default receiver",
        ("receivers", "zipkin"): "upstream default receiver",
        ("receivers", "prometheus"): "upstream default self-scrape",
        ("receivers", "otlp", "protocols", "http"): "upstream default; the tier takes OTLP/gRPC only",
        ("service", "pipelines", "logs"): "upstream default pipeline",
        ("service", "pipelines", "metrics"): "upstream default pipeline",
    }),
    "otel-collector-daemonset": ("DaemonSet", {
        ("receivers", "jaeger"): "upstream default receiver; no pipeline runs it, its ports are closed",
        ("receivers", "zipkin"): "upstream default receiver; no pipeline runs it, its port is closed",
        ("receivers", "prometheus"): "upstream default self-scrape; no pipeline runs it",
    }),
}

# Upstream 0.170.0's default config of the receivers the wrappers leave out (upstream values.yaml,
# config.receivers at lines 236-260).
UPSTREAM_RECEIVER_DEFAULTS = {
    "jaeger": {"protocols": {"grpc": {"endpoint": "${env:MY_POD_IP}:14250"},
                             "thrift_http": {"endpoint": "${env:MY_POD_IP}:14268"},
                             "thrift_compact": {"endpoint": "${env:MY_POD_IP}:6831"}}},
    "otlp": {"protocols": {"grpc": {"endpoint": "${env:MY_POD_IP}:4317"},
                           "http": {"endpoint": "${env:MY_POD_IP}:4318"}}},
    "prometheus": {"config": {"scrape_configs": [{
        "job_name": "opentelemetry-collector", "scrape_interval": "10s",
        "static_configs": [{"targets": ["${env:MY_POD_IP}:8888"]}]}]}},
    "zipkin": {"endpoint": "${env:MY_POD_IP}:9411"},
}
# A wrapper leaves out an upstream default receiver only while no pipeline names it. Each case
# names one in a pipeline without setting its config, and the receiver must then be in the config
# with upstream's default, never left out (which would make the collector fail at startup on a
# pipeline naming an undeclared receiver). (chart, receiver, --set arguments)
NAMED_DEFAULTS = [
    ("otel-collector-daemonset", "prometheus",
     ["opentelemetry-collector.config.service.pipelines.metrics.receivers={hostmetrics,kubeletstats,otlp,prometheus}"]),
    ("otel-collector-deployment", "otlp",
     ["opentelemetry-collector.config.service.pipelines.traces.receivers={otlp}",
      "opentelemetry-collector.config.service.pipelines.traces.exporters={clickhouse}"]),
    ("otel-collector-tailsampling", "zipkin",
     ["opentelemetry-collector.config.service.pipelines.traces.receivers={otlp,zipkin}"]),
]

# Receiver types that listen, by protocol (the key under `protocols`, or None for a receiver with
# one endpoint): (default port, TCP or UDP). A configured endpoint's port overrides the default.
LISTENING = {
    "otlp": {"grpc": (4317, "TCP"), "http": (4318, "TCP")},
    "jaeger": {"grpc": (14250, "TCP"), "thrift_http": (14268, "TCP"),
               "thrift_compact": (6831, "UDP"), "thrift_binary": (6832, "UDP")},
    "zipkin": {None: (9411, "TCP")},
}
# Receiver types that open no port: they read the Kubernetes API, files or the host, or scrape.
# A pipeline receiver of a type in neither list fails the check until it is classified.
PASSIVE = {"k8sobjects", "k8s_cluster", "k8s_events", "filelog", "hostmetrics", "kubeletstats", "prometheus"}

failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


def version(helm):
    return subprocess.run([helm, "version", "--short"], check=True, capture_output=True, text=True).stdout.strip()


def render(helm, root, chart, args):
    r = subprocess.run([helm, "template", chart, str(root / "charts" / chart), "--namespace", NAMESPACE, *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        errors = [l for l in r.stderr.splitlines() if l.startswith("Error")]
        return None, (errors or r.stderr.strip().splitlines()[-1:] or ["(no stderr)"])[0][:300]
    docs = {}
    for d in yaml.safe_load_all(r.stdout):
        if not d:
            continue
        if d["kind"] == "ConfigMap" and "relay" in (d.get("data") or {}):
            d["data"]["relay"] = yaml.safe_load(d["data"]["relay"])
        docs[(d["kind"], d["metadata"].get("name"))] = d
    return docs, None


def leaves(node, path=""):
    if isinstance(node, dict):
        if not node:
            yield path, {}
        for k, v in node.items():
            yield from leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        if not node:
            yield path, []
        for i, v in enumerate(node):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, node


def flat(docs):
    return {(kind, name, path): v for (kind, name), d in docs.items() for path, v in leaves(d)}


def show(kind, v):
    return "<not shown>" if kind == "Secret" else json.dumps(v)[:120]


def workload(docs, kind):
    found = [d for (k, _), d in docs.items() if k == kind]
    return found[0] if len(found) == 1 else None


def relay_of(docs, kind):
    """The collector config the workload actually mounts, or (None, reason)."""
    wl = workload(docs, kind)
    if wl is None:
        return None, f"no single {kind}"
    spec = wl["spec"]["template"]["spec"]
    args = spec["containers"][0].get("args") or []
    if "--config=/conf/relay.yaml" not in args:
        return None, f"container args {args} lack --config=/conf/relay.yaml"
    names = [v["configMap"]["name"] for v in spec.get("volumes") or []
             if "configMap" in v and any(i.get("key") == "relay" for i in v["configMap"].get("items") or [])]
    if len(names) != 1:
        return None, f"volumes mount {len(names)} ConfigMap(s) with key relay"
    cm = docs.get(("ConfigMap", names[0]))
    if cm is None or not isinstance((cm.get("data") or {}).get("relay"), dict):
        return None, f"mounted ConfigMap {names[0]} is not in the render with a relay config"
    return cm["data"]["relay"], names[0]


def dig(node, path):
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def port_of(endpoint, default):
    m = re.search(r":(\d+)$", str(endpoint or ""))
    return int(m.group(1)) if m else default


def listeners(relay):
    """(required, optional, problems), as sets of (port, TCP or UDP). required: what the receivers
    named in a pipeline listen on. optional: the health_check extension (the kubelet probes it by
    number) and the collector's own telemetry reader, which need no declared port."""
    required, optional, problems = set(), set(), []
    receivers = relay.get("receivers") or {}
    connectors = relay.get("connectors") or {}
    service = relay.get("service") or {}
    started = {r for p in (service.get("pipelines") or {}).values() for r in p.get("receivers") or []}
    for rid in sorted(started):
        rtype = rid.split("/")[0]
        if rid in connectors or rtype in PASSIVE:
            continue
        if rtype not in LISTENING:
            problems.append(f"pipeline receiver {rid} is of a type this test does not know; "
                            f"add it to LISTENING or PASSIVE")
            continue
        conf = receivers.get(rid) or {}
        protocols = conf.get("protocols") or {}
        for proto, (default, l4) in LISTENING[rtype].items():
            if proto is None:
                required.add((port_of(conf.get("endpoint"), default), l4))
            elif proto in protocols:
                required.add((port_of((protocols[proto] or {}).get("endpoint"), default), l4))
    for ext in service.get("extensions") or []:
        port = port_of(((relay.get("extensions") or {}).get(ext) or {}).get("endpoint"), None)
        if port:
            optional.add((port, "TCP"))
    for reader in ((service.get("telemetry") or {}).get("metrics") or {}).get("readers") or []:
        prom = ((reader.get("pull") or {}).get("exporter") or {}).get("prometheus") or {}
        if prom.get("port"):
            optional.add((int(prom["port"]), "TCP"))
    return required, optional, problems


def rendered_ports(docs, kind):
    """({(port, protocol): [where]}, {Service name: {(target port, protocol)}}): the workload's container ports
    and hostPorts, and every rendered Service's target ports."""
    wl = workload(docs, kind)
    containers = (wl or {}).get("spec", {}).get("template", {}).get("spec", {}).get("containers") or [{}]
    ports = containers[0].get("ports") or []
    by_name = {p.get("name"): p["containerPort"] for p in ports}
    found = {}
    for p in ports:
        key = (p["containerPort"], p.get("protocol", "TCP"))
        found.setdefault(key, []).append(f"containerPort {p.get('name')}")
        if p.get("hostPort"):
            found[key].append(f"hostPort {p['hostPort']}")
    services = {}
    for (k, name), d in docs.items():
        if k != "Service":
            continue
        services[name] = set()
        for p in d["spec"].get("ports") or []:
            target = p.get("targetPort", p["port"])
            key = (by_name.get(target, target), p.get("protocol", "TCP"))
            services[name].add(key)
            found.setdefault(key, []).append(f"Service {name} port {p.get('name')}")
    return found, services


def check_ports(tag, relay, docs, kind):
    required, optional, problems = listeners(relay)
    for problem in problems:
        check(False, f"{tag}: {problem}")
    found, services = rendered_ports(docs, kind)
    dead = {key: where for key, where in found.items() if key not in required | optional}
    for (port, l4), where in sorted(dead.items()):
        print(f"      no listener on {port}/{l4}: {', '.join(where)}")
    check(not dead, f"{tag}: every rendered port has a started listener ({len(dead)} without)")
    container = {key for key, where in found.items() if any(w.startswith("containerPort") for w in where)}
    no_port = sorted(required - container)
    no_service = sorted(required - set().union(*services.values())) if services else []
    check(not no_port and not no_service,
          f"{tag}: every started listener has its port (listeners {sorted(required)}; "
          f"without a container port {no_port}; on no Service {no_service})")


def check_declared_used(tag, relay):
    service = relay.get("service") or {}
    declared = set(relay.get("receivers") or {})
    connectors = set(relay.get("connectors") or {})
    named = {r for p in (service.get("pipelines") or {}).values() for r in p.get("receivers") or []}
    unused, undeclared = sorted(declared - named), sorted(named - declared - connectors)
    check(not unused and not undeclared,
          f"{tag}: every declared receiver is in a pipeline and every pipeline receiver is declared "
          f"(in no pipeline {unused}; not declared {undeclared})")
    ext_declared = set(relay.get("extensions") or {})
    ext_named = set(service.get("extensions") or [])
    check(ext_declared == ext_named,
          f"{tag}: every declared extension is in service.extensions and the other way round "
          f"(not enabled {sorted(ext_declared - ext_named)}; not declared {sorted(ext_named - ext_declared)})")


def checksum(docs, kind):
    wl = workload(docs, kind)
    return ((wl or {}).get("spec", {}).get("template", {}).get("metadata", {}).get("annotations") or {}).get("checksum/config")


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    root, h3, h4 = pathlib.Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    v3, v4 = version(h3), version(h4)
    check(v3.startswith("v3."), f"first binary is Helm 3 ({v3})")
    check(v4.startswith("v4."), f"second binary is Helm 4 ({v4})")
    for chart, (kind, forbidden) in CHARTS.items():
        for label, args in (("defaults", []), ("injected global", ["-f", str(root / INJECTED)])):
            tag = f"{chart}, {label}"
            renders = {}
            for v, helm in ((v3, h3), (v4, h4)):
                docs, err = render(helm, root, chart, args)
                check(docs is not None, f"{tag}: renders under {v} ({err or 'ok'})")
                if docs is None:
                    continue
                renders[v] = docs
                relay, where = relay_of(docs, kind)
                check(relay is not None, f"{tag}, {v}: {kind} mounts the rendered collector config ({where})")
                for path, why in forbidden.items():
                    present = relay is not None and dig(relay, path) is not None
                    check(not present, f"{tag}, {v}: no {'.'.join(path)} in the collector config ({why})")
                if relay is not None:
                    check_ports(f"{tag}, {v}", relay, docs, kind)
                    check_declared_used(f"{tag}, {v}", relay)
                if forbidden:
                    before = checksum(docs, kind)
                    changed, err = render(helm, root, chart, [*args, "--set", CHANGE])
                    after = checksum(changed, kind) if changed else None
                    check(bool(before) and before != EMPTY_SHA and changed is not None and after != before,
                          f"{tag}, {v}: checksum/config hashes the config and changes with it "
                          f"({(before or 'absent')[:12]} -> {(after or err or 'absent')[:12]})")
            if len(renders) != 2:
                continue
            fa, fb = flat(renders[v3]), flat(renders[v4])
            unexpected = [f"{k[0]}/{k[1]} {k[2]}: {v3}={show(k[0], fa.get(k, '<absent>'))} "
                          f"{v4}={show(k[0], fb.get(k, '<absent>'))}"
                          for k in sorted(set(fa) | set(fb), key=str) if fa.get(k, object()) != fb.get(k, object())]
            for line in unexpected[:40]:
                print(f"      {line}")
            check(not unexpected, f"{tag}: {v3} and {v4} render the same ({len(unexpected)} difference(s))")
    for chart, receiver, args in NAMED_DEFAULTS:
        kind = CHARTS[chart][0]
        tag = f"{chart}, {receiver} named by a pipeline, its config left at upstream's default"
        relays = {}
        for v, helm in ((v3, h3), (v4, h4)):
            sets = [x for a in args for x in ("--set", a)]
            docs, err = render(helm, root, chart, sets)
            relay, where = relay_of(docs, kind) if docs else (None, err)
            check(relay is not None, f"{tag}, {v}: renders and mounts its config ({where})")
            if relay is None:
                continue
            relays[v] = relay
            got = dig(relay, ("receivers", receiver))
            check(got == UPSTREAM_RECEIVER_DEFAULTS[receiver],
                  f"{tag}, {v}: receivers.{receiver} is kept, with upstream's default "
                  f"({'kept' if got is not None else 'left out'})")
            check_declared_used(f"{tag}, {v}", relay)
        if len(relays) == 2:
            check(relays[v3] == relays[v4], f"{tag}: {v3} and {v4} render the same collector config")
    print(f"\n{len(failures)} check(s) failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
