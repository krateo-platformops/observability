#!/usr/bin/env python3
"""Fail if public text carries an internal id or a private repository reference.

This repository is public. It may state SLO targets (500 ms, 2000 ms, 99.9 %, 0.001), but it
must never name an internal work-item or plan id of any kind (a tracker key, a plan step code, a
numbered lockdown or phase), a decision id (the letter D and a number), the private repository
that defines the SLOs, its issue or PR numbers, its file paths, or the team by its internal short
name. Say what the work is instead. Public GitHub references (installer#406, helm/helm#32132)
are fine. Earlier history is not rewritten; this check keeps new text clean.

Checked:
  - the content of every tracked file (binary files are skipped);
  - every tracked file's path;
  - each file named with --text (a PR title and body, commit messages), as plain text.

The id-shaped patterns also match real technical tokens. Two exemptions, both explicit:
  - STANDARD, in this file: token families that are always technical (a CVE or RFC number, a
    character encoding, a hash or cipher name), each with its reason. They pass anywhere.
  - hack/public-text-allowlist.txt: one token in one file, as `<path> <token> <reason>` (a shell
    variable for a day, a test's time variable, a pydocstyle code, a log level). An entry that
    no longer matches anything fails, so the list cannot go stale.
Only the id-shaped patterns can be exempted; the private-repository and team patterns never can,
and nothing in --text input or a file path can be allowlisted.

Planted control: before anything is read, the same matcher runs over a string that is known to
leak (a decision id, a plan step id and the private repository name). The string is built at
run time, so this repository never contains it. If the matcher finds nothing in it, the
matcher is broken and a clean result on the real files would mean nothing: exit 2.
tests/public-text-check.py proves the control fires when the matcher is stubbed out.

Usage: public-text-check.py [ROOT] [--text FILE ...]
Exit 1 on any finding, 2 if the planted control was not caught.
"""
import pathlib
import re
import sys
import subprocess

# Written with character classes and \d so that this file does not match itself.
PATTERNS = [
    ("internal work-item id", re.compile(r"\bX5\b|\bx5[-_]", re.I)),
    ("private repository name", re.compile(r"krateo-a[c]mp", re.I)),
    ("internal team name", re.compile(r"\ba[c]mp\b", re.I)),
    ("private repository path", re.compile(r"assembl[y]/|sli[s]\.py|target[s]\.yaml", re.I)),
]
# Id-shaped patterns: a match fails unless STANDARD or the allowlist exempts it.
ALLOWABLE = [
    # A decision id: the letter D, a number and at most one letter, in any case.
    ("decision id", re.compile(r"\bD\d+[a-z]?\b", re.I | re.A)),
    # A tracker key: 2-10 capitals and digits, a dash, a number (a project key and item number).
    ("work-item id", re.compile(r"\b[A-Z][A-Z\d]{1,9}-\d+\b", re.A)),
    # A plan step or finding code: one capital and one or two digits (a step, a finding).
    # D is left to the decision-id pattern.
    ("plan step id", re.compile(r"\b[A-CE-Z]\d{1,2}\b", re.A)),
    # A numbered plan stage. `step` is left out on purpose: test sections, runbooks and load
    # profiles number their steps ("step 2: ClickHouse", "Step 1: Create ..."), and a coded step
    # is caught by the plan-step pattern above.
    ("numbered plan stage", re.compile(
        r"\b(?:lockdown|wave|phase|stage|sprint|milestone|epic|ticket|work[- ]item)\s+#?[A-Z]?\d+[a-z]?\b",
        re.I | re.A)),
]
# Token families that are always technical, by the part before the first dash, or whole.
STANDARD = {
    "CVE": "a public vulnerability id (CVE-2026-1234)",
    "CWE": "a public weakness class id",
    "RFC": "an IETF standard number (RFC-7231)",
    "ISO": "an ISO standard number (ISO-8601)",
    "IEC": "an IEC standard number",
    "ECMA": "an Ecma standard number",
    "PEP": "a Python enhancement proposal",
    "UTF": "a character encoding (UTF-8)",
    "SHA": "a hash function (SHA-1, SHA-256)",
    "AES": "a cipher (AES-256)",
    "S3": "the object storage service and its API",
}
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


def standard(token):
    return token.split("-", 1)[0] in STANDARD


def scan(name, text, allow, used, findings, listed=frozenset()):
    """`listed`: tokens that pass without being marked used (the allowlist's own tokens, which
    its entries and reasons name)."""
    for lineno, line in enumerate(text.splitlines(), 1):
        for what, rx in PATTERNS:
            for m in rx.finditer(line):
                findings.append(f"{name}:{lineno}: {what} {m.group(0)!r}: {line.strip()[:160]}")
        for what, rx in ALLOWABLE:
            for m in rx.finditer(line):
                tok = m.group(0)
                if standard(tok) or tok in listed:
                    continue
                if (name, tok) in allow:
                    used.add((name, tok))
                    continue
                findings.append(f"{name}:{lineno}: {what} {tok!r}: {line.strip()[:160]}")


def planted_leak():
    """A string that must produce findings, assembled here so no file in the repository holds it."""
    return "decided in " + "D" + "3" + " for " + "X" + "5" + ", see " + "krateo-a" + "cmp" + "\n"


def control_findings():
    findings = []
    scan("(planted control)", planted_leak(), {}, set(), findings)
    return findings


def main(argv):
    found = control_findings()
    if not found:
        print("public-text-check: the check found nothing in a planted leak: the matcher is broken")
        return 2
    print(f"public-text-check: planted control caught ({len(found)} findings in a known leak)")
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
        # The allowlist names the tokens it allows, so in it alone those tokens pass. Its paths,
        # reasons and comments are public text like any other, and any other id in them fails.
        listed = frozenset(tok for _, tok in allow) if f == ALLOWLIST else frozenset()
        scan(f, data.decode("utf-8", "replace"), allow, used, findings, listed)
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
