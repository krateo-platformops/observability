#!/usr/bin/env python3
"""The child collector's rendered config passes the pinned image's own `validate`.

`helm template` renders a config the collector would refuse perfectly cleanly (the repo's history:
otel-collector-deployment 0.3.9 crash-looped on a component the image lacked). hack/check-collector-components.py
asks whether each component exists; this asks the collector itself whether the whole config decodes,
which also catches a key a receiver does not know. It found one while this was written: Prometheus's
kubernetes discovery takes `namespaces.own_namespace`, not `own_namespaces`, and the receiver's
config check names the line.

`validate` needs no Kubernetes API and starts nothing, so it runs on the child config as rendered
(the child has no host path for the daemonset's receivers to stat). The daemonset's config is not run
here: its filelog, hostmetrics and file_storage check that /hostfs and /var/lib/otelcol exist.

Controls, which must FAIL validation, so a pass is not a collector that checks nothing:
  - the child config with the unknown key own_namespaces in prometheus/clickhouse's discovery;
  - the child config with a receiver the image does not have.

Run: python3 tests/collector-child-validate-docker.py [repo-root]
Needs docker (the image the chart pins, already pulled or pullable) and helm. Exit 0 = all passed.
"""
import copy
import pathlib
import subprocess
import sys
import tempfile
import os

import yaml

CHART = "charts/otel-collector-daemonset"
CHILD = "tests/fixtures/collector-child-values.yaml"
# The environment the pod gives the collector (env of the rendered Deployment, minus the secrets).
ENV = {"MY_POD_IP": "127.0.0.1", "CH_USERNAME": "u", "CH_PASSWORD": "p", "OTEL_K8S_NODE_NAME": "n",
       "OTEL_K8S_NODE_IP": "10.0.0.1", "OTEL_K8S_NAMESPACE": "krateo-system", "OTEL_K8S_POD_NAME": "p",
       "OTEL_K8S_POD_IP": "10.0.0.2"}
failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


def validate(image, relay):
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o755)
        f = pathlib.Path(d) / "relay.yaml"
        f.write_text(yaml.safe_dump(relay))
        os.chmod(f, 0o644)
        env = [x for k, v in ENV.items() for x in ("-e", f"{k}={v}")]
        r = subprocess.run(["docker", "run", "--rm", *env, "-v", f"{d}:/v:ro", image, "validate", "--config=/v/relay.yaml"],
                           capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    out = subprocess.run(["helm", "template", "otel-collector-daemonset", str(root / CHART), "-n", "krateo-system",
                          "-f", str(root / CHILD)], check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    dep = next(d for d in docs if d["kind"] == "Deployment")
    image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
    relay = yaml.safe_load(cm["data"]["relay"])
    print(f"info image {image}; {len(relay['receivers'])} receivers, {len(relay['processors'])} processors, "
          f"{len(relay['exporters'])} exporters, {len(relay['service']['pipelines'])} pipelines")

    rc, text = validate(image, relay)
    check(rc == 0, f"the child config validates ({text[-300:] or 'no output'})")

    bad = copy.deepcopy(relay)
    sd = bad["receivers"]["prometheus/clickhouse"]["config"]["scrape_configs"][0]["kubernetes_sd_configs"][0]
    sd["namespaces"] = {"own_namespaces": True}
    rc, text = validate(image, bad)
    check(rc != 0 and "own_namespaces" in text, f"control: an unknown discovery key is refused ({text[-200:] or 'no output'})")

    bad = copy.deepcopy(relay)
    bad["receivers"]["nosuchreceiver"] = {}
    bad["service"]["pipelines"]["metrics"]["receivers"].append("nosuchreceiver")
    rc, text = validate(image, bad)
    check(rc != 0 and "nosuchreceiver" in text, f"control: a receiver the image lacks is refused ({text[-200:] or 'no output'})")
    print(f"\n{len(failures)} check(s) failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
