#!/usr/bin/env python3
"""Functional test of otel-collector-tailsampling's sampling policies, against the pinned image.

`otelcol validate` does not compile the tail_sampling OTTL conditions (a misspelt function passes
it), so the only real check is to run the collector with the chart's own policies, send it traces,
and read which ones it kept. This renders the chart, keeps its processors exactly as rendered, and
changes only the edges: an OTLP/HTTP receiver to send into, a file exporter instead of ClickHouse,
and a short decision_wait so the test is quick.

Run: python3 tests/tailsampling-docker.py [repo-root]
Exit 0 = every case behaved as the design says (observability#118), 1 = at least one did not.
"""
import base64
import json
import os
import pathlib
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import yaml

CHART = "charts/otel-collector-tailsampling"
DECISION_WAIT_S = 3


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


# A synthetic JWT-shaped value, built here so no token-shaped literal sits in the repo (the secret
# scan would rightly flag one). Unsigned and meaningless: it only has to look like a JWT.
JWT = ".".join([b64url({"alg": "none", "typ": "JWT"}), b64url({"sub": "tailsampling-test"}), "x" * 24])


def render(root):
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    out = subprocess.run(
        ["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"],
        check=True, capture_output=True, text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    config = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    workload = next(d for d in docs if d["kind"] == "Deployment")
    image = workload["spec"]["template"]["spec"]["containers"][0]["image"]
    return yaml.safe_load(config["data"]["relay"]), image


def test_config(relay):
    """The chart's processors untouched; only the receiver, exporter and wait are test doubles."""
    relay["receivers"] = {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
    relay["exporters"] = {"file": {"path": "/out/traces.json"}}
    relay["processors"]["tail_sampling"]["decision_wait"] = f"{DECISION_WAIT_S}s"
    relay["service"]["pipelines"] = {
        "traces": {
            "receivers": ["otlp"],
            "processors": relay["service"]["pipelines"]["traces"]["processors"],
            "exporters": ["file"],
        }
    }
    relay["service"].pop("extensions", None)
    relay.pop("extensions", None)
    return relay


def attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def span(trace_id, service, name, attrs=None, status=0, message="", events=None):
    now = time.time_ns()
    s = {
        "traceId": trace_id,
        "spanId": "%016x" % random.getrandbits(64),
        "name": name,
        "kind": 2,
        "startTimeUnixNano": str(now - 5_000_000),
        "endTimeUnixNano": str(now),
        "attributes": [attr(k, v) for k, v in (attrs or {}).items()],
        "status": {"code": status, "message": message},
    }
    if events:
        s["events"] = [
            {"timeUnixNano": str(now), "name": "e", "attributes": [attr(k, v) for k, v in ev.items()]}
            for ev in events
        ]
    return service, s


def payload(spans):
    by_service = {}
    for service, s in spans:
        by_service.setdefault(service, []).append(s)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [attr("service.name", svc)]},
                "scopeSpans": [{"scope": {"name": "test"}, "spans": items}],
            }
            for svc, items in by_service.items()
        ]
    }


def portal_trace(trace_id, gw_status="200", sp_status=200, **sp_extra):
    """The portal path: a platform-gateway span and a snowplow server span in one trace."""
    sp_attrs = {"http.response.status_code": sp_status, "http.route": "/call"}
    sp_attrs.update(sp_extra.pop("attrs", {}))
    return [
        span(trace_id, "krateo-agent-gateway-platform", "GET /content/*", {"http.status": gw_status}),
        span(trace_id, "snowplow", "GET /call", sp_attrs, **sp_extra),
    ]


def sse_trace(trace_id, status=0):
    """A portal SSE stream: the platform gateway's span and sse-proxy's, in one trace."""
    return [
        span(trace_id, "krateo-agent-gateway-platform", "GET /events/*", {"http.status": "200"}),
        span(trace_id, "sse-proxy", "GET /notifications", {"http.response.status_code": 200}, status=status),
    ]


