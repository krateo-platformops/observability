#!/usr/bin/env python3
"""The ClickHouse server and Keeper images must be pinned by an EXACT version tag.

operator v0.0.7 (ClickHouse/clickhouse-operator api/v1alpha1/defaults.go) defaults an empty
`containerTemplate.image.tag` to `latest`, and ContainerImage.String() then prefers the tag over
`hash`. So a hash-only pin silently becomes `latest`: krateo-observability 0.1.43/0.1.44 did exactly
that, and the 2026-11-11 roll on krateo-057 ran clickhouse-server:latest (26.10.3.7) instead of the
intended 26.8.36.3. This renders the chart, applies the operator's rule to both CRs, and fails unless
each resolves to `<repository>:<exact version>`. It also checks the ClickHouse client image of
krateo-audit-worm's Jobs, a plain image whose floating tag pulled each new build.

Run: python3 tests/clickhouse-image-pin.py [repo-root]    Exit 0 = both pinned.
"""
import pathlib
import re
import subprocess
import sys

import yaml

EXACT = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?(-alpine)?$")


def operator_image(img, default_repo):
    """api/v1alpha1: defaulting (tag "" -> "latest"), then ContainerImage.String()."""
    repo = img.get("repository") or default_repo
    tag = img.get("tag") or "latest"
    return f"{repo}:{tag}", tag


def pod_images(doc):
    """Every container image of a workload or CronJob."""
    spec = doc.get("spec") or {}
    if doc.get("kind") == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    pod = (spec.get("template") or {}).get("spec") or {}
    return [c["image"] for k in ("initContainers", "containers") for c in pod.get(k) or []]


def check_audit_worm(root):
    """krateo-audit-worm runs clickhouse-client from the server image in its Jobs: an exact tag, not
    a floating `24.8-alpine` that pulls each new build on the next pod."""
    chart = root / "charts" / "krateo-audit-worm"
    out = subprocess.run(["helm", "template", "x", str(chart), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    imgs = sorted({i for d in yaml.safe_load_all(out) if d for i in pod_images(d) if "clickhouse" in i})
    if not imgs:
        print("FAIL krateo-audit-worm: no ClickHouse image rendered, so nothing was checked")
        return 1
    fails = 0
    for i in imgs:
        tag = i.rsplit(":", 1)[1] if ":" in i.rsplit("/", 1)[-1] else ""
        ok = bool(EXACT.match(tag))
        print(f"{'ok  ' if ok else 'FAIL'} krateo-audit-worm: {i}" + ("" if ok else " (needs an exact version tag)"))
        fails += not ok
    return fails


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    chart = root / "charts" / "krateo-observability"
    subprocess.run(["helm", "dependency", "build", str(chart)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "x", str(chart), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    fails = 0
    for kind, default_repo in (("ClickHouseCluster", "docker.io/clickhouse/clickhouse-server"),
                               ("KeeperCluster", "docker.io/clickhouse/clickhouse-keeper")):
        crs = [d for d in docs if d.get("kind") == kind]
        if len(crs) != 1:
            print(f"FAIL {kind}: {len(crs)} rendered, expected 1"); fails += 1; continue
        img = ((crs[0]["spec"].get("containerTemplate") or {}).get("image") or {})
        ref, tag = operator_image(img, default_repo)
        ok = bool(EXACT.match(tag)) and "hash" not in img
        print(f"{'ok  ' if ok else 'FAIL'} {kind}: the operator runs {ref}"
              + ("" if ok else " (needs an exact version tag, and no hash, which the operator ignores)"))
        fails += not ok
    s = [d for d in docs if d.get("kind") == "ClickHouseCluster"][0]["spec"]["containerTemplate"]["image"].get("tag")
    k = [d for d in docs if d.get("kind") == "KeeperCluster"][0]["spec"].get("containerTemplate", {}).get("image", {}).get("tag")
    same = bool(s) and s == k
    print(f"{'ok  ' if same else 'FAIL'} server and Keeper on the same version ({s} / {k})"); fails += not same
    fails += check_audit_worm(root)
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
