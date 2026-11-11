#!/usr/bin/env python3
"""Functional test of otel-collector-daemonset's gateway audit projection (AUDIT-4, observability#138).

The input is what the REAL proxy writes: tests/fixtures/gateway-access-log.otlp.jsonl was produced by
agentgateway v1.5.0 running agentgateway-policies' rendered access-log CEL
(krateo-agentiko/agentgateway-policies hack/accesslog-facts-docker.py --keep), with JWTs minted at
run time: plain, delegated two deep, an actor with no class, legacy (no `principal`), a 0xFF
x-forwarded-for, a JWT in baggage. Its mcp.session.id values were replaced by a placeholder (the
proxy's are base64 session JSON, which the security scan's JWT rule would flag); the projection
never writes that key. Synthetic variants are DERIVED from those records here (a denied hop, a
backend failure, an unknown MCP method, a deep chain, a long user agent, an _OTHER method).

The chart is rendered and the gateway-audit pipelines run exactly as rendered, with two changes for
Docker: the clickhouse exporters become file exporters, and k8sattributes (which needs the API
server) is left out of the pipelines that have it. The records are sent to the collector over
OTLP/HTTP, and the checks run on what it writes:
  * every projected record passes `auditschema check -writer gateway` (krateo-platformops/audit
    v0.5.0, the library's own checker);
  * which records are projected: the acting hops, and an MCP method on neither list; and which
    are excluded and COUNTED (krateo.gateway_audit.excluded, per protocol and method);
  * each mapping: actor, class, delegation chain ending with the subject's class, outcome, target,
    source_ips, user agent cut, incomplete markers in the library's order;
  * the closed set: no access-log key survives, and no bearer token appears anywhere;
  * a falsifier: removing each input a required key is built from makes auditschema report it.

Run: python3 tests/gateway-audit-docker.py [repo-root]
Needs docker, helm, go (to build auditschema, or AUDITSCHEMA=/path/to/binary). Exit 0 = all passed.
"""
import copy
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

import yaml

CHART = "charts/otel-collector-daemonset"
AUDIT_LIB = "github.com/krateo-platformops/audit/cmd/auditschema@v0.5.0"
SCOPE = "github.com/krateo-platformops/observability/gateway-audit"
PIPES = ["logs/gateway-audit", "logs/gateway-audit-excluded", "metrics/gateway-audit-excluded"]
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def sh(*args, check=True, **kw):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, **kw)


def render(root):
    sh("helm", "dependency", "build", str(root / CHART))
    out = sh("helm", "template", "gate", str(root / CHART), "-n", "krateo-system").stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    return yaml.safe_load(cm["data"]["relay"]), ds["spec"]["template"]["spec"]["containers"][0]["image"]


def test_config(relay):
    svc = relay["service"]["pipelines"]
    for p in PIPES:
        if p not in svc:
            raise SystemExit(f"rendered config has no pipeline {p}")
    pipes = {p: copy.deepcopy(svc[p]) for p in PIPES}
    used_proc = {x for p in pipes.values() for x in p.get("processors", [])} - {"k8sattributes"}
    for p in pipes.values():
        p["processors"] = [x for x in p.get("processors", []) if x != "k8sattributes"]
        p["receivers"] = ["otlp" if r == "otlp" else r for r in p["receivers"]]
        p["exporters"] = [{"clickhouse/audit": "file/audit", "clickhouse": "file/metrics"}.get(e, e) for e in p["exporters"]]
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {k: v for k, v in relay["processors"].items() if k in used_proc},
        "connectors": {k: v for k, v in relay["connectors"].items() if k == "count/gateway-audit-excluded"},
        "exporters": {"file/audit": {"path": "/out/audit.jsonl", "flush_interval": "1s"},
                      "file/metrics": {"path": "/out/metrics.jsonl", "flush_interval": "1s"}},
        "service": {"pipelines": pipes},
    }


# ------------------------------------------------------------------------------------- records --
def attrs_of(lr):
    return {a["key"]: (next(iter(a["value"].values())) if a["value"] else None) for a in lr.get("attributes", [])}


def set_attr(lr, key, value):
    lr["attributes"] = [a for a in lr["attributes"] if a["key"] != key]
    if value is not None:
        lr["attributes"].append({"key": key, "value": {"stringValue": value} if isinstance(value, str) else {"intValue": str(value)}})


