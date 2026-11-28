#!/usr/bin/env python3
"""Self-test for hack/public-text-check.py: the planted control must fire when the matcher is dead.

Run: python3 tests/public-text-check.py
"""
import contextlib
import importlib.util
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ptc", ROOT / "hack" / "public-text-check.py")
ptc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ptc)

failures = []


def check(name, cond, detail=""):
    print("%-4s %s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        failures.append(name)


def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ptc.main([str(ROOT)])
    return rc, buf.getvalue()


# 1. The real matcher catches the planted leak, and the leak is not in any tracked file.
check("planted leak is found by the real matcher", len(ptc.control_findings()) >= 3, str(len(ptc.control_findings())))
rc, out = run()
check("working matcher: control passes and the repo is clean", rc == 0 and "planted control caught" in out, out.strip().splitlines()[-1])

# 2. scan() stubbed to find nothing: exit 2 with the message, before any file is read.
real_scan = ptc.scan
ptc.scan = lambda *a, **k: None
rc, out = run()
ptc.scan = real_scan
check("stubbed scan: exit 2", rc == 2, out.strip())
check("stubbed scan: says the matcher is broken", "found nothing in a planted leak: the matcher is broken" in out)
check("stubbed scan: no file was scanned", "tracked files" not in out)

# 3. Every pattern emptied: same refusal.
saved = ptc.PATTERNS[:], ptc.ALLOWABLE[:]
ptc.PATTERNS[:], ptc.ALLOWABLE[:] = [], []
rc, out = run()
ptc.PATTERNS[:], ptc.ALLOWABLE[:] = saved
check("empty pattern lists: exit 2", rc == 2, out.strip())

# 4. Restored, the check works again.
check("restored: exit 0", run()[0] == 0)

sys.exit(1 if failures else 0)
