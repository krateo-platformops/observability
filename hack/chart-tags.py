#!/usr/bin/env python3
"""One lightweight git tag per chart version, so a deployed chart version is a ref of this repository.

usage: hack/chart-tags.py --sha <commit> [--apply]

The repository is released as 3.1.N while each chart has its own version, so nothing here used to
answer "what does otel-collector-daemonset 0.1.38 look like?" with a ref. A release run calls this
after its charts are published: for each chart in charts/, `<chart>-<version>` is created at the
released commit unless it exists. An existing tag is never moved: it marks the first release that had
that version. release-oci publishes an unchanged version only if its contents equal what is already
published, so tagging it at a later commit still points at the same chart.

Lightweight on purpose. release-notes takes its base from `git describe`, which prefers an annotated
tag on the same commit, and the release tags are annotated (tests/chart-tags.py checks the rule).

Without --apply it only prints what it would create.
"""
import argparse
import pathlib
import re
import subprocess
import sys

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def field(text, key):
    m = re.search(rf"^{key}:\s*[\"']?([^\"'\s#]+)", text, re.M)
    return m.group(1) if m else ""


def charts(sha):
    out = subprocess.run(["git", "ls-tree", "--name-only", f"{sha}:charts"], check=True, capture_output=True, text=True).stdout
    for d in out.split():
        text = subprocess.run(["git", "show", f"{sha}:charts/{d}/Chart.yaml"], check=True, capture_output=True, text=True).stdout
        name, version = field(text, "name"), field(text, "version")
        if name != d or not NAME.match(name) or not VERSION.match(version):
            sys.exit(f"charts/{d}/Chart.yaml: name {name!r}, version {version!r}: expected name {d!r} and a plain N.N.N version")
        yield f"{name}-{version}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sha", required=True)
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    sha = subprocess.run(["git", "rev-parse", f"{a.sha}^{{commit}}"], check=True, capture_output=True, text=True).stdout.strip()
    remote = subprocess.run(["git", "ls-remote", "--tags", "origin"], check=True, capture_output=True, text=True).stdout
    have = {line.split("\t")[1][len("refs/tags/"):] for line in remote.splitlines()}
    if not have:
        sys.exit("git ls-remote returned no tags at all: refusing to conclude that none exist")
    for tag in charts(sha):
        if tag in have:
            print(f"keep    {tag}")
        elif a.apply:
            subprocess.run(["git", "push", "--quiet", "origin", f"{sha}:refs/tags/{tag}"], check=True)
            print(f"created {tag} at {sha[:12]}")
        else:
            print(f"would create {tag} at {sha[:12]}")


if __name__ == "__main__":
    main()
