#!/usr/bin/env python3
"""Guards retracted-versions.yaml (the list installer's CI reads to refuse a pin).

Checks: the file parses; `retracted` is a list; each entry has version, charts, use and reason;
every chart named is one this repo publishes (charts/<dir>/Chart.yaml `name:`), so the list can't
speak for another publisher; version and use are semver; use != version; and, unless --offline,
both `version` and `use` resolve in the OCI registry (a retraction of an unpublished version, or a
replacement that doesn't exist, is a broken entry).

Run: python3 tests/retracted-versions.py [--offline] [--file PATH]
(--file checks another list than the repo's; tests/retracted-versions-refusal.py uses it to show refusals.)
"""
import pathlib
import re
import subprocess
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = "oci://ghcr.io/krateo-platformops/charts"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def published_names():
    names = set()
    for f in sorted(ROOT.glob("charts/*/Chart.yaml")):
        names.add(yaml.safe_load(f.read_text())["name"])
    return names


def resolves(chart, version):
    r = subprocess.run(["helm", "show", "chart", f"{REGISTRY}/{chart}", "--version", version],
                       capture_output=True, text=True)
    return r.returncode == 0 and f"version: {version}" in r.stdout


def main():
    offline = "--offline" in sys.argv
    path = pathlib.Path(sys.argv[sys.argv.index("--file") + 1]) if "--file" in sys.argv else ROOT / "retracted-versions.yaml"
    doc = yaml.safe_load(path.read_text())
    check(isinstance(doc, dict) and isinstance(doc.get("retracted"), list), "`retracted` is a list")
    names = published_names()
    check(len(names) > 0, f"this repo publishes charts: {sorted(names)}")
    for i, e in enumerate(doc.get("retracted") or []):
        tag = f"entry {i} ({e.get('charts')} {e.get('version')})"
        check(set(e) >= {"version", "charts", "use", "reason"}, f"{tag}: has version, charts, use, reason")
        check(isinstance(e.get("charts"), list) and e["charts"], f"{tag}: charts is a non-empty list")
        for c in e.get("charts") or []:
            check(c in names, f"{tag}: chart {c} is published by this repo")
        v, u = str(e.get("version", "")), str(e.get("use", ""))
        check(bool(SEMVER.match(v)) and bool(SEMVER.match(u)), f"{tag}: version and use are semver")
        check(v != u, f"{tag}: use differs from version")
        check(len(str(e.get("reason", "")).strip()) > 40, f"{tag}: reason says why")
        if not offline:
            for c in e.get("charts") or []:
                check(resolves(c, v), f"{tag}: {c} {v} is published (a retraction needs a real release)")
                check(resolves(c, u), f"{tag}: {c} {u} (the replacement) is published")
    # The refusal cases (an entry naming a chart this repo does not publish) are exercised by
    # tests/retracted-versions-refusal.py, which runs this file against a temp list.
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
