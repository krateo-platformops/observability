#!/usr/bin/env python3
"""hack/chart-tags.py against a throwaway repository and remote; nothing here leaves the machine."""
import os
import pathlib
import subprocess
import sys
import tempfile

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "hack" / "chart-tags.py"
fails = 0


def git(cwd, *args, ok=True, date=None):
    # Between annotated tags on one commit `git describe` takes the newer, by the second.
    env = {**os.environ, **({"GIT_COMMITTER_DATE": date} if date else {})}
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "tag.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if ok and r.returncode:
        sys.exit(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def run(cwd, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def check(what, ok, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} {what} {detail if not ok else ''}")


with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    git(tmp, "init", "-q", "--bare", "origin.git")
    git(tmp, "clone", "-q", "origin.git", "w")
    w = tmp / "w"

    def release(tag, **versions):
        for n, v in versions.items():
            d = w / "charts" / n
            d.mkdir(parents=True, exist_ok=True)
            (d / "Chart.yaml").write_text(f"apiVersion: v2\nname: {n}\n# a comment with version: 9.9.9\nversion: {v}\nappVersion: \"1.0.0\"\n")
        (w / "n").write_text(tag)
        git(w, "add", "-A")
        git(w, "commit", "-q", "-m", tag)
        git(w, "tag", "-a", tag, "-m", tag, date="2026-01-01T00:00:00Z")
        git(w, "push", "-q", "origin", "HEAD:refs/heads/main", tag)
        return git(w, "rev-parse", "HEAD")

    remote = lambda: dict(reversed(x.split("\t")) for x in git(w, "ls-remote", "--tags", "origin").splitlines())
    a = release("3.1.1", one="0.1.0", two="0.2.0")
    rc, out = run(w, "--sha", a)
    check("without --apply nothing is created", rc == 0 and "would create one-0.1.0" in out and "refs/tags/one-0.1.0" not in remote(), out)
    rc, out = run(w, "--sha", a, "--apply")
    check("both charts are tagged at the released commit", rc == 0 and remote().get("refs/tags/one-0.1.0") == a and remote().get("refs/tags/two-0.2.0") == a, out)
    check("the tags are lightweight", "refs/tags/one-0.1.0^{}" not in remote())
    b = release("3.1.2", one="0.1.1")
    rc, out = run(w, "--sha", b, "--apply")
    check("a new version is tagged, an unchanged one is not moved",
          rc == 0 and remote().get("refs/tags/one-0.1.1") == b and remote().get("refs/tags/two-0.2.0") == a and "keep    two-0.2.0" in out, out)
    rc, out = run(w, "--sha", b, "--apply")
    check("a second run changes nothing", rc == 0 and "created" not in out, out)
    git(w, "fetch", "-q", "--tags")
    c = release("3.1.3", two="0.3.0")
    got = git(w, "describe", "--tags", "--abbrev=0", "--exclude=3.1.3", "3.1.3^")
    check("release-notes' base is still the previous 3.1.N tag", got == "3.1.2", got)
    git(w, "tag", "-a", "zz-annotated", "-m", "x", b, date="2026-01-02T00:00:00Z")
    d = git(w, "describe", "--tags", "--abbrev=0", "--exclude=3.1.3", "3.1.3^")
    check("CONTROL an annotated chart tag could take its place", d == "zz-annotated", d)
    bad = release("3.1.4", one="CHART_VERSION")
    rc, out = run(w, "--sha", bad, "--apply")
    check("a version that is not N.N.N is refused", rc != 0 and "expected name" in out, out)
print(f"{fails} failed")
sys.exit(1 if fails else 0)
