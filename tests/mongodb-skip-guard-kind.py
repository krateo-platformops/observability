#!/usr/bin/env python3
"""krateo-observability 0.1.46 empties upstream's dead MongoDB password Secret. That is safe only on a
cluster whose MongoDB user already reads mongodb-hyperdx-credentials (0.1.44 or later): MCK watches the
Secret the CR references, and Helm updates Secrets before the CR, so emptying the old one in the same
upgrade that moves the user off it could set an empty password for a moment.

The chart refuses an UPGRADE when mongodb-hyperdx-credentials has no `uri` (the cluster skipped
0.1.44). It reads that Secret, not the MongoDBCommunity, because the composition controller that
renders the chart may get Secrets in its namespace but not MongoDBCommunity objects (installer read
both clusters' per-version ServiceAccounts, 2026-11-11), and a lookup gives rbacgen nothing to derive
a grant from. `lookup` needs an API server, so this runs the chart against one (kind):

  1. install, no Secret (a fresh install)              -> renders;
  2. upgrade, no Secret (skipped 0.1.44)               -> refused, naming 0.1.44;
  3. upgrade, the Secret without `uri` (hand-made)     -> refused;
  4. upgrade, the Secret with `uri` (0.1.44 has run)   -> renders.
Each render is `helm template --dry-run=server`, which runs lookups against the cluster and installs
nothing; `--is-upgrade` sets .Release.IsUpgrade as an upgrade does.

Run: python3 tests/mongodb-skip-guard-kind.py [repo-root] [--context CTX]
Without --context it creates a kind cluster and deletes it at the end. Exit 0 = every case behaved.
"""
import argparse
import base64
import pathlib
import secrets
import subprocess
import sys
import tempfile

import yaml

NS = "krateo-system"
NAME = "mongodb-hyperdx-credentials"
REFUSAL = "has not rolled krateo-observability 0.1.44"

failures = []


def check(ok, msg):
    print(f"{'ok  ' if ok else 'FAIL'} {msg}")
    if not ok:
        failures.append(msg)


def run(*args, check_=True, input_=None):
    return subprocess.run(list(args), capture_output=True, text=True, check=check_, input=input_)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--context")
    a = ap.parse_args()
    root = pathlib.Path(a.root).resolve()
    chart = root / "charts" / "krateo-observability"
    run("helm", "dependency", "build", str(chart))

    tmp = pathlib.Path(tempfile.mkdtemp())
    kubeconfig = tmp / "kubeconfig"
    cluster = None
    if a.context:
        kc, hc = ["--context", a.context], ["--kube-context", a.context]
    else:
        cluster = "mongodb-skip-guard"
        run("kind", "create", "cluster", "--name", cluster, "--kubeconfig", str(kubeconfig), "--wait", "120s")
        kc, hc = ["--kubeconfig", str(kubeconfig)], ["--kubeconfig", str(kubeconfig)]
    try:
        run("kubectl", *kc, "create", "namespace", NS, check_=False)

        def render(*extra):
            return run("helm", "template", "krateo-observability", str(chart), "-n", NS,
                       "--dry-run=server", *hc, *extra, check_=False)

        def put_secret(data):
            obj = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": NAME, "namespace": NS},
                   "type": "Opaque",
                   "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}
            run("kubectl", *kc, "apply", "-f", "-", input_=yaml.safe_dump(obj))

        def tail(r):
            return "" if r.returncode == 0 else f" -- {r.stderr.strip()[-300:]}"

        r = render()
        check(r.returncode == 0, "1. install, no Secret (fresh install): renders" + tail(r))
        r = render("--is-upgrade")
        check(r.returncode != 0 and REFUSAL in r.stderr, "2. upgrade, no Secret (skipped 0.1.44): refused, naming 0.1.44")
        pw = secrets.token_urlsafe(24)  # generated here, never printed
        put_secret({"username": "hyperdx", "password": pw})
        r = render("--is-upgrade")
        check(r.returncode != 0 and REFUSAL in r.stderr, "3. upgrade, the Secret without uri: refused")
        put_secret({"username": "hyperdx", "password": pw,
                    "uri": f"mongodb://hyperdx:{pw}@krateo-clickstack-mongodb-svc:27017/hyperdx?authSource=hyperdx"})
        r = render("--is-upgrade")
        check(r.returncode == 0, "4. upgrade, the Secret with uri (0.1.44 has run): renders" + tail(r))
    finally:
        if cluster:
            run("kind", "delete", "cluster", "--name", cluster, "--kubeconfig", str(kubeconfig), check_=False)

    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
