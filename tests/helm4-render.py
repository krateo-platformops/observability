#!/usr/bin/env python3
"""krateo-observability renders the same under Helm 3 and Helm 4 (0.1.54).

The platform renders the chart with Helm 3 (composition-dynamic-controller, Helm 3.20). Helm 4.2 and
later coalesce values differently: a null in a parent chart's own values.yaml is dropped before the
subchart's defaults merge in (helm/helm#31979; reported as helm/helm#32132), so it no longer deletes
the subchart's key. Under Helm 4.3.2, 0.1.53's `hyperdx.config.MONGO_URI: null` brought upstream's
URI, with its default password, back into clickstack-config, and the chart's guard refused the render.

For the default values, and again with .github/ci/injected-global.yaml (the `global` block the
platform injects), this renders the chart with both binaries and checks:
  1. both renders succeed;
  2. in each, clickstack-config's MONGO_URI and HYPERDX_OTEL_EXPORTER_TABLES_TTL are empty, and no
     ConfigMap holds a mongodb://user:password@ URI;
  3. the two renders are the same object for object, field for field, except:
     - values that differ between two renders by the SAME binary (generated passwords), and
     - ALLOWED: ClickHouseCluster spec.settings.logger, present under Helm 4 only. It stays null in
       values.yaml (no value renders as "no logger" in both versions); once Helm 4 changes this (helm/helm#32132) the
       difference goes away, and this check still passes.
Values are never printed for a Secret (path only).

Run: python3 tests/helm4-render.py <repo-root> <helm3-binary> <helm4-binary>
The chart's dependency must already be built (helm dependency build charts/krateo-observability).
Exit 0 = every check passed, 1 = at least one did not.
"""
import json
import pathlib
import re
import subprocess
import sys

import yaml

CHART = "charts/krateo-observability"
INJECTED = ".github/ci/injected-global.yaml"
RELEASE = "krateo-observability"
NAMESPACE = "krateo-system"
ALLOWED = {("ClickHouseCluster", "spec.settings.logger")}
CRED_URI = re.compile(r"mongodb(\+srv)?://[^/\s@]*:[^/\s@]*@", re.I)

failures = []


def check(ok, what):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


def version(helm):
    return subprocess.run([helm, "version", "--short"], check=True, capture_output=True, text=True).stdout.strip()


def render(helm, root, args):
    r = subprocess.run([helm, "template", RELEASE, str(root / CHART), "--namespace", NAMESPACE, *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        # The error names the template and the guard; it carries no value.
        errors = [l for l in r.stderr.splitlines() if l.startswith("Error")]
        return None, (errors or r.stderr.strip().splitlines()[-1:] or ["(no stderr)"])[0][:300]
    docs = {}
    for d in yaml.safe_load_all(r.stdout):
        if d:
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


def allowed(kind, path):
    return any(kind == k and (path == p or path.startswith(p + ".")) for k, p in ALLOWED)


def show(kind, v):
    return "<not shown>" if kind == "Secret" else json.dumps(v)[:120]


def check_credential(docs, label):
    cm = (docs.get(("ConfigMap", "clickstack-config")) or {}).get("data") or {}
    for key in ("MONGO_URI", "HYPERDX_OTEL_EXPORTER_TABLES_TTL"):
        state = "absent" if key not in cm else ("empty" if cm[key] == "" else "NOT EMPTY")
        check(state == "empty", f"{label}: clickstack-config {key} is empty ({state})")
    held = sorted(name for (kind, name), d in docs.items()
                  if kind == "ConfigMap" and CRED_URI.search(json.dumps(d.get("data") or {})))
    check(not held, f"{label}: no ConfigMap holds a mongodb://user:password@ URI ({held or 'none'})")


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    root, h3, h4 = pathlib.Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
    v3, v4 = version(h3), version(h4)
    check(v3.startswith("v3."), f"first binary is Helm 3 ({v3})")
    check(v4.startswith("v4."), f"second binary is Helm 4 ({v4})")
    for label, args in (("defaults", []), ("injected global", ["-f", str(root / INJECTED)])):
        a, err_a = render(h3, root, args)
        a2, _ = render(h3, root, args)
        b, err_b = render(h4, root, args)
        check(a is not None, f"{label}: renders under {v3} ({err_a or 'ok'})")
        check(b is not None, f"{label}: renders under {v4} ({err_b or 'ok'})")
        if a is None or b is None:
            continue
        check_credential(a, f"{label}, {v3}")
        check_credential(b, f"{label}, {v4}")

        fa, fa2, fb = flat(a), flat(a2 or {}), flat(b)
        random = {k for k in fa if fa2.get(k, object()) != fa[k]}
        if random:
            print(f"      {label}: {len(random)} generated value(s) differ between two {v3} renders; "
                  f"not compared: {sorted({f'{k[0]}/{k[1]}' for k in random})}")
        seen_allowed, unexpected = set(), []
        for k in sorted(set(fa) | set(fb), key=str):
            if k in random or fa.get(k, object()) == fb.get(k, object()):
                continue
            kind, name, path = k
            if allowed(kind, path):
                seen_allowed.add(f"{kind}/{name} {path.rsplit('.', 1)[0] if '.' in path else path}")
                continue
            unexpected.append(f"{kind}/{name} {path}: {v3}={show(kind, fa.get(k, '<absent>'))} "
                              f"{v4}={show(kind, fb.get(k, '<absent>'))}")
        for line in unexpected[:40]:
            print(f"      {line}")
        check(not unexpected, f"{label}: {v3} and {v4} render the same except the allowed difference "
                              f"({len(unexpected)} other difference(s))")
        for s in sorted(seen_allowed):
            print(f"      {label}: allowed difference present: {s} (helm/helm#32132)")
        if not seen_allowed:
            print(f"      {label}: the allowed difference is gone; {v4} keeps the parent's null")
    print(f"\n{len(failures)} check(s) failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
