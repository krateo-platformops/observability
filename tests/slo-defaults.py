#!/usr/bin/env python3
"""The SLO defaults (0.1.52): the chart's cmp-slo-* Alerts and slo-span-* Metrics equal the CMP SLO generator's.

The five SLO objects were proven on krateo-057 on 2026-11-10, applied there from the CMP team's SLO
generator. From 0.1.52 the chart ships them, under the same names so the live CRs (and the HyperDX
alerts alert-provider keeps their ids for) are adopted, not recreated. This test keeps the two sources
from drifting apart:

1. Fixture. tests/fixtures/slo-objects-render.yaml is the generator's output at the commit its header
   names. Every object in it must render from the chart with the same kind, apiVersion, name and
   namespace, and a spec EQUAL to the fixture's: same keys, same values, same JSON types (500 an
   integer, 0.001 a number, "true" a string). Nothing in the chart's render may carry one of those
   names without being in the fixture.
2. Metadata. Each of the five carries exactly the chart's standard labels
   (app.kubernetes.io/managed-by: krateo-observability-defaults, app.kubernetes.io/part-of:
   krateo-observability) and nothing else, and no annotations (Helm adds its own
   ownership metadata when it applies them).
3. References. Every rendered metric Alert names a rendered Metric (alert-provider refuses one whose
   Metric is missing).
4. Counts and gates. Default render: 35 Alerts (the core tier) and 4 Metrics; with defaultAlerts.extended,
   41 Alerts; with defaultAlerts.enabled=false, none of either.
5. Threshold. The `threshold` of every rendered Alert is a YAML number, and only the ratio alert's is a
   fraction.

Run: python3 tests/slo-defaults.py [repo-root]
Exit 0 = every expectation held, 1 = at least one did not.
"""
import pathlib
import subprocess
import sys

import yaml

CHART = "charts/krateo-observability"
FIXTURE = "tests/fixtures/slo-objects-render.yaml"
NAMESPACE = "krateo-system"
RELEASE = "krateo-observability"
STANDARD_LABELS = {
    "app.kubernetes.io/managed-by": "krateo-observability-defaults",
    "app.kubernetes.io/part-of": "krateo-observability",
}
COUNTS = {  # (Alerts, Metrics)
    "default": (35, 4),
    "extended": (41, 4),
    "disabled": (0, 0),
}

failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


def render(root, *args):
    out = subprocess.run(
        ["helm", "template", RELEASE, str(root / CHART), "--namespace", NAMESPACE, *args],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d and d.get("kind") in ("Alert", "Metric")]


def typed(v):
    """A value with its JSON type spelled out, so 500 != 500.0 and 1 != True."""
    if isinstance(v, dict):
        return {k: typed(x) for k, x in v.items()}
    if isinstance(v, list):
        return [typed(x) for x in v]
    return (type(v).__name__, v)


def diff(a, b, path=""):
    out = []
    for k in sorted(set(a) | set(b)):
        p = f"{path}.{k}" if path else k
        if k not in a:
            out.append(f"{p}: only in the chart ({b[k]!r})")
        elif k not in b:
            out.append(f"{p}: missing from the chart (fixture {a[k]!r})")
        elif isinstance(a[k], dict) and isinstance(b[k], dict):
            out += diff(a[k], b[k], p)
        elif typed(a[k]) != typed(b[k]):
            out.append(f"{p}: fixture {a[k]!r} ({type(a[k]).__name__}), chart {b[k]!r} ({type(b[k]).__name__})")
    return out


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    fixture = [d for d in yaml.safe_load_all((root / FIXTURE).read_text()) if d]
    check(len(fixture) == 5, f"fixture holds 5 objects ({len(fixture)})")

    docs = render(root)
    by_key = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    names = {d["metadata"]["name"] for d in fixture}

    # 1 + 2: each fixture object, rendered identically, with the chart's own labels.
    for want in fixture:
        key = (want["kind"], want["metadata"]["name"])
        got = by_key.get(key)
        check(got is not None, f"{key[0]} {key[1]} renders")
        if got is None:
            continue
        check(got["apiVersion"] == want["apiVersion"], f"{key[1]}: apiVersion {got['apiVersion']}")
        check(got["metadata"]["namespace"] == want["metadata"]["namespace"] == NAMESPACE,
              f"{key[1]}: namespace {got['metadata']['namespace']}")
        d = diff(want["spec"], got["spec"])
        check(not d, f"{key[1]}: spec equals the generator's" + ("" if not d else ": " + "; ".join(d)))
        check(got["metadata"].get("labels") == STANDARD_LABELS,
              f"{key[1]}: labels are exactly the chart's standard two ({got['metadata'].get('labels')})")
        check(not got["metadata"].get("annotations"), f"{key[1]}: no annotations in the render")

    stray = sorted(n for (k, n) in by_key if (n.startswith("cmp-slo-") or n.startswith("slo-span-")) and n not in names)
    check(not stray, f"no cmp-slo-*/slo-span-* object outside the fixture ({stray})")

    # 3: every metric Alert's Metric is rendered.
    metrics = {n for (k, n) in by_key if k == "Metric"}
    for (k, n), d in sorted(by_key.items()):
        ref = (d["spec"].get("metricRef") or {}).get("name") if k == "Alert" else None
        if ref:
            check(ref in metrics, f"{n}: its Metric {ref} renders")

    # 5: thresholds are numbers; only the ratio alert's is a fraction.
    for (k, n), d in sorted(by_key.items()):
        if k != "Alert":
            continue
        t = d["spec"]["threshold"]
        check(isinstance(t, (int, float)) and not isinstance(t, bool), f"{n}: threshold {t!r} is a number")
        if isinstance(t, float):
            check(n == "cmp-slo-api-error-ratio", f"{n}: the only fractional threshold is the ratio alert's ({t!r})")

    # 4: counts per gate.
    for label, args in (("default", ()), ("extended", ("--set", "defaultAlerts.extended=true")),
                        ("disabled", ("--set", "defaultAlerts.enabled=false"))):
        rendered = docs if label == "default" else render(root, *args)
        alerts = sum(1 for d in rendered if d["kind"] == "Alert")
        mets = sum(1 for d in rendered if d["kind"] == "Metric")
        check((alerts, mets) == COUNTS[label], f"{label}: {alerts} Alerts, {mets} Metrics (want {COUNTS[label]})")

    print()
    if failures:
        print(f"{len(failures)} expectation(s) failed")
        return 1
    print("every expectation held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
