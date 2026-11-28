#!/usr/bin/env python3
"""otel-collector-daemonset renders as a child collector in deployment mode, and refuses a bad one.

A vcluster child enforces the baseline Pod Security Standard, which refuses hostPath volumes,
hostPorts and host namespaces. Child mode (opentelemetry-collector.mode: deployment, the values in
tests/fixtures/collector-child-values.yaml) must therefore render nothing that standard refuses,
without losing what a child needs: the OTLP receivers and the audit pipeline that creates
krateo_audit. `helm template` is the only instrument here (no cluster, no image); the admission
itself is held by tests/collector-child-pss-kind.py.

Checked, on the child render:
  1. a Deployment, no DaemonSet;
  2. across every pod spec of the render: no hostPath volume, hostPort, hostNetwork, hostPID,
     hostIPC, privileged container, or capability beyond a drop. The number of pod specs examined
     is asserted (a pass over zero pod specs proves nothing);
  3. the collector config has the otlp receiver on 4317 and 4318, pipeline logs/audit feeding exporter
     clickhouse/audit (table krateo_audit), the redaction `transform` on every log pipeline, and
     no filelog, hostmetrics or kubeletstats receiver, no pipeline without a receiver, no pipeline
     naming an undeclared receiver, no ${env:K8S_NODE_NAME}, and no node filter on k8sattributes;
  4. the <release>-local Service selects the Deployment's pods (every selector label is a label of
     the pod template) and has internalTrafficPolicy Cluster; the ClusterRole has no nodes/proxy;
  5. with auditCollector on, its Deployment is also free of all of the above (a second pod spec).
Refused (each must fail to render, naming the preset): mode deployment with logsCollection on,
hostMetrics on or kubeletMetrics on, each alone, and with the chart's own defaults.
And the daemonset default render stays what it was: a DaemonSet, no Deployment, four hostPath
volumes, the Service on agent-collector with internalTrafficPolicy Local, filelog and the node
selector in place, and none of the child-only entries.

Run: python3 tests/collector-child-render.py [repo-root]
Needs helm and PyYAML; the chart's dependency must be built. Exit 0 = every check passed.
"""
import json
import pathlib
import subprocess
import sys

import yaml

CHART = "charts/otel-collector-daemonset"
CHILD = "tests/fixtures/collector-child-values.yaml"
POD_SPEC_KINDS = {"Pod": ("spec",), "Deployment": ("spec", "template", "spec"),
                  "DaemonSet": ("spec", "template", "spec"), "StatefulSet": ("spec", "template", "spec"),
                  "ReplicaSet": ("spec", "template", "spec"), "Job": ("spec", "template", "spec"),
                  "CronJob": ("spec", "jobTemplate", "spec", "template", "spec")}
# The denominators. Pod specs examined: the child render has one (the collector's Deployment), and
# one more with auditCollector on. Measured 2026-11-28; the floor is the count itself, because a
# render that loses its Deployment must fail, not pass over nothing.
MIN_POD_SPECS_CHILD = 1
MIN_POD_SPECS_CHILD_WITH_AUDIT = 2
# Four hostPath volumes in the daemonset render (/var/log/pods, /var/lib/otelcol, /var/lib/docker/containers,
# and the hostfs root), measured 2026-11-28. If this drops the control below stops proving anything.
MIN_DAEMONSET_HOSTPATHS = 4
HOST_PRESETS = ("logsCollection", "hostMetrics", "kubeletMetrics")

failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


def helm_template(root, *args):
    r = subprocess.run(["helm", "template", "otel-collector-daemonset", str(root / CHART), "-n", "krateo-system", *args],
                       capture_output=True, text=True)
    return r


def docs_of(r):
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def pod_specs(docs):
    found = []
    for d in docs:
        path = POD_SPEC_KINDS.get(d["kind"])
        if not path:
            continue
        node = d
        for key in path:
            node = (node or {}).get(key)
        if node:
            found.append((f"{d['kind']}/{d['metadata']['name']}", node, d))
    return found


def host_access(spec):
    """Everything the baseline Pod Security Standard refuses that a pod spec here could carry."""
    bad = []
    for v in spec.get("volumes") or []:
        if "hostPath" in v:
            bad.append(f"hostPath volume {v['name']} ({v['hostPath'].get('path')})")
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(key):
            bad.append(key)
    for c in [*(spec.get("containers") or []), *(spec.get("initContainers") or [])]:
        for p in c.get("ports") or []:
            if p.get("hostPort"):
                bad.append(f"hostPort {p['hostPort']} on {c['name']}")
        sc = c.get("securityContext") or {}
        if sc.get("privileged"):
            bad.append(f"privileged container {c['name']}")
        if (sc.get("capabilities") or {}).get("add"):
            bad.append(f"added capabilities on {c['name']}: {sc['capabilities']['add']}")
    return bad


