#!/usr/bin/env python3
"""Functional test of otel-collector-daemonset's agent-content deletion (0.1.25).

kagent agents record the full tool response and tool arguments on every tool span, and the whole
prompt and model reply on every LLM span; the Python agents' instrumentor records gen_ai.prompt.* and
gen_ai.completion.*. Diego (2026-11-10): strip all content, keep kagent.user_id. The node agent's
`transform` processor must drop those keys on spans and span events and keep the rest: span metadata,
model, token counts, tool names and the user id. This runs the RENDERED processor in the pinned image
and checks what comes out.

Run: python3 tests/agent-content-keys-docker.py [repo-root]
Exit 0 = every check passed, 1 = at least one did not.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import yaml

CHART = "charts/otel-collector-daemonset"
PROC = "transform"

CONTENT_KEYS = {
    "gcp.vertex.agent.tool_response": '{"secret":"data: cGFzc3dvcmQ="}',
    "gcp.vertex.agent.tool_call_args": '{"namespace":"team-a"}',
    "gcp.vertex.agent.llm_request": '{"contents":[{"role":"user","parts":["show me the db password"]}]}',
    "gcp.vertex.agent.llm_response": '{"text":"here it is"}',
    "gen_ai.prompt.0.content": "show me the db password",
    "gen_ai.completion.0.content": "here it is",
    "gen_ai.input.messages": "[...]",
    "gen_ai.output.messages": "[...]",
    "gen_ai.system_instructions": "You are...",
}
INNOCENT = {
    "gen_ai.operation.name": "execute_tool",
    "gen_ai.request.model": "gpt-4o",
    "gen_ai.usage.input_tokens": "1200",
    "gen_ai.tool.name": "k8s_get_resources",
    "gen_ai.prompt_tokens_estimate": "kept: prefix gen_ai.prompt but not the content key",
    "kagent.user_id": "user-1",
    "gen_ai.conversation.id": "conv-1",
}


def render(root):
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    out = subprocess.run(["helm", "template", "gate", str(root / CHART), "-n", "krateo-system"],
                         check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    return yaml.safe_load(cm["data"]["relay"]), ds["spec"]["template"]["spec"]["containers"][0]["image"]


def kv(attrs):
    return [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]


def payloads():
    now = str(time.time_ns())
    traces = {"resourceSpans": [{"resource": {"attributes": kv({"service.name": "k8s_agent"})},
        "scopeSpans": [{"scope": {"name": "t"}, "spans": [{
            "traceId": "%032x" % 1, "spanId": "%016x" % 1, "name": "execute_tool k8s_get_resources", "kind": 1,
            "startTimeUnixNano": now, "endTimeUnixNano": now,
            "attributes": kv({**CONTENT_KEYS, **INNOCENT}),
            "events": [{"timeUnixNano": now, "name": "gen_ai.content.prompt",
                        "attributes": kv({"gen_ai.prompt": "show me the db password", "detail": "kept"})}],
        }]}]}]}
    logs = {"resourceLogs": [{"resource": {"attributes": kv({"service.name": "agent-gateway"})},
        "scopeLogs": [{"scope": {"name": "request"}, "logRecords": [{
            "timeUnixNano": now, "body": {"stringValue": "access"},
            "attributes": kv({"http.request.header.authorization": "Bearer opaque-789", **INNOCENT}),
        }]}]}]}
    return traces, logs


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, image = render(root)
    cfg = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {PROC: relay["processors"][PROC]},
        "exporters": {"file/traces": {"path": "/out/traces.json"}, "file/logs": {"path": "/out/logs.json"}},
        "service": {"pipelines": {
            "traces": {"receivers": ["otlp"], "processors": [PROC], "exporters": ["file/traces"]},
            "logs": {"receivers": ["otlp"], "processors": [PROC], "exporters": ["file/logs"]},
        }},
    }
    work = pathlib.Path(tempfile.mkdtemp(prefix="agentcontent-"))
    os.chmod(work, 0o755)
    (work / "out").mkdir()
    os.chmod(work / "out", 0o777)
    (work / "relay.yaml").write_text(yaml.safe_dump(cfg))
    os.chmod(work / "relay.yaml", 0o644)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"agentcontent-{port}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:4318", "-e", "MY_POD_IP=0.0.0.0",
                    "-v", f"{work}:/cfg:ro", "-v", f"{work / 'out'}:/out", image, "--config=/cfg/relay.yaml"],
                   check=True, capture_output=True)
    traces, logs = payloads()
    span_attrs = event_attrs = log_attrs = None
    try:
        for path, body in (("/v1/traces", traces), ("/v1/logs", logs)):
            for _ in range(60):
                try:
                    urllib.request.urlopen(urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"}), timeout=5)
                    break
                except Exception:
                    time.sleep(1)
        for _ in range(20):
            time.sleep(1)
            t, l = work / "out" / "traces.json", work / "out" / "logs.json"
            if t.exists() and l.exists() and t.read_text().strip() and l.read_text().strip():
                sp = json.loads(t.read_text().splitlines()[-1])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
                span_attrs = {a["key"] for a in sp.get("attributes", [])}
                event_attrs = {a["key"] for a in sp["events"][0].get("attributes", [])}
                lr = json.loads(l.read_text().splitlines()[-1])["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
                log_attrs = {a["key"] for a in lr.get("attributes", [])}
                break
        if span_attrs is None:
            print(subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr[-2000:])
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    failed = False

    def check(ok, msg):
        nonlocal failed
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {msg}")

    span_attrs, event_attrs, log_attrs = span_attrs or set(), event_attrs or set(), log_attrs or set()
    check(not (span_attrs & set(CONTENT_KEYS)), f"span: no content key left {sorted(span_attrs & set(CONTENT_KEYS))}")
    check(set(INNOCENT) <= span_attrs, f"span: metadata kept, user id included {sorted(span_attrs)}")
    check("gen_ai.prompt" not in event_attrs and "detail" in event_attrs, f"span event: prompt dropped, detail kept {sorted(event_attrs)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
