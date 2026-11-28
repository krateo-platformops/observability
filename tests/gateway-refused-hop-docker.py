#!/usr/bin/env python3
"""Functional test of otel-collector-daemonset's gateway refused-hop count (krateo.gateway.refused_hops).

The chart is rendered and the gateway pipelines run exactly as rendered, with two changes for Docker:
the clickhouse exporters become file exporters, and nothing else is touched. Synthetic access-log
records (the shape of the real ones measured on krateo-057 on 2026-11-28: a 403 `Authorization`
refusal with an empty `user`, protocol `http`, a route, error "authorization failed") are sent over
OTLP/HTTP, in BOTH access-log shapes (agentgateway < 1.6: scope `request`, http.status; >= 1.6: scope
`agentgateway.access`, http.response.status_code), and the checks run on what the collector writes:
  * refused-before-verification hops (user empty, a refusal reason) increment the count with the
    right route.class, reason and http.status;
  * a refusal AFTER verification (a user is present) does not, and is projected as a denied audit row;
  * a 200, and a backend's own 403 (reason Upstream, or none) do not;
  * an unknown reason text on a 403 is counted as reason `other`, and its text appears nowhere;
  * the three classes are disjoint: every hop is in exactly one of krateo_audit, krateo.gateway_audit.excluded,
    krateo.gateway.refused_hops (the totals add up to the hops sent);
  * the metric points carry exactly route.class, reason and http.status, and the resource only
    service.name: no identity, path, route name, source address, user agent or session id;
  * refusals tell their truth: the same case set, with the refusal markers removed, counts zero (control);
  * the daemonset's gateway pipelines and the child's whole config pass `otelcol validate`.

Run: python3 tests/gateway-refused-hop-docker.py [repo-root]
Needs docker (the image the chart pins, already pulled) and helm. Exit 0 = all passed.
"""
import collections
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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from agentgateway_access_log_v16 import convert_request  # noqa: E402

CHART = "charts/otel-collector-daemonset"
CHILD = "tests/fixtures/collector-child-values.yaml"
PIPES = ["logs/gateway-audit", "logs/gateway-audit-excluded", "metrics/gateway-audit-excluded",
         "logs/gateway-refused-hop", "metrics/gateway-refused-hop"]
CONNECTORS = ["count/gateway-audit-excluded", "count/gateway-refused-hop"]
METRIC = "krateo.gateway.refused_hops"
# The denominator: records sent per shape. Examined 2026-11-28 with the cases below: 31 (29, plus the two pinned cases). The test
# fails if fewer were sent or read back, so a case list emptied by mistake cannot print "passed".
MIN_CASES = 29
FAILS = []
LABEL = ""
ROUTE = {"mcp": "krateo-system/agentgateway-policies-mcp-repo-mcp-server",
         "a2a": "krateo-system/agentgateway-policies-a2a",
         "subagent": "krateo-system/agentgateway-policies-subagent-nightly-review-agent",
         "llm": "krateo-system/agentgateway-policies-llm"}
# Values that identify a caller or a request. None may appear anywhere in the metric output.
SECRETS = ["alice@example.com", "10.9.8.7", "/secret/path", "evil-agent/9.9", "sess-123", "krateo-system/agentgateway-policies",
           "unknown-reason-text-xyz", "authorization failed", "jwt-subject-42", "pod-abc", "10.40.4.22"]


def check(cond, msg):
    msg = f"[{LABEL}] {msg}" if LABEL else msg
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def sh(*args, check=True, **kw):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, **kw)


