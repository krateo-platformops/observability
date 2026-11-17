#!/usr/bin/env python3
"""Fail if public text carries an internal id or a private repository reference.

This repository is public. It may state SLO targets (500 ms, 2000 ms, 99.9 %, 0.001), but it
must never name an internal work-item id, a decision id (the letter D and a number), the
private repository that defines the SLOs, its issue or PR numbers, its file paths, or the team by
its internal short name. Earlier history is not rewritten; this check keeps new text clean.

Checked:
  - the content of every tracked file (binary files are skipped);
  - every tracked file's path;
  - each file named with --text (a PR title and body, commit messages), as plain text.

A decision-id-shaped token that is something else (a shell variable for a day, a pydocstyle
code, a cluster name) is listed, with its reason, in hack/public-text-allowlist.txt as
`<path> <token> <reason>`. Only that pattern can be allowlisted; the others never can. An entry
that no longer matches anything fails too, so the list cannot go stale.

Usage: public-text-check.py [ROOT] [--text FILE ...]
Exit 1 on any finding.
"""
import pathlib
import re
import subprocess
import sys

# Written with character classes so that this file does not match itself.
PATTERNS = [
    ("internal work-item id", re.compile(r"\bX5\b|\bx5[-_]", re.I)),
    ("private repository name", re.compile(r"krateo-a[c]mp", re.I)),
    ("internal team name", re.compile(r"\ba[c]mp\b", re.I)),
    ("private repository path", re.compile(r"assembl[y]/|sli[s]\.py|target[s]\.yaml", re.I)),
]
DECISION = ("decision id", re.compile(r"\bD[0-9]+\b", re.I))
ALLOWLIST = "hack/public-text-allowlist.txt"


def load_allowlist(root):
    entries = {}
    path = root / ALLOWLIST
    if not path.exists():
        return entries
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            sys.exit(f"{ALLOWLIST}:{n}: needs `<path> <token> <reason>`: {line}")
        entries[(parts[0], parts[1])] = parts[2]
    return entries


def scan(name, text, allow, used, findings, decision=True):
    for lineno, line in enumerate(text.splitlines(), 1):
        for what, rx in PATTERNS + ([DECISION] if decision else []):
            for m in rx.finditer(line):
                if rx is DECISION[1] and (name, m.group(0)) in allow:
                    used.add((name, m.group(0)))
                    continue
                findings.append(f"{name}:{lineno}: {what} {m.group(0)!r}: {line.strip()[:160]}")


def main(argv):
    texts, args = [], []
    it = iter(argv)
    for a in it:
        if a == "--text":
            texts.append(next(it))
        else:
            args.append(a)
    root = pathlib.Path(args[0] if args else ".").resolve()
    allow = load_allowlist(root)
    used, findings = set(), []

    files = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True,
                           capture_output=True).stdout.decode().split("\0")
    files = [f for f in files if f]
    for f in files:
        scan(f"(path) {f}", f, {}, used, findings)
        p = root / f
        if not p.is_file():
            continue
        data = p.read_bytes()
        if b"\0" in data:
            continue
        # The allowlist names the tokens it allows, so the decision-id pattern skips it alone.
        scan(f, data.decode("utf-8", "replace"), allow, used, findings, decision=f != ALLOWLIST)
    for t in texts:
        scan(f"(text) {t}", pathlib.Path(t).read_text(), {}, used, findings)

    for key in sorted(set(allow) - used):
        findings.append(f"{ALLOWLIST}: stale entry, matches nothing: {key[0]} {key[1]}")

    for line in findings:
        print(f"FAIL {line}")
    print(f"public-text-check: {len(files)} tracked files, {len(texts)} text inputs, "
          f"{len(used)} allowlisted tokens, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
