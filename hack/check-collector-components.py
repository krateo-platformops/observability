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

Three ways this gate could pass having checked nothing, each refused:
  - the chart list is derived from charts/otel-collector-*; if it differs from CHARTS below the
    gate fails, so a new collector chart forces a decision instead of going unchecked;
  - each chart must compare at least its MIN_COMPONENTS configured components, so a relay config
    with none of the five sections cannot print "ok";
  - tests/check-collector-components.py runs in the same workflow: an unknown component must exit
    1, an empty config must fail the floor, an unlisted chart must fail.

Run: python3 hack/check-collector-components.py [repo-root] [--image CHART=IMAGE]
                                                 [--components CHART=FILE]
  --components reads the image's `components` output from FILE instead of running docker. It
  exists for tests/check-collector-components.py only.
Exit 0 = every chart's components exist in its image, 1 = at least one does not (or the chart
list or a floor is wrong).
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
# The denominator: configured components compared per chart (receivers, processors, exporters,
# connectors and extensions in the rendered relay config). Measured 2026-11-28 with
# `helm template`: deployment 14, daemonset 28, tailsampling 7. Floor = measured minus ~20%,
# rounded down. Raise it when a config grows; lower it only with a reason.
MIN_COMPONENTS = {
    "charts/otel-collector-deployment": 11,
    "charts/otel-collector-daemonset": 22,
    "charts/otel-collector-tailsampling": 5,
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


def chart_list_errors(root):
    """The declared CHARTS must be exactly the charts/otel-collector-* directories."""
    found = {f"charts/{p.name}" for p in (root / "charts").glob("otel-collector-*") if p.is_dir()}
    errs = [f"{c} is not in CHARTS (and has no MIN_COMPONENTS): decide whether it is checked"
            for c in sorted(found - set(CHARTS))]
    errs += [f"{c} is in CHARTS but charts/ has no such directory" for c in sorted(set(CHARTS) - found)]
    errs += [f"{c} has no MIN_COMPONENTS entry" for c in sorted(set(CHARTS) - set(MIN_COMPONENTS))]
    return errs


def image_components(image, components_file=None):
    if components_file:
        out = pathlib.Path(components_file).read_text()
    else:
        out = subprocess.run(
            ["docker", "run", "--rm", image, "components"],
            check=True, capture_output=True, text=True,
        ).stdout
    listed = yaml.safe_load(out)
    return {s: {normalise(c["name"]) for c in listed.get(s) or []} for s in SECTIONS}


def compare(relay, available):
    """(gaps, compared): every configured component the image lacks, and how many were compared.
    A `name/suffix` id has type `name`."""
    gaps, compared = [], 0
    for section in SECTIONS:
        for component_id in relay.get(section) or {}:
            compared += 1
            kind = component_id.split("/")[0]
            if normalise(kind) not in available[section]:
                gaps.append(f"{section}: {component_id}")
    return gaps, compared


def missing(relay, available):
    return compare(relay, available)[0]


def main():
    args = sys.argv[1:]
    overrides, listings = {}, {}
    for flag, target in (("--image", overrides), ("--components", listings)):
        while flag in args:
            i = args.index(flag)
            chart, value = args[i + 1].split("=", 1)
            target[chart] = value
            del args[i : i + 2]
    root = pathlib.Path(args[0] if args else ".").resolve()

    errors = chart_list_errors(root)
    for err in errors:
        print(f"FAIL {err}")
    if errors:
        return 1  # nothing below can be trusted against a chart list that is wrong
    failed = False
    for chart, workload_kind in CHARTS.items():
        relay, image = render(root, chart, workload_kind)
        image = overrides.get(chart, image)
        gaps, compared = compare(relay, image_components(image, listings.get(chart)))
        floor = MIN_COMPONENTS.get(chart, 1)
        if compared < floor:
            failed = True
            print(f"FAIL {chart}: compared {compared} configured components, expected at least {floor}")
        if gaps:
            failed = True
            print(f"FAIL {chart}: {image} does not contain:")
            for gap in gaps:
                print(f"  {gap}")
        elif compared >= floor:
            print(f"ok   {chart}: all {compared} configured components exist in {image}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