def fixtures(root):
    reqs = [json.loads(l) for l in (root / "tests/fixtures/gateway-access-log.otlp.jsonl").read_text().splitlines() if l.strip()]
    # One request per record, each tagged with a test id (attribute `t`, dropped by keep_keys) so
    # the checks can find each record's projection, whatever order the collector writes them in.
    out = []
    for req in reqs:
        for rl in req["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for lr in sl["logRecords"]:
                    out.append((rl["resource"], sl["scope"], lr))
    return out


def wrap(resource, scope, lr):
    return {"resourceLogs": [{"resource": resource, "scopeLogs": [{"scope": scope, "logRecords": [lr]}]}]}


def build_cases(base):
    cases = []

    def add(tid, rec, expect):
        res, sc, lr = rec
        lr = copy.deepcopy(lr)
        set_attr(lr, "t", tid)
        cases.append((tid, wrap(res, sc, lr), expect))
        return lr

    def find(pred):
        for r in base:
            a = attrs_of(r[2])
            if pred(a):
                return r
        raise SystemExit("fixture lacks a record the test needs")

    excluded_mcp = ("initialize", "ping", "tools/list", "resources/list", "resources/templates/list", "prompts/list",
                    "completion/complete", "logging/setLevel")
    for i, r in enumerate(base):
        a = attrs_of(r[2])
        proto, mm, hm = a.get("protocol"), a.get("mcp.method.name"), a.get("http.method")
        if not a.get("route"):
            want = "excluded"  # matched no route, whatever its protocol (the real record is `http`)
        elif proto == "mcp":
            plumbing = (mm and (mm in excluded_mcp or mm.startswith("notifications/"))) or (not mm and hm in ("GET", "DELETE"))
            want = "excluded" if plumbing else "projected"  # incl. a POST with no parsable method
        elif proto == "a2a":
            want = "projected" if a.get("a2a.method") else "excluded"
        elif proto == "llm":
            want = "excluded" if hm == "GET" else "projected"
        else:
            want = "ignored"
        add(f"real-{i}", r, want)

    tc = find(lambda a: a.get("mcp.method.name") == "tools/call" and a.get("principal") == "human" and "act" not in a)
    lr = add("unknown-mcp-method", tc, "projected"); set_attr(lr, "mcp.method.name", "tools/frobnicate")
    a2a = find(lambda a: a.get("protocol") == "a2a" and a.get("principal") == "human" and "act" not in a and a.get("baggage_session"))
    lr = add("denied-authz", a2a, "projected"); set_attr(lr, "reason", "Authorization"); set_attr(lr, "http.status", 403)
    llm = find(lambda a: a.get("protocol") == "llm" and a.get("principal") == "human" and "act" not in a)
    lr = add("denied-ratelimit", llm, "projected"); set_attr(lr, "reason", "RateLimit"); set_attr(lr, "http.status", 429)
    lr = add("backend-500", a2a, "projected"); set_attr(lr, "http.status", 500)
    lr = add("gateway-failure", a2a, "projected"); set_attr(lr, "reason", "UpstreamFailure"); set_attr(lr, "http.status", 503)
    lr = add("long-ua", a2a, "projected"); set_attr(lr, "user_agent", "x" * 1500)
    lr = add("method-other", a2a, "projected"); set_attr(lr, "http.method", "PROPFIND")
    dele = find(lambda a: a.get("protocol") == "a2a" and a.get("principal") == "agent" and a.get("subject_principal"))
    lr = add("deep-chain", dele, "projected")
    set_attr(lr, "act", json.dumps({"sub": "sa:one", "principal": "agent", "act": {"sub": "sa:two", "principal": "service",
                                    "act": {"sub": "sa:three", "act": {"sub": "sa:four", "principal": "superuser"}}}}))
    # Falsifier: remove the input each required G key is built from.
    for tid, keys in (("drop-method", ["http.method"]), ("drop-target", ["endpoint", "backend_host"]),
                      ("drop-user", ["user"]), ("drop-src", ["src.addr"])):
        lr = add(tid, a2a, "projected")
        for key in keys:
            set_attr(lr, key, None)
    # A refused-before-dial hop: no endpoint, so server.address is the route's configured target.
    lr = add("refused-before-dial", a2a, "projected"); set_attr(lr, "endpoint", None)
    set_attr(lr, "reason", "JwtAuth"); set_attr(lr, "http.status", 401)

    return cases


# ----------------------------------------------------------------------------------- auditschema --
def auditschema():
    if os.environ.get("AUDITSCHEMA"):
        return os.environ["AUDITSCHEMA"]
    d = pathlib.Path(tempfile.mkdtemp(prefix="auditschema-"))
    sh("go", "install", AUDIT_LIB, env={**os.environ, "GOBIN": str(d)})
    return str(d / "auditschema")


def run_check(binary, record, work, name):
    f = work / f"{name}.json"
    f.write_text(json.dumps(record))
    r = sh(binary, "check", "-writer", "gateway", str(f), check=False)
    return r.returncode, r.stdout + r.stderr


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, image = render(root)
    cfg = test_config(relay)
    binary = auditschema()
    base = fixtures(root)
    cases = build_cases(base)
    work = pathlib.Path(tempfile.mkdtemp(prefix="gwaudit-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir(); os.chmod(work / "out", 0o777)
    (work / "otel.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False)); os.chmod(work / "otel.yaml", 0o644)
    name = "gwaudit-" + uuid.uuid4().hex[:8]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    try:
        sh("docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:4318",
           "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/otel.yaml")
        up = False
        for _ in range(120):
            try:
                urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/v1/logs", data=b"{}",
                                       headers={"content-type": "application/json"}), timeout=2).read()
                up = True; break
            except urllib.error.HTTPError:
                up = True; break
            except OSError:
                time.sleep(0.5)
        if not up:
            raise SystemExit("collector never answered:\n" + sh("docker", "logs", "--tail", "60", name, check=False).stderr[-4000:])
        for tid, req, _ in cases:
            r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/logs", data=json.dumps(req).encode(),
                                       headers={"content-type": "application/json"}, method="POST")
            urllib.request.urlopen(r, timeout=10).read()
        time.sleep(8)
        logs = sh("docker", "logs", name, check=False).stderr
        check("error" not in logs.lower() or "Everything is ready" in logs, "collector started the gateway-audit pipelines")
        verify(work, cases, binary, logs)
    finally:
        if os.environ.get("SHOW_LOGS") == "1":
            print(sh("docker", "logs", "--tail", "80", name, check=False).stderr[-6000:])
        sh("docker", "rm", "-f", name, check=False)
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


def read_out(path):
    recs = []
    if not path.exists():
        return recs
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        for rl in json.loads(line).get("resourceLogs", []):
            for sl in rl.get("scopeLogs", []):
                for lr in sl.get("logRecords", []):
                    recs.append((rl.get("resource", {}), sl.get("scope", {}), lr))
    return recs


def verify(work, cases, binary, logs):
    out = read_out(work / "out" / "audit.jsonl")
    # The test id `t` is removed by keep_keys, so projected records are matched to their case by
    # the order-free fact that each case is sent as its own request: map by observed timestamp.
    by_ts = {}
    for res, sc, lr in out:
        by_ts.setdefault(lr.get("timeUnixNano"), []).append((res, sc, lr))
    expected = [c for c in cases if c[2] == "projected"]
    check(len(out) == len(expected), f"exactly the projected hops reach krateo_audit ({len(out)} written, {len(expected)} expected)")
    # 1. the library's checker on every projected record
    # Every record passes, incomplete ones included (a marked gap is conformant); the ONLY record
    # allowed to break is the falsifier whose route was removed, checked separately below.
    bad = []
    for i, (res, sc, lr) in enumerate(out):
        a = attrs_of(lr)
        if "server.address" not in a:
            continue  # the target falsifier: checked below
        code, msg = run_check(binary, wrap(res, sc, lr), work, f"rec{i}")
        # The checker REPORTS a marked gap (`<key>: incomplete`, exit 1): that is the schema's own
        # verdict for an honest gap, and those records are the legacy-token and falsifier cases.
        # Any other reason (missing, unexpected, bad_format, outside the closed set...) is a defect.
        reasons = [l for l in msg.splitlines() if ": record " in l and not l.rstrip().endswith(": incomplete")]
        if code == 2 or reasons:
            bad.append(reasons[0] if reasons else msg.strip()[:200])
        if code == 1 and not a.get("krateo.audit.incomplete"):
            bad.append(f"rec{i}: exit 1 without a marked gap")
    check(not bad and len(out) > 20, f"auditschema check -writer gateway: no reason but a marked gap on any projected record except the falsifier ({len(bad)} of {len(out)}): {bad[:3]}")
    clean = sum(1 for _, _, lr in out if not attrs_of(lr).get("krateo.audit.incomplete"))
    check(clean >= 15, f"most projected records are complete and pass with exit 0 ({clean})")
    allkeys = set()
    for _, sc, lr in out:
        allkeys |= set(attrs_of(lr))
    check(all(sc.get("name") == SCOPE for _, sc, _ in out), "every projected record carries the gateway-audit scope name")
    check(all(lr.get("eventName") == "audit" for _, _, lr in out), "every projected record has EventName=audit")
    leaked = allkeys & {"user", "team", "principal", "act", "subject_principal", "endpoint", "backend_host", "baggage_session",
                        "source_xff", "user_agent", "route", "tool", "agent", "tokens_in", "mcp.session.id", "jwt.sub", "t",
                        "krateo.gateway_audit.class", "guardrail_detail", "http.method", "http.status", "protocol", "reason"}
    check(not leaked, f"closed set: no access-log key survives the projection ({sorted(leaked)})")
    blob = json.dumps([lr for _, _, lr in out])
    check("eyJ" not in blob, "no JWT-shaped value in any projected record")

    # 2. per-case mappings, by content
    def recs(pred):
        return [attrs_of(lr) for _, _, lr in out if pred(attrs_of(lr))]
    dele = recs(lambda a: a.get("krateo.audit.delegation") and len(a["krateo.audit.delegation"]["values"]) == 3)
    check(bool(dele) and all(a["user.name"].endswith(":k8s-agent") and a.get("krateo.audit.actor_class") == "agent"
                             and a.get("krateo.audit.on_behalf_of.user.name") == "alice" for a in dele),
          "delegated: user.name = outermost actor, class agent, on_behalf_of = the subject")
    if dele:
        chain = [v["stringValue"] for v in dele[0]["krateo.audit.delegation"]["values"]]
        check(chain[0].startswith("agent:") and chain[-1] == "human:alice", f"delegation outermost first, ends with the subject's class ({chain})")
    deep = recs(lambda a: a.get("krateo.audit.delegation") and len(a["krateo.audit.delegation"]["values"]) == 5)
    if deep:
        chain = [v["stringValue"] for v in deep[0]["krateo.audit.delegation"]["values"]]
        check(chain == ["agent:sa:one", "service:sa:two", ":sa:three", ":sa:four", "human:alice"],
              f"deep chain: no class and an UNKNOWN class (superuser) both as :<sub> ({chain})")
    else:
        check(False, "deep chain record projected")
    nocls = recs(lambda a: a.get("krateo.audit.delegation") and "krateo.audit.actor_class" not in a)
    check(bool(nocls) and all("actor_class" in a.get("krateo.audit.incomplete", "") for a in nocls),
          "actor with no class: actor_class absent and marked incomplete, never the subject's")
    plain = recs(lambda a: a.get("user.name") == "alice" and "krateo.audit.delegation" not in a)
    check(bool(plain) and all(a.get("krateo.audit.actor_class") == "human" and a.get("user.roles") for a in plain),
          "plain token: user.name = subject, class human, roles from team")
    den = recs(lambda a: a.get("krateo.audit.outcome") == "denied")
    check(len(den) == 3 and all("http.response.status_code" not in a for a in den),
          "gateway refusals (Authorization, RateLimit, JwtAuth) -> outcome denied, no backend status code")
    fail = recs(lambda a: a.get("krateo.audit.outcome") == "failure")
    check(any(a.get("http.response.status_code") == "500" for a in fail) and any("http.response.status_code" not in a for a in fail),
          "backend 500 -> failure with its status; gateway UpstreamFailure -> failure without one")
    tgt = recs(lambda a: a.get("krateo.action") == "gateway.mcp")
    check(bool(tgt) and all(a.get("server.address") == "kagent-tools.krateo-system.svc:8084" for a in tgt if a.get("krateo.audit.gateway.mcp_tool")),
          "MCP tools/call: server.address = the configured target host, mcp_tool set")
    check(len(tgt) == sum(1 for c in cases if c[2] == "projected" and c[0] in ("unknown-mcp-method",) or
                          (c[2] == "projected" and attrs_of(c[1]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]).get("protocol") == "mcp")),
          f"an MCP method on neither list (tools/frobnicate) is PROJECTED, not dropped ({len(tgt)} MCP records)")
    ips = recs(lambda a: a.get("krateo.request.source_ips"))
    check(bool(ips) and all(len(a["krateo.request.source_ips"]["values"]) >= 1 for a in ips)
          and any([v["stringValue"] for v in a["krateo.request.source_ips"]["values"]][0] == "203.0.113.7" for a in ips),
          "source_ips = forwarded entries, then the TCP peer")
    ua = recs(lambda a: (a.get("user_agent.original") or "").endswith("...TRUNCATED"))
    check(len(ua) == 1 and len(ua[0]["user_agent.original"]) == 1024 + len("...TRUNCATED"), "user agent cut at 1024 bytes + ...TRUNCATED")
    oth = recs(lambda a: a.get("http.request.method") == "_OTHER")
    check(len(oth) == 1 and oth[0].get("http.request.method_original") == "PROPFIND" and "verb" in oth[0].get("krateo.audit.incomplete", ""),
          "non-semconv method -> _OTHER + method_original, verb marked")
    sess = recs(lambda a: a.get("session.id") == "sess-42")
    check(bool(sess), "session.id from baggage")

    # 3. falsifier + marker order: each case's projection must be flagged by the checker
    check(not recs(lambda a: "krateo.audit.gateway.route" not in a), "a request that matched no route is never projected")
    rbd = recs(lambda a: a.get("krateo.audit.outcome") == "denied" and a.get("server.address") == "k8s-agent.krateo-system.svc:8080"
               and "http.response.status_code" not in a)
    check(len(rbd) >= 1, "refused before dialing (JwtAuth, no endpoint): denied, server.address = the route's configured target")
    # Falsifier on the checker itself: strip krateo.audit.gateway.route from a projected record.
    res0, sc0, lr0 = next((r for r in out if attrs_of(r[2]).get("krateo.audit.gateway.route")))
    lr0 = copy.deepcopy(lr0); lr0["attributes"] = [x for x in lr0["attributes"] if x["key"] != "krateo.audit.gateway.route"]
    code, msg = run_check(binary, wrap(res0, sc0, lr0), work, "noroute")
    check(code == 1 and "krateo.audit.gateway.route" in msg, "falsifier: auditschema reports a missing gateway.route")
    for i, (res, sc, lr) in enumerate(out):
        a = attrs_of(lr)
        if "server.address" not in a:
            code, msg = run_check(binary, wrap(res, sc, lr), work, f"notarget{i}")
            check(code == 1 and "server.address" in msg and "target" in a.get("krateo.audit.incomplete", ""),
                  "falsifier: no endpoint -> target marked, and auditschema reports server.address")
    marks = [a.get("krateo.audit.incomplete") for a in recs(lambda a: a.get("krateo.audit.incomplete"))]
    check("verb,method" in marks or any(m and m.endswith("verb,method") for m in marks), f"dropping http.method marks verb,method in the library's order ({marks})")
    check(any(m and "target" in m for m in marks), "dropping endpoint AND backend_host marks target")
    check(any(m and m.startswith("actor") and not m.startswith("actor_class") for m in marks), "dropping user marks actor")
    order = ["actor", "actor_class", "action", "verb", "method", "preview", "target"]
    check(all([order.index(x) for x in m.split(",")] == sorted(order.index(x) for x in m.split(",")) for m in marks if m),
          "every krateo.audit.incomplete lists markers in the library's order")
    for i, (res, sc, lr) in enumerate(out):
        a = attrs_of(lr)
        if a.get("krateo.audit.incomplete"):
            code, msg = run_check(binary, wrap(res, sc, lr), work, f"inc{i}")
            check("bad_format" not in msg and "order" not in msg.lower(), f"auditschema accepts the marker list {a['krateo.audit.incomplete']!r}")

    # 4. excluded hops are counted, per protocol and method
    met = (work / "out" / "metrics.jsonl")
    counts = {}
    if met.exists():
        for line in met.read_text().splitlines():
            for rm in json.loads(line).get("resourceMetrics", []):
                for sm in rm.get("scopeMetrics", []):
                    for m in sm.get("metrics", []):
                        if m["name"] != "krateo.gateway_audit.excluded":
                            continue
                        for dp in m.get("sum", {}).get("dataPoints", []):
                            k = tuple(sorted((x["key"], next(iter(x["value"].values()), "")) for x in dp.get("attributes", [])))
                            counts[k] = counts.get(k, 0) + int(dp.get("asInt", 0))
    excluded = [c for c in cases if c[2] == "excluded"]
    check(sum(counts.values()) == len(excluded), f"every excluded hop is counted ({sum(counts.values())} counted, {len(excluded)} excluded)")
    check(sum(v for k, v in counts.items() if dict(k).get("protocol") == "http") == 2,
          "the real no-route records (protocol http, the gateway's 404) are excluded AND counted")
    nm = recs(lambda a: a.get("krateo.action") == "gateway.mcp" and "krateo.audit.gateway.mcp_tool" not in a
              and a.get("http.request.method") == "POST")
    check(len(nm) >= 1, "the real MCP POST with no parsable method is PROJECTED")
    check(any(dict(k).get("mcp.method.name") == "initialize" for k in counts), "the count is per method (initialize seen)")


if __name__ == "__main__":
    main()
