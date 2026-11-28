#!/usr/bin/env python3
"""Resolve every chart and image reference in a repository. Fail if any does not exist.

Four separate outages in this platform were the same defect: a pinned chart or image that
resolved to nothing. Each surfaced 30-45 minutes into a live cluster rebuild, as an opaque
ImagePullBackOff or a composition that reported Ready=True while deploying nothing. Each
would have taken seconds to catch here.

Deliberately regex-driven, not YAML-parsed: most of these files are Helm templates, so they
are not valid YAML, and the references live in printf blocks and doc comments as often as in
values. Matching text finds them all.

Exit 1 if any reference is unresolvable.
"""
import base64, json, os, re, sys, urllib.error, urllib.request

TIMEOUT = 25

# oci://host/path/chart  — the chart's version is looked for nearby (see pair_version)
OCI = re.compile(r'oci://([a-z0-9.-]+)/([a-zA-Z0-9._/-]+)')
# host/path:tag — an image. Requires a dot in the host so `foo/bar:baz` in prose is skipped.
IMG = re.compile(r'\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)/([a-zA-Z0-9._/-]+):([a-zA-Z0-9._-]+)\b')
VERSION_NEAR = re.compile(r'version:\s*"?([0-9]+\.[0-9]+\.[0-9]+[^"\s]*)"?')

# Anything templated, placeholder, or obviously not a literal.
#   \.\.\.  -- `ghcr.io/.../name:1.2.3` is the usual way docs and comments elide an org.
#             It parses as a perfectly good reference and is never a real one.
SKIP = re.compile(r'\{\{|\$\{|%[sqvd]|CHART_VERSION|VERSION|<[a-zA-Z]|x\.y\.z|\.\.\.|latest$')



# RFC 6761/2606 reserved TLDs + conventional placeholder orgs never resolve and are never
# real deployment refs — they are documentation. Skipping them is not laxity: a check that
# flags `kubernetes.example/hyperkube` in vendored k8s type defs, or `ghcr.io/example/x` in a
# test fixture, cries wolf and gets disabled.
RESERVED_TLDS = (".example", ".invalid", ".test", ".localhost")
PLACEHOLDER_ORGS = ("example", "test", "sample", "your-org", "myorg")

def is_placeholder_ref(host, path):
    if any(host == "example" or host.endswith(t) for t in RESERVED_TLDS):
        return True
    seg = path.split("/", 1)[0] if path else ""
    return seg in PLACEHOLDER_ORGS


def ghcr_token(repo):
    url = "https://%s/token?scope=repository:%s:pull" % ("ghcr.io", repo)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.load(r).get("token", "")
    except Exception:
        return ""