def relay(docs, workload_name):
    wl = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == workload_name)
    spec = wl["spec"]["template"]["spec"]
    cm_name = next(v["configMap"]["name"] for v in spec["volumes"]
                   if "configMap" in v and any(i.get("key") == "relay" for i in v["configMap"].get("items") or []))
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == cm_name)
    return yaml.safe_load(cm["data"]["relay"]), cm["data"]["relay"]


def child_render(root):
    r = helm_template(root, "-f", str(root / CHILD))
    check(r.returncode == 0, f"the child values render ({(r.stderr.strip().splitlines() or ['ok'])[-1][:200]})")
    return docs_of(r) if r.returncode == 0 else []


def test_child(root):
    docs = child_render(root)
    kinds = [d["kind"] for d in docs]
    check("Deployment" in kinds and "DaemonSet" not in kinds, f"child: a Deployment and no DaemonSet ({sorted(set(kinds))})")
    specs = pod_specs(docs)
    check(len(specs) >= MIN_POD_SPECS_CHILD, f"child: pod specs examined {len(specs)} >= {MIN_POD_SPECS_CHILD}")
    for name, spec, _ in specs:
        bad = host_access(spec)
        check(not bad, f"child: {name} asks for no host access {bad or ''}")
    text = "\n".join(yaml.safe_dump(d) for d in docs)
    check("hostPath" not in text and "hostPort" not in text, "child: the words hostPath and hostPort appear nowhere in the render")

    name = "otel-collector-daemonset-opentelemetry-collector"
    cfg, raw = relay(docs, name)
    rec, pipes = cfg["receivers"], cfg["service"]["pipelines"]
    check(set(rec["otlp"]["protocols"]) == {"grpc", "http"}, "child: the otlp receiver serves grpc and http")
    for rid in ("filelog", "hostmetrics", "kubeletstats"):
        check(rid not in rec and not any(rid in (p.get("receivers") or []) for p in pipes.values()),
              f"child: no {rid} receiver, declared or named")
    check("logs" not in pipes, "child: the pipeline only filelog fed is gone")
    check(all(p.get("receivers") for p in pipes.values()), f"child: every pipeline has a receiver ({sorted(pipes)})")
    named = {r for p in pipes.values() for r in p["receivers"]}
    check(named <= set(rec) | set(cfg.get("connectors") or {}), "child: every receiver a pipeline names is declared")
    check(set(rec) <= named, f"child: every declared receiver is named by a pipeline (unused: {sorted(set(rec) - named)})")
    check("${env:K8S_NODE_NAME}" not in raw, "child: no ${env:K8S_NODE_NAME} anywhere in the collector config")
    check("node_from_env_var" not in raw, "child: no node filter on k8sattributes")
    check(cfg["exporters"].get("clickhouse/audit", {}).get("logs_table_name") == "krateo_audit",
          "child: exporter clickhouse/audit writes krateo_audit")
    audit = pipes.get("logs/audit", {})
    check(audit.get("receivers") == ["otlp"] and audit.get("exporters") == ["clickhouse/audit"],
          "child: pipeline logs/audit takes otlp and feeds clickhouse/audit")
    for pname, p in pipes.items():
        if pname.startswith("logs") and "clickhouse" in " ".join(p.get("exporters") or []):
            check("transform" in p["processors"], f"child: {pname} runs the JWT redaction (transform)")
    otlp_pods = pipes.get("logs/otlp-pods", {})
    check(otlp_pods.get("receivers") == ["otlp"] and otlp_pods.get("exporters") == ["clickhouse"]
          and "filter/otlp-pods" in cfg["processors"], "child: logs/otlp-pods carries an app's other OTLP logs to otel_logs")
    check("clickhouse" in rec["prometheus/clickhouse"]["config"]["scrape_configs"][0]["job_name"]
          and "field" not in rec["prometheus/clickhouse"]["config"]["scrape_configs"][0]["kubernetes_sd_configs"][0]["selectors"][0],
          "child: the ClickHouse disk scrape is kept, without a node selector")

    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)
    pod_labels = dep["spec"]["template"]["metadata"]["labels"]
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "otel-collector-daemonset-local")
    missing = {k: v for k, v in svc["spec"]["selector"].items() if pod_labels.get(k) != v}
    check(not missing, f"child: the -local Service selector matches the Deployment's pod labels (unmatched: {missing})")
    check(svc["spec"].get("internalTrafficPolicy") == "Cluster", "child: the -local Service has internalTrafficPolicy Cluster")
    headless = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == name)
    missing = {k: v for k, v in headless["spec"]["selector"].items() if pod_labels.get(k) != v}
    check(not missing, f"child: the collector's own Service selects the Deployment too (unmatched: {missing})")
    role = next(d for d in docs if d["kind"] == "ClusterRole")
    check(not any("nodes/proxy" in (r.get("resources") or []) for r in role["rules"]), "child: the ClusterRole has no nodes/proxy")
    check(not any("nodes" in (r.get("resources") or []) for r in role["rules"]), "child: the ClusterRole has no access to nodes")

    # The audit collector, a second Deployment beside it.
    r = helm_template(root, "-f", str(root / CHILD), "--set", "auditCollector.enabled=true")
    check(r.returncode == 0, f"child with auditCollector renders ({(r.stderr.strip().splitlines() or ['ok'])[-1][:200]})")
    if r.returncode == 0:
        specs = pod_specs(docs_of(r))
        check(len(specs) >= MIN_POD_SPECS_CHILD_WITH_AUDIT,
              f"child with auditCollector: pod specs examined {len(specs)} >= {MIN_POD_SPECS_CHILD_WITH_AUDIT}")
        for sname, spec, _ in specs:
            bad = host_access(spec)
            check(not bad, f"child with auditCollector: {sname} asks for no host access {bad or ''}")


