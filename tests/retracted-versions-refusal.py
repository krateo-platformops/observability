#!/usr/bin/env python3
"""Shows tests/retracted-versions.py REFUSING bad lists, and still passing the good ones.

A guard that has only been seen passing proves nothing. This runs the real check (--offline, so no
registry) against temp copies of the list:

  - an entry naming a chart this repo does not publish         -> must exit non-zero, naming the chart
  - an entry whose use equals its version                       -> must exit non-zero
  - an entry that is not semver                                 -> must exit non-zero
  - an empty `retracted: []`                                    -> must exit zero (nothing retracted is fine)
  - the repo's own list                                         -> must exit zero

Run: python3 tests/retracted-versions-refusal.py
"""
import pathlib
import subprocess
import sys
import tempfile

import yaml

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
CHECK = HERE / "retracted-versions.py"
REASON = "A reason that is long enough to pass the length check, written for this fixture only."


def published():
    return sorted(yaml.safe_load(f.read_text())["name"] for f in ROOT.glob("charts/*/Chart.yaml"))


def entry(**kw):
    e = {"version": "0.1.1", "charts": [published()[0]], "use": "0.1.2", "reason": REASON}
    e.update(kw)
    return e


def run(doc):
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "retracted-versions.yaml"
        f.write_text(yaml.safe_dump(doc))
        r = subprocess.run([sys.executable, str(CHECK), "--offline", "--file", str(f)], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def main():
    assert published(), "this repo publishes no chart: the fixtures below would test nothing"
    cases = [
        ("unpublished chart", {"retracted": [entry(charts=["not-a-chart-we-publish"])]}, False, "chart not-a-chart-we-publish is published"),
        ("use == version", {"retracted": [entry(use="0.1.1")]}, False, "use differs from version"),
        ("version not semver", {"retracted": [entry(version="latest")]}, False, "version and use are semver"),
        ("empty list", {"retracted": []}, True, None),
        ("the repo's own list", yaml.safe_load((ROOT / "retracted-versions.yaml").read_text()), True, None),
    ]
    bad = 0
    for name, doc, want_pass, needle in cases:
        rc, out = run(doc)
        ok = (rc == 0) == want_pass and (needle is None or any(needle in l and l.startswith("FAIL") for l in out.splitlines()))
        print(("ok   " if ok else "FAIL ") + f"{name}: exit {rc}, expected {'pass' if want_pass else 'refusal'}")
        if not ok:
            bad += 1
            print(out)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