def registry_get(host, repo, ref):
    """HEAD-ish GET of a manifest, ANONYMOUSLY. Returns HTTP status.

    401 counts as a failure and that is deliberate: the child's kubelet and core-provider
    pull without credentials, so "exists but needs auth" is indistinguishable from "gone"
    at the point where it matters. A private package fails provisioning exactly like a
    missing one.
    """
    if host == "ghcr.io":
        tok = ghcr_token(repo)
        auth = {"Authorization": "Bearer " + tok} if tok else {}
        base = "https://ghcr.io/v2"
    elif host in ("docker.io", "registry-1.docker.io", "docker.hyperdx.io"):
        # docker.hyperdx.io is a vanity name for Docker Hub: its /v2/ answers 307 to
        # registry-1.docker.io and its 401 challenge points at auth.docker.io. Without this
        # branch it is queried without a token and every hyperdx image reads as unresolvable.
        # Docker Hub official images ("rabbitmq", "nginx") live under library/. Querying
        # the bare name returns 401, which would flag every official image as broken —
        # the exact cry-wolf failure that gets a check disabled.
        if "/" not in repo:
            repo = "library/" + repo
        try:
            u = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:%s:pull" % repo
            with urllib.request.urlopen(u, timeout=TIMEOUT) as r:
                tok = json.load(r)["token"]
        except Exception:
            tok = ""
        auth = {"Authorization": "Bearer " + tok} if tok else {}
        base = "https://registry-1.docker.io/v2"
    elif host == "quay.io":
        # quay's /v2 manifest endpoint returns 401 for an ABSENT tag as often as 404, which is
        # indistinguishable from "needs auth". Its tag API is authoritative and anonymous, so
        # ask it directly: a tag either is in the active-tag list or it is not.
        try:
            u = "https://quay.io/api/v1/repository/%s/tag/?specificTag=%s&onlyActiveTags=true" % (repo, ref)
            with urllib.request.urlopen(u, timeout=TIMEOUT) as r:
                return 200 if json.load(r).get("tags") else 404
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return 0
    else:  # other anonymous-friendly registries
        auth = {}
        base = "https://%s/v2" % host

    hdrs = dict(auth)
    hdrs["Accept"] = ",".join([
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ])
    req = urllib.request.Request("%s/%s/manifests/%s" % (base, repo, ref), headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


NAME_NEAR = re.compile(r'^\s*(?:-\s+)?(?:chart|name):\s*"?([a-z0-9][a-z0-9._-]*)"?\s*$')


def pair_name(lines, idx):
    """Find the chart NAME declared near an oci:// line.

    Several repos pin `repo: oci://host/org/charts` with the chart name in a separate
    `chart:` or `name:` key. Querying the repo root instead of the chart is meaningless —
    it fails for every pin, correct or not, which would make this check pure noise.
    """
    for off in (0, 1, 2, -1, -2, 3, -3):
        j = idx + off
        if 0 <= j < len(lines):
            m = NAME_NEAR.match(lines[j])
            if m:
                return m.group(1)
    return None


def pair_version(lines, idx):
    """Find the chart version declared near an oci:// line.

    Charts are pinned in several shapes across these repos — {url, version},
    {repo, chart, version}, and printf blocks where the two are lines apart — so look in a
    small window both ways rather than assuming one layout.
    """
    for off in (0, 1, 2, -1, -2, 3, -3):
        j = idx + off
        if 0 <= j < len(lines):
            m = VERSION_NEAR.search(lines[j])
            if m and not SKIP.search(m.group(1)):
                return m.group(1)
    return None


REPO_LINE = re.compile(r'^\s*repository:\s*["\']?([a-zA-Z0-9][a-zA-Z0-9._/-]*)["\']?\s*(?:#.*)?$')
TAG_LINE = re.compile(r'^\s*tag:\s*["\']?([a-zA-Z0-9][a-zA-Z0-9._-]*)["\']?\s*(?:#.*)?$')
REG_LINE = re.compile(r'^\s*registry:\s*["\']?([a-zA-Z0-9._-]*)["\']?\s*(?:#.*)?$')


def split_image(lines, idx):
    """Pair a `repository:` line with its neighbouring `tag:` (and optional `registry:`).

    This is the DOMINANT way Helm charts declare images:

        image:
          repository: ghcr.io/org/name
          tag: "1.2.3"

    The single-line matcher never sees these, which is not a corner case — it is most
    charts. It cost a real miss: nutanix-v4-proxy referenced
    ghcr.io/krateo-blueprints/nutanix-v4-proxy, an image that does not exist, and the
    check reported "all references resolve" because it found nothing to check.
    """
    m = REPO_LINE.match(lines[idx])
    if not m:
        return None
    repo = m.group(1)
    if repo.startswith("oci://") or SKIP.search(lines[idx]):
        return None            # chart dependency, handled by the OCI matcher
    tag = registry = None
    for off in (1, 2, 3, -1, -2, -3, 4, -4):
        j = idx + off
        if not (0 <= j < len(lines)):
            continue
        if tag is None:
            t = TAG_LINE.match(lines[j])
            if t and not SKIP.search(t.group(1)):
                tag = t.group(1)
        if registry is None:
            r = REG_LINE.match(lines[j])
            if r is not None:
                registry = r.group(1)
    if not tag:
        return None                     # no resolvable tag -> cannot check, skip
    if registry:
        host, path = registry, repo
    elif "/" in repo and "." in repo.split("/")[0]:
        host, path = repo.split("/", 1)  # repository already carries the host
    else:
        host, path = "docker.io", repo   # bare name -> Docker Hub (library/ added later)
    return host, path, tag


# The denominator. A run that examines almost nothing must not print "all references
# resolve": this script once did exactly that, pruning charts/ and seeing 3 chart refs and 0
# image refs while the real image pins live in charts/*/values.yaml.
# Measured 2026-11-28 (python3 hack/preflight-refs.py --list .): 4 chart refs, 4 image refs.
# Floor = measured minus ~20%, rounded down. Raise it when the count grows; lower it only
# with a reason.
MIN_CHARTS = 3
MIN_IMAGES = 3

# Directories the scan skips, each with the reason. Everything else is scanned, charts/ included.
#   .git, node_modules      not ours: VCS metadata and installed JS dependencies.
#   charts/<name>/charts/   vendored subcharts (`helm dependency build`): upstream's refs, which
#                           we do not pin and which are not ours to fix. Detected by path shape,
#                           not by the name "charts", so charts/ itself is scanned.
#   testdata/, test/, tests/  fixtures: refs there are deliberately fake or illustrative.
#                           Checked 2026-11-28: skipping them changes no count.
# (ops/ and docs/design/ were skipped before; no justification survives -- docs/design/ does
# not exist, and the refs under ops/ are deployable harness refs. Scanning them changed no
# count on 2026-11-28, so they are no longer skipped.) Packaged *.tgz are never read: only
# .yaml/.yml/.tpl/.json files are opened.
PRUNE_NAMES = (".git", "node_modules")
FIXTURE_SEGS = ("testdata/", "test/", "tests/")


def is_vendored_chart_dir(root, dirpath, name):
    """True for charts/<chart>/charts, the helm dependency directory."""
    rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/").split("/")
    return len(rel) >= 3 and rel[-1] == "charts" and rel[-3] == "charts"


def is_fixture_dir(rel_dir):
    return any(("/" + rel_dir).find("/" + seg) != -1 for seg in FIXTURE_SEGS)


def scan(root, skipped=None):
    charts, images = set(), set()
    skipped = skipped if skipped is not None else set()
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            full = os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/")
            if d in PRUNE_NAMES or is_vendored_chart_dir(root, dirpath, d):
                skipped.add(full + "/")
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if not fn.endswith((".yaml", ".yml", ".tpl", ".json")):
                continue
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/") + "/"
            if is_fixture_dir(rel_dir):
                skipped.add(rel_dir)
                continue
            p = os.path.join(dirpath, fn)
            try:
                lines = open(p, encoding="utf-8", errors="replace").read().split("\n")
            except Exception:
                continue
            rel = os.path.relpath(p, root)
            for i, line in enumerate(lines):
                for m in OCI.finditer(line):
                    host, path = m.group(1), m.group(2).rstrip('"\'/,')
                    if SKIP.search(m.group(0)):
                        continue
                    ver = pair_version(lines, i)
                    # `oci://host/org/charts` is a repo root, not a chart. Append the
                    # chart name from a neighbouring key when the path clearly lacks one.
                    if path.rsplit("/", 1)[-1] in ("charts", "chart"):
                        nm = pair_name(lines, i)
                        if nm:
                            path = path + "/" + nm
                        else:
                            continue  # repo root with no resolvable chart name — not a ref
                    elif "/" not in path:
                        # `repository: oci://ghcr.io/clickhouse` in a Chart.yaml dependency: the
                        # path is an org, the chart is the neighbouring `name:`. Querying the org
                        # as if it were a chart is a false 401 (seen 2026-11-28).
                        nm = pair_name(lines, i)
                        if not nm:
                            continue
                        path = path + "/" + nm
                    if ver and not is_placeholder_ref(host, path):
                        charts.add((host, path, ver, rel, i + 1))
                sp = split_image(lines, i)
                if sp and not is_placeholder_ref(sp[0], sp[1]):
                    images.add((sp[0], sp[1], sp[2], rel, i + 1))
                for m in IMG.finditer(line):
                    host, path, tag = m.group(1), m.group(2), m.group(3)
                    if SKIP.search(m.group(0)) or host.endswith((".krateo.dev", ".svc")):
                        continue
                    # A web link, not an image. `https://wiki.xen.org/wiki/Bus:Device.Function`
                    # in an OpenAPI description matches host/path:tag exactly, and these repos
                    # vendor tens of thousands of lines of upstream specs full of such links.
                    # No image reference is ever written with a scheme, so this discriminates
                    # precisely -- unlike skipping prose keys, which would also have to guess
                    # at block scalars and would start losing real references.
                    if line[:m.start()].rstrip().endswith("//"):
                        continue
                    if "/" not in path and host.count(".") < 1:
                        continue
                    if is_placeholder_ref(host, path):
                        continue
                    images.add((host, path, tag, rel, i + 1))
    return charts, images


def floor_errors(charts, images):
    errs = []
    if len(charts) < MIN_CHARTS:
        errs.append("examined %d chart refs, expected at least %d" % (len(charts), MIN_CHARTS))
    if len(images) < MIN_IMAGES:
        errs.append("examined %d image refs, expected at least %d" % (len(images), MIN_IMAGES))
    return errs


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    list_only = "--list" in sys.argv[1:]   # no network; used by tests/preflight-refs.py only
    root = args[0] if args else "."
    skipped = set()
    charts, images = scan(root, skipped)
    print("skipped directories (%d): %s" % (len(skipped), ", ".join(sorted(skipped)) or "none"))
    errs = floor_errors(charts, images)
    if errs:
        for e in errs:
            print("FAIL: " + e)
        print("The scan examined too little to say anything: a green result here would mean nothing.")
        return 1
    if list_only:
        print("chart references (%d)" % len(charts))
        for host, path, ver, rel, ln in sorted(charts):
            print("  %s/%s:%s   (%s:%d)" % (host, path, ver, rel, ln))
        print("image references (%d)" % len(images))
        for host, path, tag, rel, ln in sorted(images):
            print("  %s/%s:%s   (%s:%d)" % (host, path, tag, rel, ln))
        return 0
    bad = []

    print("chart references (%d)" % len(charts))
    for host, path, ver, rel, ln in sorted(charts):
        code = registry_get(host, path, ver)
        ok = code == 200
        print("  %-4s %s/%s:%s   (%s:%d)" % ("OK" if ok else "FAIL", host, path, ver, rel, ln))
        if not ok:
            bad.append(("chart", "%s/%s:%s" % (host, path, ver), rel, ln, code))

    print()
    print("image references (%d)" % len(images))
    for host, path, tag, rel, ln in sorted(images):
        code = registry_get(host, path, tag)
        ok = code == 200
        print("  %-4s %s/%s:%s   (%s:%d)" % ("OK" if ok else "FAIL", host, path, tag, rel, ln))
        if not ok:
            bad.append(("image", "%s/%s:%s" % (host, path, tag), rel, ln, code))

    print()
    if bad:
        print("UNRESOLVABLE REFERENCES: %d" % len(bad))
        for kind, ref, rel, ln, code in bad:
            print("  %-6s %s  -> HTTP %s   %s:%d" % (kind, ref, code, rel, ln))
        print()
        print("A reference that does not resolve here fails at provisioning time instead,")
        print("as an ImagePullBackOff or a composition that reports Ready=True and deploys nothing.")
        return 1
    print("all references resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
