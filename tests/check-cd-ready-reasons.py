#!/usr/bin/env python3
"""Shows hack/check-cd-ready-reasons.py REFUSING what it must, and passing what it must.

A gate seen only passing proves nothing. This runs the real script against a saved copy of the Ready
rows of core-provider 2.13.136's reason table (tests/fixtures/cd-ready-reasons-2.13.136-ready-rows.json)
and against variants of it, offline:

  the saved table, as is                              -> exit 0
  a converging or next-pass reason with no bound, a
  bound removed, stale or with the wrong status       -> exit 1, naming it
  an extra HEALTHY Ready reason                       -> exit 1, naming it (the alert would fire on it)
  an extra CONVERGING reason, an extra UNEVALUATED    -> exit 1 each
  a healthy reason the table dropped                  -> exit 1 (the alert lists one that is gone)
  a FAILED reason that is now healthy                 -> exit 1 (the alert would stay silent on it)
  an extra FAILED reason                              -> exit 0 (the alert fires on it already)
  fewer Ready rows than the floor, and none           -> exit 1 (a comparison over nothing)
  a verdict the gate does not know                    -> exit 1
  a table that is not JSON                            -> exit 1
  a URL that cannot be fetched                        -> exit 1 (fail closed)

It also runs the gate over the real table at the pinned tag when --online is given (CI does).

Run: python3 tests/check-cd-ready-reasons.py [--online]
"""
import copy
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
GATE = ROOT / "hack" / "check-cd-ready-reasons.py"
BASE = json.loads((ROOT / "tests/fixtures/cd-ready-reasons-2.13.136-ready-rows.json").read_text())
FAILS = 0


def with_values(mutate, table=None):
    """The gate against the repo's values.yaml after mutate(values), and the saved table or `table`."""
    import yaml
    values = yaml.safe_load((ROOT / "charts/krateo-observability/values.yaml").read_text())
    mutate(values)
    with tempfile.TemporaryDirectory() as d:
        v, t = pathlib.Path(d) / "v.yaml", pathlib.Path(d) / "t.json"
        v.write_text(yaml.safe_dump(values))
        t.write_text(json.dumps(table or BASE))
        return run("--table", str(t), "--values", str(v))


def run(*args):
    r = subprocess.run([sys.executable, str(GATE), "--root", str(ROOT), *args], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def check(name, ok, detail=""):
    global FAILS
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    FAILS += not ok


def with_table(table):
    with tempfile.TemporaryDirectory() as d:
        f = pathlib.Path(d) / "t.json"
        f.write_text(table if isinstance(table, str) else json.dumps(table))
        return run("--table", str(f))


def variant(fn):
    t = copy.deepcopy(BASE)
    fn(t["reasons"])
    return t


def add(verdict, reason, status="True"):
    return lambda rows: rows.append({"condition": "Ready", "status": status, "reason": reason, "verdict": verdict,
                                      "clearance": {"kind": "indefinite" if verdict == "failed" else "steady"}})


assert sum(r["condition"] == "Ready" for r in BASE["reasons"]) == 12, "the fixture is the 12 Ready rows"

rc, out = with_table(BASE)
check("the saved 2.13.136 table passes", rc == 0, out)
for verdict, reason in (("healthy", "AdoptedByPolicy"), ("converging", "Upgrading"), ("unevaluated", "Pending")):
    rc, out = with_table(variant(add(verdict, reason)))
    check(f"an extra {verdict} reason is refused and named", rc == 1 and reason in out, out)


def drop_healthy(rows):
    # One healthy row replaced by a failed one, so the count stays at the floor.
    rows.remove(next(r for r in rows if r["reason"] == "ScaledToZero" and r["condition"] == "Ready"))
    add("failed", "SomethingNewBroke", status="False")(rows)


rc, out = with_table(variant(drop_healthy))
check("a healthy reason the table dropped is refused and named", rc == 1 and "ScaledToZero" in out, out)


def heal(rows):
    for r in rows:
        if r["reason"] == "ValuesSchemaMissing":
            r["verdict"], r["status"] = "healthy", "True"


rc, out = with_table(variant(heal))
check("a failed reason that became healthy is refused and named", rc == 1 and "ValuesSchemaMissing" in out, out)
rc, out = with_table(variant(add("failed", "SomethingNewBroke", status="False")))
check("an extra failed reason passes (the alert fires on it already)", rc == 0, out)
rc, out = with_table(variant(lambda rows: rows.remove(next(r for r in rows if r["reason"] == "Deleting"))))
check("one Ready row fewer than the floor is refused", rc == 1 and "fewer than" in out, out)
# The bounds of sre-krateo-definition-converging-too-long.
rc, out = with_table(variant(add("converging", "Upgrading", status="False")))
check("a new converging reason with no bound is refused and named (by the bound check as well)", rc == 1 and "Upgrading" in out, out)
rc, out = with_table(variant(lambda rows: rows.append({"condition": "Ready", "status": "Unknown", "reason": "Waiting",
                                                       "verdict": "unevaluated", "clearance": {"kind": "next pass"}})))
check("a new next-pass reason with no bound is refused and named", rc == 1 and "Waiting" in out and "no bound" in out, out)
rc, out = with_values(lambda v: v["defaultAlerts"]["definitionConvergenceBounds"].pop("Deleting"))
check("a bound removed is refused and named", rc == 1 and "Deleting" in out and "no bound" in out, out)
rc, out = with_values(lambda v: v["defaultAlerts"]["definitionConvergenceBounds"].update({"Available": {"status": "True", "minutes": 5}}))
check("a bound for a healthy reason is refused", rc == 1 and "Available" in out, out)
rc, out = with_values(lambda v: v["defaultAlerts"]["definitionConvergenceBounds"]["Creating"].update({"status": "True"}))
check("a bound with the wrong status is refused", rc == 1 and "status" in out, out)
rc, out = with_values(lambda v: v["defaultAlerts"].pop("definitionConvergenceBounds"))
check("no bounds at all is refused", rc == 1, out)
rc, out = with_table({"reasons": []})
check("an empty table is refused", rc == 1 and "0 Ready rows" in out, out)
rc, out = with_table(variant(add("mystery", "Odd", status="False")))
check("a verdict the gate does not know is refused", rc == 1 and "mystery" in out, out)
rc, out = with_table("<html>not json</html>")
check("a table that is not JSON is refused", rc == 1, out)
rc, out = run("--url", "http://127.0.0.1:9/cd-ready-reasons.json")
check("a URL that cannot be fetched is refused", rc == 1 and "cannot fetch" in out, out)
if "--online" in sys.argv:
    rc, out = run()
    check("the real table at the pinned tag passes", rc == 0, out)
print(f"\n{FAILS} failure(s)")
sys.exit(1 if FAILS else 0)