def test_refusals(root):
    """Each must fail to render, with a message that names the preset. A run that renders, or fails
    for another reason, is a failed check."""
    cases = [(f"{p} on", ["--set", f"opentelemetry-collector.presets.{p}.enabled=true"], [p]) for p in HOST_PRESETS]
    cases.append(("all three on (the chart's defaults, only the mode set)", None, list(HOST_PRESETS)))
    refused = 0
    for label, extra, names in cases:
        if extra is None:
            r = helm_template(root, "--set", "opentelemetry-collector.mode=deployment")
        else:
            r = helm_template(root, "-f", str(root / CHILD), *extra)
        err = r.stderr
        ok = r.returncode != 0 and "mode is deployment" in err and all(n in err.split("Set opentelemetry-collector")[0] for n in names)
        refused += ok
        check(ok, f"refused: mode deployment with {label} fails to render, naming {', '.join(names)}"
                  f"{'' if ok else ' (rc=%d: %s)' % (r.returncode, (err.strip().splitlines() or ['no stderr'])[0][:160])}")
    check(refused == len(cases), f"refusal fixtures: {refused} of {len(cases)} refused")
    # The positive control: with all three off the same chart renders, so the refusals above are
    # about the presets and not about something else in the command line.
    check(helm_template(root, "-f", str(root / CHILD)).returncode == 0, "control: the same values with the presets off render")


def test_daemonset(root):
    r = helm_template(root)
    check(r.returncode == 0, "daemonset default renders")
    docs = docs_of(r)
    kinds = [d["kind"] for d in docs]
    check("DaemonSet" in kinds and "Deployment" not in kinds, f"daemonset: a DaemonSet and no Deployment ({sorted(set(kinds))})")
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    spec = ds["spec"]["template"]["spec"]
    paths = [v["hostPath"]["path"] for v in spec["volumes"] if "hostPath" in v]
    check(len(paths) >= MIN_DAEMONSET_HOSTPATHS, f"daemonset: {len(paths)} hostPath volumes >= {MIN_DAEMONSET_HOSTPATHS} ({paths})")
    check(any(p.get("hostPort") for c in spec["containers"] for p in c.get("ports") or []), "daemonset: the OTLP hostPorts are kept")
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "otel-collector-daemonset-local")
    check(svc["spec"]["internalTrafficPolicy"] == "Local" and svc["spec"]["selector"]["component"] == "agent-collector",
          "daemonset: the -local Service is node-local on agent-collector")
    pod_labels = ds["spec"]["template"]["metadata"]["labels"]
    check(all(pod_labels.get(k) == v for k, v in svc["spec"]["selector"].items()), "daemonset: the -local selector matches the agent pods")
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    cfg, raw = yaml.safe_load(cm["data"]["relay"]), cm["data"]["relay"]
    check({"filelog", "hostmetrics", "kubeletstats"} <= set(cfg["receivers"]), "daemonset: filelog, hostmetrics and kubeletstats are declared")
    check("logs" in cfg["service"]["pipelines"], "daemonset: the filelog pipeline is there")
    check("logs/otlp-pods" not in cfg["service"]["pipelines"] and "filter/otlp-pods" not in cfg["processors"],
          "daemonset: none of the child-only entries")
    jobs = [cfg["receivers"][r]["config"]["scrape_configs"][0]["kubernetes_sd_configs"][0]["selectors"][0]
            for r in ("prometheus/agentgateway", "prometheus/clickhouse")]
    check(all(j["field"] == "spec.nodeName=${env:K8S_NODE_NAME}" for j in jobs),
          "daemonset: both scrape jobs pin the agent's own node")
    check(raw.count("node_from_env_var") == 1, "daemonset: k8sattributes keeps its node filter")


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    test_child(root)
    test_refusals(root)
    test_daemonset(root)
    print(f"\n{len(failures)} check(s) failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