def post(port, spans):
    body = json.dumps(payload(spans)).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/traces", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200, r.status


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kept_traces(out_dir):
    path = out_dir / "traces.json"
    kept = {}
    if not path.exists():
        return kept
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        for rs in json.loads(line).get("resourceSpans", []):
            svc = next(
                (a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name"),
                "",
            )
            for ss in rs.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    kept.setdefault(s["traceId"], []).append((svc, s["name"]))
    return kept


def tid():
    return "%032x" % random.getrandbits(128)


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, image = render(root)
    relay = test_config(relay)

    # The collector runs as a non-root user in the container, so on Linux (unlike Docker Desktop,
    # which ignores host permissions) it must be able to read the config and write the output.
    work = pathlib.Path(tempfile.mkdtemp(prefix="tailsampling-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir()
    os.chmod(work / "out", 0o777)
    (work / "relay.yaml").write_text(yaml.safe_dump(relay))
    os.chmod(work / "relay.yaml", 0o644)
    port = free_port()
    name = f"tailsampling-test-{port}"
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:4318", "-e", "MY_POD_IP=0.0.0.0",
         "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/relay.yaml"],
        check=True, capture_output=True,
    )
    try:
        for _ in range(60):
            try:
                post(port, [])
                break
            except Exception:
                state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                                       capture_output=True, text=True).stdout.strip()
                if state != "true":
                    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
                    print("FAIL collector exited at startup:\n" + (logs.stdout + logs.stderr)[-3000:])
                    return 1
                time.sleep(1)
        else:
            print("FAIL collector never accepted OTLP/HTTP")
            return 1

        cases = {}  # name -> (trace id, expected kept)
        def case(label, expected, spans_fn):
            t = tid()
            cases[label] = (t, expected)
            post(port, spans_fn(t))

        case("portal 5xx (span status ERROR)", True, lambda t: portal_trace(t, gw_status="500", sp_status=500, status=2))
        case("portal 4xx on snowplow http.response.status_code", True, lambda t: portal_trace(t, sp_status=404))
        case("portal 4xx on gateway http.status (string)", True, lambda t: portal_trace(t, gw_status="403", sp_status=200))
        case("JWT in a span attribute", True, lambda t: portal_trace(t, attrs={"url.query": "token=" + JWT}))
        case("JWT in the status message", True, lambda t: portal_trace(t, message="bad token " + JWT))
        case("JWT in a span event attribute", True, lambda t: portal_trace(t, events=[{"detail": JWT}]))
        case("reconcile trace (composition-dynamic-controller)", True,
             lambda t: [span(t, "composition-dynamic-controller", "reconcile")])
        case("LLM trace (agent gateway)", True,
             lambda t: [span(t, "krateo-agent-gateway", "POST /v1/chat/completions", {"http.status": "200"})])
        case("portal trace touching another service", True,
             lambda t: portal_trace(t) + [span(t, "git-provider", "GET /repos")])
        case("eyJ-prefixed but too short to be a JWT", "sampled",
             lambda t: portal_trace(t, attrs={"note": "eyJshort"}))

        case("browser-originated portal 5xx", True,
             lambda t: [span(t, "krateo-frontend", "krateo.widget.fetch", {"http.response.status_code": 500})]
             + portal_trace(t, gw_status="500", sp_status=500, status=2))

        case("SSE stream that errors (sse-proxy, on the portal path)", True,
             lambda t: sse_trace(t, status=2))

        ok_ids = [tid() for _ in range(400)]
        for t in ok_ids:
            post(port, portal_trace(t))
        # Browser-originated portal traces (installer#405): krateo-frontend is on the portal path, so a
        # plain one is sampled like the rest, NOT kept whole as "outside the portal path".
        browser_ids = [tid() for _ in range(400)]
        for t in browser_ids:
            post(port, [span(t, "krateo-frontend", "krateo.widget.fetch",
                             {"http.route": "/call", "http.response.status_code": 200})] + portal_trace(t))

        # Portal SSE streams: sse-proxy is on the portal path, so a plain stream is sampled too.
        sse_ids = [tid() for _ in range(400)]
        for t in sse_ids:
            post(port, sse_trace(t))

        # Late spans: arrive after their trace's decision. Each must follow that decision.
        late_kept, late_dropped = cases["portal 4xx on snowplow http.response.status_code"][0], None
        time.sleep(DECISION_WAIT_S + 4)
        before = kept_traces(work / "out")
        dropped_ok = [t for t in ok_ids if t not in before]
        late_dropped = dropped_ok[0]
        post(port, [span(late_kept, "snowplow", "GET /refreshes (late)", {"http.route": "/refreshes"})])
        post(port, [span(late_dropped, "snowplow", "GET /refreshes (late)", {"http.route": "/refreshes"})])
        time.sleep(DECISION_WAIT_S + 4)
        kept = kept_traces(work / "out")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    failed = False
    for label, (t, expected) in cases.items():
        got = t in kept
        if expected == "sampled":
            print(f"info {label}: {'kept' if got else 'dropped'} (no keep rule; only the 5% sample applies)")
            continue
        status = "ok  " if got == expected else "FAIL"
        failed |= got != expected
        print(f"{status} {label}: {'kept' if got else 'dropped'}, expected {'kept' if expected else 'dropped'}")
        if got:
            services = {svc for svc, _ in kept[t]}
            if label.startswith("portal") and not {"krateo-agent-gateway-platform", "snowplow"} <= services:
                failed = True
                print(f"FAIL {label}: kept but incomplete, services {sorted(services)}")

    n_ok = sum(1 for t in ok_ids if t in kept)
    share = 100 * n_ok / len(ok_ids)
    # 5% of 400 is 20; a binomial 99.9% interval is about 6..38.
    within = 6 <= n_ok <= 38
    failed |= not within
    print(f"{'ok  ' if within else 'FAIL'} plain 2xx portal traces kept: {n_ok}/{len(ok_ids)} ({share:.1f}%), expected about 5%")
    n_br = sum(1 for t in browser_ids if t in kept)
    within_br = 6 <= n_br <= 38
    failed |= not within_br
    print(f"{'ok  ' if within_br else 'FAIL'} plain browser-originated portal traces kept: {n_br}/{len(browser_ids)}, expected about 5%")

    n_sse = sum(1 for t in sse_ids if t in kept)
    within_sse = 6 <= n_sse <= 38
    failed |= not within_sse
    print(f"{'ok  ' if within_sse else 'FAIL'} plain portal SSE traces (sse-proxy) kept: {n_sse}/{len(sse_ids)}, expected about 5%")

    late_k = any(n == "GET /refreshes (late)" for _, n in kept.get(late_kept, []))
    late_d = late_dropped in kept
    failed |= not late_k or late_d
    print(f"{'ok  ' if late_k else 'FAIL'} late span of a KEPT trace: {'kept' if late_k else 'dropped'}")
    print(f"{'ok  ' if not late_d else 'FAIL'} late span of a DROPPED trace: {'kept' if late_d else 'dropped'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
