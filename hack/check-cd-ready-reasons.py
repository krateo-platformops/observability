#!/usr/bin/env python3
"""Gate: the reasons sre-krateo-definition-not-ready does NOT fire on are the table's.

core-provider writes a failed Ready on a CompositionDefinition as a successful reconcile that only
sets status.conditions[type=Ready]. Its table, docs/cd-ready-reasons.json, gives every Ready reason a
verdict: healthy, converging, unevaluated or failed. The alert fires on a Ready=False whose reason is
NOT IN a list, so a reason that is not failing must be on that list, or the alert fires on it; and a
failing reason must not be, or the alert is silent on it. The table is open, a release can add a row,
so this gate holds the list to the table at the tag pinned in hack/core-provider-pin.yaml:

  list == the reasons of every Ready row whose verdict is healthy, converging or unevaluated

It also holds defaultAlerts.definitionConvergenceBounds, the bounds of sre-krateo-definition-converging-too-long,
to the table: every Ready reason whose verdict is converging, or whose clearance kind is `next pass`,
has a bound with the table's status, and a bound names no other reason. A new converging or next-pass
reason with no bound fails here.

It fails closed: a table that cannot be fetched or parsed, fewer Ready rows than MIN_READY_ROWS, a
verdict it does not know, a reason that is both failed and not, or a list it cannot find, all exit 1.

Run: python3 hack/check-cd-ready-reasons.py [--root DIR] [--table FILE | --url URL] [--values FILE]
  --table FILE  read the table from a file (the self-test, tests/check-cd-ready-reasons.py)
  --url URL     fetch from URL instead of the pinned tag's
"""
import argparse
import json
import pathlib
import re
import sys
import time
import urllib.request

import yaml

ALERT = "sre-krateo-definition-not-ready"
PIN = "hack/core-provider-pin.yaml"
VALUES = "charts/krateo-observability/values.yaml"
URL = "https://raw.githubusercontent.com/krateo-platformops/core-provider/{tag}/docs/cd-ready-reasons.json"
# 2026-11-28: core-provider 2.13.136 has 12 Ready rows. A table with fewer has lost rows, or this
# read the wrong thing, and a comparison over nothing passes.
MIN_READY_ROWS = 12
NOT_FAILING = {"healthy", "converging", "unevaluated"}
KNOWN = NOT_FAILING | {"failed"}


def fetch(url):
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return r.read().decode()
        except Exception as e:  # any failure is a failure of the gate, not a pass
            last = e
            time.sleep(2 * attempt)
    raise SystemExit(f"FAIL cannot fetch {url}: {last}")


def table_reasons(text):
    """(not failing reasons, failing reasons, Ready rows examined) of a table's text."""
    try:
        rows = json.loads(text)["reasons"]
    except Exception as e:
        raise SystemExit(f"FAIL the table is not JSON with a `reasons` list: {e}")
    ready = [r for r in rows if r.get("condition") == "Ready"]
    if len(ready) < MIN_READY_ROWS:
        raise SystemExit(f"FAIL {len(ready)} Ready rows examined, fewer than {MIN_READY_ROWS} (2026-11-28)")
    good, bad = set(), set()
    for r in ready:
        v = r.get("verdict")
        if v not in KNOWN:
            raise SystemExit(f"FAIL Ready reason {r.get('reason')!r} has verdict {v!r}, not one of {sorted(KNOWN)}: "
                             "decide what the alert does with it, then teach this gate")
        (good if v in NOT_FAILING else bad).add(r["reason"])
    both = good & bad
    if both:
        raise SystemExit(f"FAIL reason(s) {sorted(both)} are failed in one Ready row and not in another: "
                         "the alert reads the reason alone")
    return good, bad, len(ready)


def needs_bound(text):
    """{reason: status} of every Ready row that must pass on its own: converging, or `next pass`."""
    out = {}
    for r in json.loads(text)["reasons"]:
        if r.get("condition") != "Ready":
            continue
        kind = (r.get("clearance") or {}).get("kind")
        if kind is None:
            raise SystemExit(f"FAIL Ready reason {r.get('reason')!r} has no clearance.kind")
        if r["verdict"] == "converging" or kind == "next pass":
            out[r["reason"]] = r["status"]
    return out


def bounds(root, values=None):
    b = yaml.safe_load((pathlib.Path(values) if values else root / VALUES).read_text())["defaultAlerts"].get("definitionConvergenceBounds")
    if not b:
        raise SystemExit("FAIL no defaultAlerts.definitionConvergenceBounds")
    return {k: v["status"] for k, v in b.items()}


def alert_list(root, values=None):
    values = yaml.safe_load((pathlib.Path(values) if values else root / VALUES).read_text())
    alerts = [a for a in values["defaultAlerts"]["alerts"] if a["name"] == ALERT]
    if len(alerts) != 1:
        raise SystemExit(f"FAIL {len(alerts)} alerts named {ALERT} in {VALUES}")
    found = re.findall(r"'reason'\) NOT IN \(([^)]*)\)", alerts[0]["where"])
    if len(found) != 1:
        raise SystemExit(f"FAIL {len(found)} `'reason') NOT IN (...)` lists in the where of {ALERT}")
    items = re.findall(r"'([^']*)'", found[0])
    if len(items) != len(set(items)) or not items:
        raise SystemExit(f"FAIL the list {items} is empty or repeats a reason")
    return set(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--table")
    ap.add_argument("--url")
    ap.add_argument("--values", help="read the alerts and bounds from this values file (the self-test)")
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    m = re.search(r"^tag:\s*(\S+)\s*$", (root / PIN).read_text(), re.M)
    if not m:
        raise SystemExit(f"FAIL no `tag:` in {PIN}")
    tag = m.group(1)
    text = pathlib.Path(a.table).read_text() if a.table else fetch(a.url or URL.format(tag=tag))
    good, bad, n = table_reasons(text)
    have = alert_list(root, a.values)
    print(f"core-provider {tag}: {n} Ready rows, {len(good)} not failing, {len(bad)} failing; alert lists {len(have)}")
    missing, extra = sorted(good - have), sorted(have - good)
    if missing:
        print(f"FAIL not failing in the table, missing from the alert's list (it would fire on them): {missing}")
    if extra:
        print(f"FAIL on the alert's list, not a not-failing Ready reason in the table (it would stay silent on them): {extra}")
    need, have_b = needs_bound(text), bounds(root, a.values)
    print(f"{len(need)} converging or next-pass Ready reasons need a bound; {len(have_b)} have one")
    nob = sorted(set(need) - set(have_b))
    stale = sorted(set(have_b) - set(need))
    wrong = sorted(r for r in set(need) & set(have_b) if need[r] != have_b[r])
    if nob:
        print(f"FAIL converging or next-pass in the table, no bound in definitionConvergenceBounds (a definition could sit in them forever): {nob}")
    if stale:
        print(f"FAIL a bound for a reason that is not converging or next-pass in the table: {stale}")
    if wrong:
        print(f"FAIL a bound whose status is not the table's: {wrong}")
    if missing or extra or nob or stale or wrong:
        sys.exit(1)
    print("ok   every converging and next-pass reason has a bound, with the table's status")
    print("ok   the alert's list is the table's healthy, converging and unevaluated Ready reasons")


if __name__ == "__main__":
    main()