def render(root, *values):
    sh("helm", "dependency", "build", str(root / CHART))
    args = ["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"]
    for v in values:
        args += ["-f", str(root / v)]
    docs = [d for d in yaml.safe_load_all(sh(*args).stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    wl = next(d for d in docs if d["kind"] in ("DaemonSet", "Deployment"))
    return yaml.safe_load(cm["data"]["relay"]), wl["spec"]["template"]["spec"]["containers"][0]["image"]


def test_config(relay):
    svc = relay["service"]["pipelines"]
    for p in PIPES:
        if p not in svc:
            raise SystemExit(f"rendered config has no pipeline {p}")
    pipes = {p: copy.deepcopy(svc[p]) for p in PIPES}
    used = {x for p in pipes.values() for x in p.get("processors", [])}
    for p in pipes.values():
        p["exporters"] = [{"clickhouse/audit": "file/audit", "clickhouse": "file/metrics"}.get(e, e) for e in p["exporters"]]
    if "k8sattributes" in used:
        raise SystemExit("a gateway pipeline uses k8sattributes: the test config would need the API server")
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {k: v for k, v in relay["processors"].items() if k in used},
        "connectors": {k: v for k, v in relay["connectors"].items() if k in CONNECTORS},
        "exporters": {"file/audit": {"path": "/out/audit.jsonl", "flush_interval": "1s"},
                      "file/metrics": {"path": "/out/metrics.jsonl", "flush_interval": "1s"}},
        "service": {"pipelines": pipes},
    }


# ------------------------------------------------------------------------------------- records --
def sval(v):
    return {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}


def rec(route=None, protocol="http", reason=None, status=403, user=None, extra=None, method="POST"):
    """One OTLP request holding one < 1.6 access-log record with every identifying field filled."""
    a = {"gateway": "krateo-system/krateo-agent-gateway", "protocol": protocol, "http.method": method, "http.status": status,
         "src.addr": "10.9.8.7:51000", "http.path": "/secret/path", "user_agent": "evil-agent/9.9",
         "baggage_session": "sess-123", "http.host": "gw.example", "team": "devs", "endpoint": "10.40.4.22:8083",
         "error": "authorization failed", "jwt.sub": "jwt-subject-42"}
    if route:
        a["route"] = route
    if reason is not None:
        a["reason"] = reason
    if user is not None:
        a["user"] = user
    a.update(extra or {})
    res = {"attributes": [{"key": k, "value": sval(v)} for k, v in
                          {"service.name": "krateo-agent-gateway", "k8s.pod.name": "pod-abc", "k8s.pod.ip": "10.40.4.22",
                           "host.name": "node-1"}.items()]}
    lr = {"timeUnixNano": str(time.time_ns()), "observedTimeUnixNano": str(time.time_ns()), "severityNumber": 9,
          "body": {"stringValue": "request"}, "attributes": [{"key": k, "value": sval(v)} for k, v in a.items()]}
    return {"resourceLogs": [{"resource": res, "scopeLogs": [{"scope": {"name": "request"}, "logRecords": [lr]}]}]}


# (name, request, where it must be counted, expected (route.class, reason, http.status) when refused)
def build_cases():
    cases = []

    def add(name, req, want, key=None, n=1):
        for _ in range(n):
            cases.append((name, copy.deepcopy(req), want, key))

    # The measured case: 403 Authorization, protocol http, no user. Three of them, on an mcp route.
    add("403 authorization mcp", rec(ROUTE["mcp"], reason="Authorization"), "refused", ("mcp", "authorization_failed", "403"), 3)
    add("403 authorization a2a", rec(ROUTE["a2a"], reason="Authorization"), "refused", ("a2a", "authorization_failed", "403"), 2)
    add("403 authorization subagent", rec(ROUTE["subagent"], reason="Authorization"), "refused", ("a2a", "authorization_failed", "403"))
    add("403 authorization llm", rec(ROUTE["llm"], protocol="llm", reason="Authorization"), "refused", ("llm", "authorization_failed", "403"))
    add("401 jwt", rec(ROUTE["mcp"], reason="JwtAuth", status=401), "refused", ("mcp", "authentication_failed", "401"), 2)
    for r in ("Oidc", "BasicAuth", "APIKeyAuth", "ExtAuth"):
        add(f"401 {r}", rec(ROUTE["a2a"], reason=r, status=401), "refused", ("a2a", "authentication_failed", "401"))
    add("429 ratelimit", rec(ROUTE["llm"], reason="RateLimit", status=429), "refused", ("llm", "rate_limited", "429"), 2)
    add("overload 503", rec(ROUTE["llm"], reason="Overload", status=503), "refused", ("llm", "other", "other"))
    add("guardrail 403", rec(ROUTE["llm"], reason="Guardrail", status=403), "refused", ("llm", "other", "403"))
    add("unknown route name", rec("krateo-system/some-other-route", reason="Authorization"), "refused", ("other", "authorization_failed", "403"))
    add("unknown reason text", rec(ROUTE["mcp"], reason="unknown-reason-text-xyz", status=403), "refused", ("mcp", "other", "403"))
    add("no route, refused", rec(None, reason="JwtAuth", status=401), "refused", ("other", "authentication_failed", "401"))
    # NOT counted here. A verified caller (user present): projected as a denied row when it is an acting hop.
    add("verified denial, a2a", rec(ROUTE["a2a"], protocol="a2a", reason="Authorization", user="alice@example.com",
                                   extra={"a2a.method": "message/send", "principal": "human"}), "audit")
    add("verified denial, llm ratelimit", rec(ROUTE["llm"], protocol="llm", reason="RateLimit", status=429, user="alice@example.com",
                                             extra={"principal": "human"}), "audit")
    # A verified caller refused on a plain http hop: excluded (not an acting protocol), still not refused-before-verification.
    add("verified denial, plain http", rec(ROUTE["a2a"], reason="Authorization", user="alice@example.com"), "excluded")
    add("empty-string user is unverified", rec(ROUTE["mcp"], reason="Authorization", user=""), "refused", ("mcp", "authorization_failed", "403"))
    # Not a refusal at all.
    cases.extend(pinned_cases())
    add("200", rec(ROUTE["mcp"], status=200), "excluded")
    add("200 with user", rec(ROUTE["a2a"], status=200, user="alice@example.com"), "excluded")
    add("backend 403, reason Upstream", rec(ROUTE["a2a"], reason="Upstream", status=403), "excluded")
    add("backend 403, no reason", rec(ROUTE["a2a"], status=403), "excluded")
    add("mcp protocol error, no user (projected, a failure row)", rec(ROUTE["mcp"], protocol="mcp", reason="MCP", status=405, method="PUT"), "audit")
    return cases


def pinned_cases():
    """The two cases snowplow asked to see pinned by name (2026-11-28)."""
    up = rec(ROUTE["a2a"], reason="Upstream", status=403)  # no user: an upstream's own 403 on a public route
    after = rec(ROUTE["a2a"], protocol="a2a", reason="Authorization", status=403, user="alice@example.com",
                extra={"a2a.method": "message/send", "principal": "human"})
    return [("PINNED 1: empty user, reason Upstream, 403", up, "excluded", None),
            ("PINNED 2: refused AFTER verification (user present, Authorization, 403)", after, "audit", None)]


def wrap_shape(req, shape16):
    return convert_request(req) if shape16 else req


# ----------------------------------------------------------------------------------------- run --
def read_lines(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def points(work):
    """(name, resource attrs, scope name, point attrs, value) for every sum point written."""
    out = []
    for line in read_lines(work / "out" / "metrics.jsonl"):
        for rm in line.get("resourceMetrics", []):
            ra = {a["key"]: next(iter(a["value"].values())) for a in rm["resource"].get("attributes", [])}
            for sm in rm.get("scopeMetrics", []):
                for m in sm.get("metrics", []):
                    for dp in m.get("sum", {}).get("dataPoints", []):
                        pa = {a["key"]: next(iter(a["value"].values())) for a in dp.get("attributes", [])}
                        out.append((m["name"], ra, sm.get("scope", {}).get("name"), pa, int(dp.get("asInt", 0) or dp.get("asDouble", 0))))
    return out


def run(image, cfg, cases, shape16):
    work = pathlib.Path(tempfile.mkdtemp(prefix="gwrefused-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir(); os.chmod(work / "out", 0o777)
    (work / "otel.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False)); os.chmod(work / "otel.yaml", 0o644)
    name = "gwrefused-" + uuid.uuid4().hex[:8]
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
        for _, req, _, _ in cases:
            r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/logs", data=json.dumps(wrap_shape(req, shape16)).encode(),
                                       headers={"content-type": "application/json"}, method="POST")
            urllib.request.urlopen(r, timeout=10).read()
        time.sleep(10)
        logs = sh("docker", "logs", name, check=False).stderr
        return work, logs, points(work), read_lines(work / "out" / "audit.jsonl")
    finally:
        sh("docker", "rm", "-f", name, check=False)


def verify(cases, logs, pts, audit, work):
    check("Everything is ready" in logs and "error" not in logs.lower().replace("error_mode", ""), "the collector started the gateway pipelines without error")
    sent = len(cases)
    check(sent >= MIN_CASES, f"examined at least {MIN_CASES} records ({sent})")
    refused_pts = [p for p in pts if p[0] == METRIC]
    got = collections.Counter()
    for _, _, _, pa, v in refused_pts:
        got[(pa.get("route.class"), pa.get("reason"), pa.get("http.status"))] += v
    want = collections.Counter(k for _, _, w, k in cases if w == "refused")
    n_want = sum(want.values())
    check(n_want >= 15 and sum(got.values()) > 0, f"the count is not empty: {sum(got.values())} counted, {n_want} expected (>= 15 expected)")
    check(got == want, f"each refused hop is counted once under its route.class, reason and http.status: got {dict(got)}")
    if got != want:
        print("  missing:", dict(want - got), " extra:", dict(got - want))
    # Exact dimensions, resource, scope.
    check(all(set(p[3]) == {"route.class", "reason", "http.status"} for p in refused_pts), "every point carries exactly route.class, reason and http.status")
    check(all(p[3]["route.class"] in ("mcp", "a2a", "llm", "other") and p[3]["reason"] in ("authorization_failed", "authentication_failed", "rate_limited", "other")
              and p[3]["http.status"] in ("401", "403", "429", "other") for p in refused_pts), "every dimension value is from its fixed list")
    check(all(set(p[1]) == {"service.name"} for p in refused_pts), f"the resource is cut to service.name ({sorted({k for p in refused_pts for k in p[1]})})")
    blob = json.dumps(refused_pts)
    leaked = [x for x in SECRETS if x in blob]
    check(not leaked, f"nothing that identifies a caller, a path or a route reaches any metric point ({leaked})")
    # Disjoint classes: audit rows + excluded count + refused count = hops sent that are in some class.
    excl = sum(p[4] for p in pts if p[0] == "krateo.gateway_audit.excluded")
    n_audit = sum(len(sl["logRecords"]) for line in audit for rl in line.get("resourceLogs", []) for sl in rl["scopeLogs"])
    check(n_audit == sum(1 for c in cases if c[2] == "audit"), f"exactly the verified acting hops reach krateo_audit ({n_audit})")
    check(excl == sum(1 for c in cases if c[2] == "excluded"), f"krateo.gateway_audit.excluded counts only the hops that are not refused ({excl})")
    check(n_audit + excl + sum(got.values()) == sent, f"no hop is counted twice or lost: {n_audit} audit + {excl} excluded + {sum(got.values())} refused = {sent} sent")
    # a verified denial is a denied row
    outcomes = []
    for line in audit:
        for rl in line.get("resourceLogs", []):
            for sl in rl["scopeLogs"]:
                for lr in sl["logRecords"]:
                    outcomes.append({a["key"]: next(iter(a["value"].values())) for a in lr["attributes"]}.get("krateo.audit.outcome"))
    check(sorted(outcomes) == ["denied", "denied", "failure"], f"a refusal after verification is a denied audit row, an unverified one is not ({outcomes})")


def validate(image, relay, label):
    with tempfile.TemporaryDirectory() as d:
        os.chmod(d, 0o755)
        f = pathlib.Path(d) / "relay.yaml"
        f.write_text(yaml.safe_dump(relay)); os.chmod(f, 0o644)
        env = [x for k, v in {"MY_POD_IP": "127.0.0.1", "CH_USERNAME": "u", "CH_PASSWORD": "p", "OTEL_K8S_NODE_NAME": "n",
                              "OTEL_K8S_NODE_IP": "10.0.0.1", "OTEL_K8S_NAMESPACE": "krateo-system", "OTEL_K8S_POD_NAME": "p",
                              "OTEL_K8S_POD_IP": "10.0.0.2"}.items() for x in ("-e", f"{k}={v}")]
        r = subprocess.run(["docker", "run", "--rm", *env, "-v", f"{d}:/v:ro", image, "validate", "--config=/v/relay.yaml"], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def main():
    global LABEL
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, image = render(root)
    cfg = test_config(relay)
    cases = build_cases()
    for label, shape16 in (("agentgateway < 1.6", False), ("agentgateway >= 1.6", True)):
        LABEL = label
        print(f"\n== {label}: {len(cases)} records")
        if shape16:
            check(all(sl["scope"]["name"] == "agentgateway.access" for _, r, _, _ in cases
                      for rl in convert_request(r)["resourceLogs"] for sl in rl["scopeLogs"]), "the 1.6+ cases carry scope agentgateway.access")
        work, logs, pts, audit = run(image, cfg, cases, shape16)
        try:
            verify(cases, logs, pts, audit, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # PINNED cases, on their own so the counts are unambiguous (both shapes).
    for label, shape16 in (("agentgateway < 1.6", False), ("agentgateway >= 1.6", True)):
        LABEL = label
        up_case, after_case = pinned_cases()
        work, logs, pts, audit = run(image, cfg, [up_case], shape16)
        shutil.rmtree(work, ignore_errors=True)
        check(sum(p[4] for p in pts if p[0] == METRIC) == 0 and sum(p[4] for p in pts if p[0] == "krateo.gateway_audit.excluded") == 1
              and not audit, f"{up_case[0]}: NOT counted as refused (counted as excluded, no audit row)")
        work, logs, pts, audit = run(image, cfg, [after_case], shape16)
        shutil.rmtree(work, ignore_errors=True)
        attrs = [{a["key"]: next(iter(a["value"].values())) for a in lr["attributes"]}
                 for line in audit for rl in line.get("resourceLogs", []) for sl in rl["scopeLogs"] for lr in sl["logRecords"]]
        check(sum(p[4] for p in pts if p[0] == METRIC) == 0, f"{after_case[0]}: NOT counted as refused")
        check(len(attrs) == 1 and attrs[0].get("krateo.audit.outcome") == "denied" and attrs[0].get("user.name") == "alice@example.com",
              f"{after_case[0]}: lands as ONE denied krateo_audit row with its actor ({[(a.get('krateo.audit.outcome'), a.get('user.name')) for a in attrs]})")

    # CONTROL: the same records with the refusal markers (reason) removed count zero; a test that
    # counted everything it was sent would pass the checks above and fail here.
    LABEL = "control"
    ctl = []
    for name, req, want, key in cases:
        req = copy.deepcopy(req)
        for rl in req["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                for lr in sl["logRecords"]:
                    lr["attributes"] = [a for a in lr["attributes"] if a["key"] != "reason"]
        ctl.append((name, req, "excluded", None))
    work, logs, pts, audit = run(image, cfg, ctl, False)
    shutil.rmtree(work, ignore_errors=True)
    check(sum(p[4] for p in pts if p[0] == METRIC) == 0 and any(p[0] == "krateo.gateway_audit.excluded" for p in pts),
          "control: with the reasons removed no hop is refused (all counted as excluded)")

    # Validation: the child's whole config, and the daemonset's gateway pipelines (its host receivers
    # need /hostfs and /var/lib/otelcol, which `validate` stats, so the rest of the daemonset is not run here).
    LABEL = "validate"
    child, cimage = render(root, CHILD)
    check(all(p in child["service"]["pipelines"] for p in PIPES), "the child config has the gateway refused-hop pipelines (no gateway there: they see nothing)")
    rc, text = validate(cimage, child, "child")
    check(rc == 0, f"the child config validates ({text[-300:] or 'no output'})")
    sub = copy.deepcopy(cfg)
    sub["exporters"] = {k: v for k, v in relay["exporters"].items() if k in ("clickhouse", "clickhouse/audit")}
    for p in sub["service"]["pipelines"].values():
        p["exporters"] = [{"file/audit": "clickhouse/audit", "file/metrics": "clickhouse"}.get(e, e) for e in p["exporters"]]
    sub["receivers"]["otlp"] = relay["receivers"]["otlp"]
    rc, text = validate(image, sub, "daemonset gateway pipelines")
    check(rc == 0, f"the daemonset's gateway pipelines validate with its own exporters ({text[-300:] or 'no output'})")
    bad = copy.deepcopy(sub)
    bad["service"]["pipelines"]["logs/gateway-refused-hop"]["processors"].append("transform/no-such")
    rc, text = validate(image, bad, "control")
    check(rc != 0 and "no-such" in text, f"control: a pipeline naming a missing processor is refused ({text[-120:]})")
    print(f"\n{len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
