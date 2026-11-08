#!/usr/bin/env python3
"""Gate: every component a collector chart configures must exist in the image it pins.

`helm template` renders a config naming a component the image was not built with as perfectly
valid. Only the collector knows its own component set, and it checks at startup, so the failure
lands on a cluster. That is how otel-collector-deployment 0.3.9 crash-looped on krateo-057:

    error decoding 'processors': unknown type: "transform" for id: "transform"

This asks the pinned image what it contains (`components`) and compares that with the rendered
config. It does not use `validate`, which also needs a live Kubernetes API.

Names are compared without underscores, because the image lists `file_log` while the config uses
the accepted older spelling `filelog`.

Run: python3 hack/check-collector-components.py [repo-root] [--image CHART=IMAGE]
Exit 0 = every chart's components exist in its image, 1 = at least one does not.
"""

import pathlib
import subprocess
import sys

import yaml

CHARTS = {
    "charts/otel-collector-deployment": "Deployment",
    "charts/otel-collector-daemonset": "DaemonSet",
    "charts/otel-collector-tailsampling": "Deployment",
}
SECTIONS = ("receivers", "processors", "exporters", "connectors", "extensions")


def normalise(name):
    return name.replace("_", "")


def render(root, chart, workload_kind):
    """Return (relay config, pinned image) from the chart as it would really install."""
    path = root / chart
    subprocess.run(["helm", "dependency", "build", str(path)], check=True, capture_output=True)
    out = subprocess.run(
        ["helm", "template", "gate", str(path), "-n", "krateo-system"],
        check=True, capture_output=True, text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    config = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    workload = next(d for d in docs if d["kind"] == workload_kind)
    image = workload["spec"]["template"]["spec"]["containers"][0]["image"]
    return yaml.safe_load(config["data"]["relay"]), image


def image_components(image):
    out = subprocess.run(
        ["docker", "run", "--rm", image, "components"],
        check=True, capture_output=True, text=True,
    ).stdout
    listed = yaml.safe_load(out)
    return {s: {normalise(c["name"]) for c in listed.get(s) or []} for s in SECTIONS}


def missing(relay, available):
    """Every configured component the image lacks. A `name/suffix` id has type `name`."""
    gaps = []
    for section in SECTIONS:
        for component_id in relay.get(section) or {}:
            kind = component_id.split("/")[0]
            if normalise(kind) not in available[section]:
                gaps.append(f"{section}: {component_id}")
    return gaps


def main():
    args = sys.argv[1:]
    overrides = {}
    while "--image" in args:
        i = args.index("--image")
        chart, image = args[i + 1].split("=", 1)
        overrides[chart] = image
        del args[i : i + 2]
    root = pathlib.Path(args[0] if args else ".").resolve()

    failed = False
    for chart, workload_kind in CHARTS.items():
        relay, image = render(root, chart, workload_kind)
        image = overrides.get(chart, image)
        gaps = missing(relay, image_components(image))
        if gaps:
            failed = True
            print(f"FAIL {chart}: {image} does not contain:")
            for gap in gaps:
                print(f"  {gap}")
        else:
            print(f"ok   {chart}: every component exists in {image}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
