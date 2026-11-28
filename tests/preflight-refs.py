#!/usr/bin/env python3
"""Self-test for hack/preflight-refs.py: the gate must count what it should and refuse what it must.

No network: the scan runs on temporary trees via `--list`.
Run: python3 tests/preflight-refs.py
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hack" / "preflight-refs.py"
spec = importlib.util.spec_from_file_location("preflight_refs", SCRIPT)
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)

VALUES = """image:
  repository: ghcr.io/acme/widget
  tag: "1.2.3"
"""
CHART_REF = """    url: oci://ghcr.io/acme/charts/gadget
    version: 0.4.5
"""
failures = []


def check(name, cond, detail=""):
    print("%-4s %s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        failures.append(name)


def write(base, rel, text):
    p = pathlib.Path(base) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def cli(tree):
    r = subprocess.run([sys.executable, str(SCRIPT), "--list", str(tree)], capture_output=True, text=True)
    return r.returncode, r.stdout


# 1. A values.yaml image ref under charts/ is counted (the original defect: charts/ was pruned).
with tempfile.TemporaryDirectory() as d:
    write(d, "charts/x/values.yaml", VALUES)
    write(d, "manifest.yaml", CHART_REF)
    charts, images = pf.scan(d)
    check("charts/x/values.yaml image counted", ("ghcr.io", "acme/widget", "1.2.3") in {i[:3] for i in images}, repr(images))
    check("chart ref counted", len(charts) == 1, repr(charts))

# 2. An empty tree fails the floor, in the library and through the command line.
with tempfile.TemporaryDirectory() as d:
    charts, images = pf.scan(d)
    check("empty tree: floor_errors reports both", len(pf.floor_errors(charts, images)) == 2)
    rc, out = cli(d)
    check("empty tree: exit 1", rc == 1 and "examined 0 image refs, expected at least" in out, out.strip().splitlines()[-2:].__repr__())

# 3. A ref under charts/x/charts/ (vendored dependency) is ignored, and the skip is reported.
with tempfile.TemporaryDirectory() as d:
    write(d, "charts/x/charts/dep/values.yaml", VALUES)
    skipped = set()
    charts, images = pf.scan(d, skipped)
    check("vendored ref ignored", not images and not charts, repr(images))
    check("vendored dir listed as skipped", "charts/x/charts/" in skipped, repr(skipped))
    write(d, "charts/x/values.yaml", VALUES)
    _, images = pf.scan(d)
    check("sibling charts/x/values.yaml still counted", len(images) == 1)

# 4. Fixture directories stay skipped.
with tempfile.TemporaryDirectory() as d:
    write(d, "charts/x/tests/values.yaml", VALUES)
    check("tests/ fixture ignored", not pf.scan(d)[1])

# 5. Split keys in values.yaml: counted wherever the tag sits, and what cannot be resolved is listed.
SPLIT = """app:
  image:
    repository: clickhouse/clickhouse-server
    # a long comment block between the keys,
    # as in this repository's charts,
    # pushes the tag out of any short line window,
    # and a digest pin is part of the tag
    # (five lines)
    tag: "26.8-alpine@sha256:abc"
other:
  image:
    registry: ghcr.io
    repository: acme/gadget
    version: 2.0.0
digested:
  repository: quay.io/acme/pinned
  digest: sha256:def
templated:
  image:
    repository: ghcr.io/acme/templated
    tag: "{{ .Chart.AppVersion }}"
untagged:
  image:
    repository: otel/collector
dependency:
  repository: oci://ghcr.io/acme
"""
with tempfile.TemporaryDirectory() as d:
    write(d, "charts/x/values.yaml", SPLIT)
    unchecked = []
    _, images = pf.scan(d, None, unchecked)
    got = {i[:3] for i in images}
    check("split keys with a comment gap and a digest counted",
          ("docker.io", "clickhouse/clickhouse-server", "26.8-alpine@sha256:abc") in got, repr(got))
    check("registry + repository + version counted", ("ghcr.io", "acme/gadget", "2.0.0") in got, repr(got))
    check("repository + digest counted", ("quay.io", "acme/pinned", "sha256:def") in got, repr(got))
    check("exactly the three resolvable mappings counted", len(images) == 3, repr(got))
    reasons = {(u[0], u[1]): u[3] for u in unchecked}
    check("templated tag listed as skipped with file and path",
          "templated tag" in reasons.get(("charts/x/values.yaml", "templated.image"), ""), repr(unchecked))
    check("absent tag listed as skipped with file and path",
          "no tag" in reasons.get(("charts/x/values.yaml", "untagged.image"), ""), repr(unchecked))
    check("oci:// repository is a chart dependency, not listed", len(unchecked) == 2, repr(unchecked))
    rc, out = cli(d)
    check("--list prints each skipped mapping",
          "charts/x/values.yaml:" in out and "templated.image" in out and "untagged.image" in out, out[:300])

# 6. The measured floor holds on this repository itself.
charts, images = pf.scan(str(ROOT))
check("this repo meets the floor", not pf.floor_errors(charts, images), "%d chart refs, %d image refs" % (len(charts), len(images)))
check("this repo: krateo-slo-rollup's split-key image is counted",
      any(i[3] == "charts/krateo-slo-rollup/values.yaml" and i[1] == "clickhouse/clickhouse-server" for i in images))

sys.exit(1 if failures else 0)
