#!/usr/bin/env python3
"""Self-test for hack/check-collector-components.py: the gate must refuse what it must refuse.

No docker, no helm: configs and component lists are fed in directly.
Run: python3 tests/check-collector-components.py
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hack" / "check-collector-components.py"
spec = importlib.util.spec_from_file_location("ccc", SCRIPT)
ccc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccc)

failures = []


def check(name, cond, detail=""):
    print("%-4s %s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        failures.append(name)


# A fake image: what `<image> components` would list. Note file_log, as the real one spells it.
IMAGE = {
    "receivers": [{"name": "otlp"}, {"name": "file_log"}],
    "processors": [{"name": "batch"}],
    "exporters": [{"name": "clickhouse"}],
    "connectors": [],
    "extensions": [{"name": "health_check"}],
}
with tempfile.TemporaryDirectory() as d:
    f = pathlib.Path(d) / "components.yaml"
    import yaml
    f.write_text(yaml.safe_dump(IMAGE))
    available = ccc.image_components("fake", str(f))

# 1. A config naming a component the image lacks is a gap.
relay = {"receivers": {"otlp": {}, "filelog/x": {}}, "processors": {"batch": {}, "transform": {}}}
gaps, compared = ccc.compare(relay, available)
check("absent component is reported", gaps == ["processors: transform"], repr(gaps))
check("every configured component is counted", compared == 4, str(compared))

# 2. An empty config compares nothing: the floor of every chart refuses it.
for chart, floor in ccc.MIN_COMPONENTS.items():
    for empty in ({}, {"service": {"pipelines": {}}}, {"receivers": None}):
        _, compared = ccc.compare(empty, available)
        check(f"{chart}: empty config fails its floor", compared < floor, f"{compared} < {floor}")

# 3. The command line exits 1 for an unknown component, via --components and no docker.
#    A tiny chart tree is not needed: render() is replaced, main() is the real one.
def run_main(relay, components, floor, image_override=None):
    ccc.CHARTS_BACKUP = (dict(ccc.CHARTS), dict(ccc.MIN_COMPONENTS))
    ccc.CHARTS.clear(); ccc.CHARTS["charts/otel-collector-fake"] = "Deployment"
    ccc.MIN_COMPONENTS.clear(); ccc.MIN_COMPONENTS["charts/otel-collector-fake"] = floor
    real_render, real_argv = ccc.render, sys.argv
    ccc.render = lambda root, chart, kind: (relay, "fake:1")
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "charts" / "otel-collector-fake").mkdir(parents=True)
        lst = pathlib.Path(d) / "components.yaml"
        lst.write_text(yaml.safe_dump(components))
        sys.argv = ["x", d, "--components", f"charts/otel-collector-fake={lst}"]
        if image_override:
            sys.argv += ["--image", f"charts/otel-collector-fake={image_override}"]
        try:
            return ccc.main()
        finally:
            ccc.render, sys.argv = real_render, real_argv
            ccc.CHARTS.clear(); ccc.CHARTS.update(ccc.CHARTS_BACKUP[0])
            ccc.MIN_COMPONENTS.clear(); ccc.MIN_COMPONENTS.update(ccc.CHARTS_BACKUP[1])


check("main: component absent from the image exits 1", run_main(relay, IMAGE, 1) == 1)
check("main: config within the image and above the floor exits 0",
      run_main({"receivers": {"otlp": {}}, "processors": {"batch": {}}}, IMAGE, 2) == 0)
import contextlib, io
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = run_main({"receivers": {"otlp": {}}}, IMAGE, 1, image_override="override:9")
check("main: --image overrides the rendered image", rc == 0 and "override:9" in buf.getvalue() and "fake:1" not in buf.getvalue(), buf.getvalue().strip())
check("main: empty config exits 1 on the floor", run_main({}, IMAGE, 2) == 1)

# 4. A collector chart that CHARTS does not name fails, and a declared chart that is gone fails.
with tempfile.TemporaryDirectory() as d:
    for name in ccc.CHARTS:
        (pathlib.Path(d) / name).mkdir(parents=True)
    check("matching chart set has no errors", ccc.chart_list_errors(pathlib.Path(d)) == [])
    (pathlib.Path(d) / "charts" / "otel-collector-extra").mkdir()
    errs = ccc.chart_list_errors(pathlib.Path(d))
    check("an unlisted otel-collector-* chart is refused", len(errs) == 1 and "otel-collector-extra" in errs[0], repr(errs))
    r = subprocess.run([sys.executable, str(SCRIPT), d], capture_output=True, text=True)
    check("command line exits 1 for it", r.returncode == 1 and "otel-collector-extra" in r.stdout, r.stdout.strip()[:120])
with tempfile.TemporaryDirectory() as d:
    (pathlib.Path(d) / "charts").mkdir()
    check("a declared chart with no directory is refused", len(ccc.chart_list_errors(pathlib.Path(d))) == len(ccc.CHARTS))

# 5. This repository: CHARTS is exactly the charts/otel-collector-* directories.
check("this repo: chart list matches charts/", ccc.chart_list_errors(ROOT) == [], repr(ccc.chart_list_errors(ROOT)))

sys.exit(1 if failures else 0)
