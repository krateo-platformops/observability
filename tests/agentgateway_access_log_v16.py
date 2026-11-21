"""The OTLP access-log record agentgateway >= 1.6 writes, derived from a < 1.6 one.

agentgateway 1.6 renamed its access-log scope and the HTTP keys of its access log and server spans.
This mirrors the proxy's own code at v1.7.0 (crates/agentgateway/src/telemetry/semconv.rs
http_attribute, and log.rs HttpSemconvAttributes):

  scope            request       -> agentgateway.access   (logger name, log.rs; upstream #3430)
  http.method                    -> http.request.method
  http.status                    -> http.response.status_code
  src.addr         "ip:port"     -> client.address  "ip"  (the peer IP only, no port, no brackets)
  http.host                      -> server.address        (the request's Host)
  http.path        "/p?q"        -> url.path "/p", url.query "q"
  http.version     "HTTP/1.1"    -> network.protocol.version "1.1"
                                    url.scheme             (added)

Every other key, including the CEL fields agentgateway-policies adds, is unchanged. The fixture
(tests/fixtures/gateway-access-log.otlp.jsonl) was written by a REAL v1.5.0 proxy; this derives the
1.6+ shape from it so both shapes run through the same checks.
"""
import copy
import re

SCOPE_V15 = "request"
SCOPE_V16 = "agentgateway.access"
RENAME = {"http.method": "http.request.method", "http.status": "http.response.status_code", "http.host": "server.address"}
VERSIONS = {"HTTP/0.9": "0.9", "HTTP/1.0": "1.0", "HTTP/1.1": "1.1", "HTTP/2.0": "2", "HTTP/2": "2", "HTTP/3.0": "3", "HTTP/3": "3"}


def _val(value):
    return next(iter(value.values()), None) if value else None


def peer_ip(addr):
    """ip:port -> ip and [v6]:port -> v6, as the proxy's peer_addr.ip().to_string() gives it."""
    m = re.match(r"^\[(.*)\]:\d+$", addr) or re.match(r"^([^:\[\]]+):\d+$", addr)
    return m.group(1) if m else addr


def convert_record(lr):
    lr = copy.deepcopy(lr)
    out = []
    for a in lr.get("attributes", []):
        k, v = a["key"], a.get("value")
        if k in RENAME:
            out.append({"key": RENAME[k], "value": v})
        elif k == "src.addr":
            if _val(v) is not None:
                out.append({"key": "client.address", "value": {"stringValue": peer_ip(_val(v))}})
        elif k == "http.path":
            if _val(v) is not None:
                path, _, query = _val(v).partition("?")
                out.append({"key": "url.path", "value": {"stringValue": path}})
                if query:
                    out.append({"key": "url.query", "value": {"stringValue": query}})
        elif k == "http.version":
            version = VERSIONS.get(_val(v))
            if version:
                out.append({"key": "network.protocol.version", "value": {"stringValue": version}})
        else:
            out.append(a)
    out.append({"key": "url.scheme", "value": {"stringValue": "http"}})
    lr["attributes"] = out
    return lr


def convert_scope(scope):
    scope = dict(scope)
    if scope.get("name") == SCOPE_V15:
        scope["name"] = SCOPE_V16
    return scope


def convert_request(req):
    """An OTLP/JSON logs request with every `request`-scope record in the 1.6+ shape."""
    req = copy.deepcopy(req)
    for rl in req.get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            if sl.get("scope", {}).get("name") != SCOPE_V15:
                continue
            sl["scope"] = convert_scope(sl["scope"])
            sl["logRecords"] = [convert_record(lr) for lr in sl.get("logRecords", [])]
    return req
