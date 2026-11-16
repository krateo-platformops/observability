#!/usr/bin/env python3
"""krateo-observability upgraded in place on kind: an older chart installed, data written, the new
chart upgraded over it, and the data and wiring checked again. Written for 0.1.48 (clickstack 3.2.0,
HyperDX 2.35.0) -> 0.1.49 (clickstack 3.5.0, HyperDX 2.39.1, dashboard provisioner on).

The whole stack runs as the platform runs it: this repo's clickhouse-operator and mongodb-operator
charts in krateo-system, then the chart (only global.storageClassName=standard and
defaultAlerts.enabled=false differ from its defaults: kind has no standard-rwo, and the Alert and
Metric CRDs live in alert-provider-crds).

Before the upgrade it writes:
  - ClickHouse: otel_logs, otel_traces (tests/fixtures DDL) and krateo_audit rows; every user's
    SHOW GRANTS; the `cmp` database.
  - HyperDX (MongoDB): the first admin and team (POST /api/register/password, as alert-provider's
    bootstrap does), and through the v2 API with the admin's access key, the objects alert-provider
    creates: a generic webhook, a one-tile dashboard (alert-provider's tile shape) and a tile alert
    on it, plus a second dashboard standing in for a user's own.
After the upgrade it checks:
  1. HyperDX's pod rolled, is Ready, and runs app + dashboard-watcher;
  2. the provisioner process runs every minute and finishes with nothing to provision, /dashboards
     is empty, and no provision-dashboards line reports a failure;
  3. the provisioner uses the app container's MONGO_URI (secretKeyRef mongodb-hyperdx-credentials/uri)
     and the pod references no other MongoDB credential;
  4. RBAC: one Role (get/list/watch configmaps) bound to ServiceAccount krateo-clickstack in
     krateo-system, no ClusterRole or ClusterRoleBinding, and `kubectl auth can-i` agrees;
  5. the team mode (DASHBOARD_PROVISIONER_ALL_TEAMS=true, no TEAM_ID);
  6. MongoDB data: the same team, the same access key (compared by hash), the webhook, both
     dashboards with their tile ids, the alert with its fields, the sources unchanged;
  7. alert-provider's calls still work: PUT of the alert and of the dashboard (same tile id, the
     alert survives), a new dashboard + alert, POST /api/v2/charts/series;
  8. ClickHouse: SHOW GRANTS byte-identical for every user, `cmp` present, the otel_* and
     krateo_audit row counts and content hashes identical; whether the ClickHouse, Keeper and
     MongoDB pods restarted is reported (pod UIDs);
  9. then, past the zero-dashboard state: a ConfigMap labelled hyperdx.io/dashboard=true is
     provisioned into the team within two minutes, flagged provisioned, beside the user's dashboard.
Credentials are generated here or read from the cluster and never printed (hashes only).

Run: python3 tests/clickstack-upgrade-kind.py --from OLD_CHART_DIR [--to NEW_CHART_DIR] [--keep]
     OLD_CHART_DIR: e.g. `git archive origin/main charts | tar -x -C /tmp/old` -> /tmp/old/charts/krateo-observability
It creates kind cluster clickstack-upgrade and deletes it at the end (--keep keeps it).
Exit 0 = every check passed.
"""
import argparse
import base64
import hashlib
import http.client
import json
import pathlib
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

NS = "krateo-system"
REL = "krateo-observability"
CLUSTER = "clickstack-upgrade"
CH_POD = "krateo-clickstack-clickhouse-clickhouse-0-0-0"
SA = "krateo-clickstack"
USERS = ["default", "app", "otelcollector", "sse_proxy", "snowplow_reader", "analyst", "mcp_reader",
         "nightly_reader", "audit_sealer", "audit_verifier", "otel_writer", "hyperdx", "slo_rollup",
         "usage_collector"]
OTEL_LOGS_DDL = """CREATE TABLE IF NOT EXISTS default.otel_logs (
    Timestamp DateTime64(9) CODEC(Delta(8), ZSTD(1)),
    TimestampTime DateTime DEFAULT toDateTime(Timestamp),
    TraceId String CODEC(ZSTD(1)), SpanId String CODEC(ZSTD(1)), TraceFlags UInt8,
    SeverityText LowCardinality(String) CODEC(ZSTD(1)), SeverityNumber UInt8,
    ServiceName LowCardinality(String) CODEC(ZSTD(1)), Body String CODEC(ZSTD(1)),
    ResourceSchemaUrl LowCardinality(String) CODEC(ZSTD(1)),
    ResourceAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    ScopeSchemaUrl LowCardinality(String) CODEC(ZSTD(1)), ScopeName String CODEC(ZSTD(1)),
    ScopeVersion LowCardinality(String) CODEC(ZSTD(1)),
    ScopeAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)),
    LogAttributes Map(LowCardinality(String), String) CODEC(ZSTD(1)), EventName String CODEC(ZSTD(1))
) ENGINE = MergeTree PARTITION BY toDate(TimestampTime) PRIMARY KEY (ServiceName, TimestampTime)
ORDER BY (ServiceName, TimestampTime, Timestamp) TTL TimestampTime + toIntervalDay(7)"""

failures = []


def check(ok, msg):
    print(f"{'ok  ' if ok else 'FAIL'} {msg}", flush=True)
    if not ok:
        failures.append(msg)
    return ok


def step(msg):
    print(f"\n== {msg} ({time.strftime('%H:%M:%S')})", flush=True)


def run(*args, check_=True, input_=None, timeout=None):
    r = subprocess.run(list(args), capture_output=True, text=True, input=input_, timeout=timeout)
    if check_ and r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args[:6])} ...\n{r.stderr[-2000:]}")
    return r


class Kube:
    def __init__(self, kubeconfig):
        self.kc = ["--kubeconfig", str(kubeconfig)]

    def k(self, *args, **kw):
        return run("kubectl", *self.kc, *args, **kw)

    def kn(self, *args, **kw):
        return self.k("-n", NS, *args, **kw)

    def json(self, *args):
        return json.loads(self.kn(*args, "-o", "json").stdout)

    def helm(self, *args, **kw):
        return run("helm", "--kubeconfig", self.kc[1], *args, **kw)


def wait(what, fn, timeout=900, every=5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 -- polled until the deadline
            last = e
        time.sleep(every)
    raise SystemExit(f"timed out waiting for {what} (last: {last})")


def pod_ready(kube, name):
    r = kube.kn("get", "pod", name, "-o", "json", check_=False)
    if r.returncode != 0:
        return False
    p = json.loads(r.stdout)
    conds = {c["type"]: c["status"] for c in p["status"].get("conditions", [])}
    return conds.get("Ready") == "True" and p


def hyperdx_pod(kube):
    pods = kube.json("get", "pods", "-l", "app=krateo-clickstack")["items"]
    pods = [p for p in pods if p["metadata"].get("deletionTimestamp") is None]
    return pods


def ch(kube, sql, fmt="TSVRaw"):  # noqa: D401
    """SQL through clickhouse-client in the server pod, as `default` (the operator's no_password)."""
    r = kube.kn("exec", "-i", CH_POD, "-c", "clickhouse-server", "--", "clickhouse-client",
                "--format", fmt, "--multiquery", input_=sql, check_=False)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-600:])
    return r.stdout


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortForward:
    def __init__(self, kube, target, remote):
        self.port = free_port()
        self.p = subprocess.Popen(["kubectl", *kube.kc, "-n", NS, "port-forward", target, f"{self.port}:{remote}"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait(f"port-forward {target}", self._up, timeout=60, every=1)

    def _up(self):
        with socket.socket() as s:
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def close(self):
        self.p.terminate()
        self.p.wait(timeout=10)


def req(port, method, path, body=None, headers=None, form=False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    h = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw.decode(errors="replace")[:300]
    return r.status, payload, r.getheader("Set-Cookie") or ""


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def unwrap(v):
    return v["data"] if isinstance(v, dict) and "data" in v else v


class HyperDX:
    """The admin's session (port 3000, the app's /api) and access key (port 8000, /api/v2)."""

    def __init__(self, kube, email, password):
        self.kube, self.email, self.password = kube, email, password
        self.app = PortForward(kube, "svc/krateo-clickstack", 3000)
        self.api = PortForward(kube, "svc/krateo-clickstack-api", 8000)
        self.sid = None

    def close(self):
        self.app.close()
        self.api.close()

    def _sid(self, cookie):
        m = re.search(r"connect\.sid=([^;]+)", cookie)
        return m.group(1) if m else None

    def register_or_login(self, register):
        if register:
            st, _, cookie = req(self.app.port, "POST", "/api/register/password",
                                 {"email": self.email, "password": self.password, "confirmPassword": self.password})
        else:
            st, _, cookie = req(self.app.port, "POST", "/api/login/password",
                                 {"email": self.email, "username": self.email, "password": self.password})
        self.sid = self._sid(cookie)
        return st, self.sid is not None

    def session(self, path):
        return req(self.app.port, "GET", path, headers={"Cookie": f"connect.sid={self.sid}"})

    def keys(self):
        _, team, _ = self.session("/api/team")
        _, me, _ = self.session("/api/me")
        return team, me

    def v2(self, method, path, body=None, key=None):
        st, payload, _ = req(self.api.port, method, "/api/v2" + path, body,
                              headers={"Authorization": f"Bearer {key}"})
        return st, unwrap(payload)


# alert-provider's request bodies (internal/hyperdx/hyperdx.go: tileFor, alertBody, EnsureWebhook).
def tile(name, source_id, where, tile_id="count"):
    return {"id": tile_id, "x": 0, "y": 0, "w": 6, "h": 3, "name": name,
            "config": {"displayType": "line", "sourceId": source_id, "asRatio": False, "fillNulls": True,
                       "select": [{"aggFn": "count", "where": where, "whereLanguage": "sql"}]}}


def alert_body(name, dashboard_id, tile_id, webhook_id):
    return {"name": name, "source": "tile", "dashboardId": dashboard_id, "tileId": tile_id,
            "interval": "5m", "threshold": 1, "thresholdType": "above",
            "channel": {"type": "webhook", "webhookId": webhook_id},
            "message": name + " threshold crossed — incident-agent will auto-triage.",
            "numConsecutiveWindows": 2}


ALERT_FIELDS = ["name", "source", "dashboardId", "tileId", "interval", "threshold", "thresholdType",
                "message", "numConsecutiveWindows", "channel"]


def ch_snapshot(kube):
    snap = {"grants": {}, "data": {}}
    for u in USERS:
        snap["grants"][u] = ch(kube, f"SHOW GRANTS FOR `{u}`")
    snap["users"] = ch(kube, "SELECT name FROM system.users ORDER BY name")
    snap["cmp"] = ch(kube, "SELECT name, engine FROM system.databases WHERE name = 'cmp'").strip()
    for t in ("otel_logs", "otel_traces", "krateo_audit"):
        snap["data"][t] = ch(kube, f"SELECT count(), sum(cityHash64(*)) FROM default.{t}").strip()
    return snap


def pod_uids(kube):
    out = {}
    for p in kube.json("get", "pods")["items"]:
        name = p["metadata"]["name"]
        for prefix in ("krateo-clickstack-clickhouse", "krateo-clickstack-keeper", "krateo-clickstack-mongodb"):
            if name.startswith(prefix):
                restarts = sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))
                out[name] = (p["metadata"]["uid"], restarts)
    return out


def wait_stack(kube):
    wait("ClickHouse Ready", lambda: pod_ready(kube, CH_POD))
    wait("Keeper Ready", lambda: pod_ready(kube, "krateo-clickstack-keeper-keeper-0-0"))
    wait("MongoDB Ready", lambda: pod_ready(kube, "krateo-clickstack-mongodb-0"))
    wait("HyperDX rolled out", lambda: kube.kn("rollout", "status", "deploy/krateo-clickstack", "--timeout=10s",
                                               check_=False).returncode == 0, timeout=900)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    ap.add_argument("--from", dest="old", required=True)
    ap.add_argument("--to", dest="new")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    root = pathlib.Path(a.root)
    old, new = pathlib.Path(a.old), pathlib.Path(a.new or root / "charts" / "krateo-observability")
    tmp = pathlib.Path(tempfile.mkdtemp())
    kubeconfig = tmp / "kubeconfig"
    values = ["--set", "global.storageClassName=standard", "--set", "defaultAlerts.enabled=false"]

    step("kind cluster, operators")
    run("kind", "create", "cluster", "--name", CLUSTER, "--kubeconfig", str(kubeconfig), "--wait", "180s")
    kube = Kube(kubeconfig)
    hx = None
    try:
        for img in ("docker.hyperdx.io/hyperdx/hyperdx:2.35.0", "docker.hyperdx.io/hyperdx/hyperdx:2.39.1"):
            if run("docker", "image", "inspect", img, check_=False).returncode == 0:
                run("kind", "load", "docker-image", img, "--name", CLUSTER, check_=False, timeout=900)
        kube.k("create", "namespace", NS)
        for op in ("clickhouse-operator", "mongodb-operator"):
            run("helm", "dependency", "build", str(root / "charts" / op))
            kube.helm("upgrade", "--install", op, str(root / "charts" / op), "-n", NS, "--wait", "--timeout", "10m")
        for c in (old, new):
            run("helm", "dependency", "build", str(c))

        step(f"install the old chart ({old})")
        kube.helm("upgrade", "--install", REL, str(old), "-n", NS, *values, "--timeout", "15m")
        wait_stack(kube)
        old_hx = hyperdx_pod(kube)
        check(len(old_hx) == 1 and old_hx[0]["spec"]["containers"][0]["image"].endswith(":2.35.0"),
              "old chart: one HyperDX pod on 2.35.0")

        step("seed ClickHouse")
        wait("ClickHouse answers", lambda: ch(kube, "SELECT 1").strip() == "1", timeout=300)
        ch(kube, OTEL_LOGS_DDL)
        for f in ("otel_traces.sql", "krateo_audit.sql"):
            ddl = (root / "tests" / "fixtures" / f).read_text().replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS default.", 1)
            ch(kube, ddl)
        ch(kube, "INSERT INTO default.otel_logs (Timestamp, ServiceName, SeverityText, Body, ResourceAttributes, LogAttributes) "
                 "SELECT now64(9) - toIntervalSecond(number), 'e2e-' || toString(number % 7), 'INFO', 'line ' || toString(number), "
                 "map('telemetry.source', ''), map('n', toString(number)) FROM numbers(5000)")
        ch(kube, "INSERT INTO default.otel_traces (Timestamp, TraceId, SpanId, SpanName, ServiceName, Duration, StatusCode) "
                 "SELECT now64(9) - toIntervalSecond(number), hex(number), hex(number * 7), 'GET /x', 'e2e', number * 1000, 'Ok' "
                 "FROM numbers(3000)")
        ch(kube, "INSERT INTO default.krateo_audit (Timestamp, ServiceName, Body) "
                 "SELECT now64(9) - toIntervalSecond(number), 'audit', 'event ' || toString(number) FROM numbers(1000)")
        before = ch_snapshot(kube)
        check(before["cmp"] == "cmp\tAtomic", f"before: database cmp exists ({before['cmp'] or 'missing'})")
        check(all(before["grants"][u] for u in USERS), f"before: SHOW GRANTS answers for all {len(USERS)} users")
        uids_before = pod_uids(kube)

        step("seed HyperDX (MongoDB)")
        email, password = "e2e-admin@example.com", "Aa1!" + secrets.token_urlsafe(18)
        hx = HyperDX(kube, email, password)
        wait("HyperDX answers /api/installation", lambda: req(hx.app.port, "GET", "/api/installation")[0] == 200, timeout=300)
        st, ok = hx.register_or_login(register=True)
        check(ok, f"register the first admin and team (HTTP {st}, session cookie {'set' if ok else 'missing'})")
        team, me = hx.keys()
        key = (me or {}).get("accessKey", "")
        team_id = (team or {}).get("_id") or (team or {}).get("id")
        check(bool(key) and bool(team_id), f"team {team_id} and the admin's access key (sha {sha(key)})")
        st, srcs = hx.v2("GET", "/sources", key=key)
        logs = next((s for s in srcs if s.get("kind") == "log"), None) if st == 200 else None
        check(logs is not None, f"GET /api/v2/sources: {len(srcs) if st == 200 else st} sources, one of kind log")
        st, wh = hx.v2("POST", "/webhooks", {"name": "e2e-alert-provider", "service": "generic",
                                             "url": "http://alert-provider.krateo-system.svc:8080/hook",
                                             "description": "e2e", "body": '{"text": "{{title}}"}'}, key=key)
        check(st == 200 and wh.get("id"), f"POST /api/v2/webhooks (HTTP {st})")
        st, d1 = hx.v2("POST", "/dashboards", {"name": "e2e-alert-tile", "tags": [],
                                               "tiles": [tile("e2e-alert-tile", logs["id"], "ServiceName = 'e2e-1'")]}, key=key)
        check(st == 200 and d1.get("id"), f"POST /api/v2/dashboards, alert-provider's tile shape (HTTP {st})")
        st, d2 = hx.v2("POST", "/dashboards", {"name": "e2e-user-dashboard", "tags": ["mine"],
                                               "tiles": [tile("errors", logs["id"], "SeverityText = 'ERROR'")]}, key=key)
        check(st == 200 and d2.get("id"), f"POST /api/v2/dashboards, a user's dashboard (HTTP {st})")
        tile_id = d1["tiles"][0]["id"]
        st, al = hx.v2("POST", "/alerts", alert_body("e2e-alert", d1["id"], tile_id, wh["id"]), key=key)
        check(st == 200 and al.get("id"), f"POST /api/v2/alerts, alert-provider's body (HTTP {st})")
        mongo_before = {"sources": srcs, "dashboards": hx.v2("GET", "/dashboards", key=key)[1],
                        "alerts": hx.v2("GET", "/alerts", key=key)[1], "webhooks": hx.v2("GET", "/webhooks", key=key)[1]}
        hx.close()
        hx = None

        step(f"upgrade to the new chart ({new})")
        r = kube.helm("upgrade", REL, str(new), "-n", NS, *values, "--timeout", "15m", check_=False)
        check(r.returncode == 0, "helm upgrade succeeds" + ("" if r.returncode == 0 else f": {r.stderr.strip()[-400:]}"))
        time.sleep(10)
        wait_stack(kube)
        pods = wait("one HyperDX pod", lambda: (lambda p: p if len(p) == 1 else None)(hyperdx_pod(kube)), timeout=300)
        p = pods[0]
        name = p["metadata"]["name"]
        wait("HyperDX Ready", lambda: pod_ready(kube, name), timeout=600)
        p = kube.json("get", "pod", name)
        cs = {c["name"]: c for c in p["spec"]["containers"]}

        step("1. HyperDX up")
        check(p["metadata"]["uid"] != old_hx[0]["metadata"]["uid"], "the HyperDX pod rolled")
        check(list(cs) == ["app", "dashboard-watcher"], f"containers: {list(cs)}")
        check(cs["app"]["image"].endswith(":2.39.1"), f"app image {cs['app']['image']}")
        check(all(c["ready"] for c in p["status"]["containerStatuses"]), "every container Ready")
        check(cs["app"]["readinessProbe"]["httpGet"]["path"] == "/ready", "readiness reads /ready")
        check(p["spec"].get("serviceAccountName") == SA, f"runs as ServiceAccount {p['spec'].get('serviceAccountName')}")

        step("2. the provisioner, with zero dashboards")
        time.sleep(120)  # past two cron ticks ('0 * * * * *'), and past the watcher's start-up peak
        log = kube.kn("logs", name, "-c", "app").stdout
        prov = [line for line in log.splitlines() if "DASH-PROVISION" in line]
        check(any("provision-dashboards started" in line for line in prov), f"provision-dashboards started ({len(prov)} lines)")
        check(any("provision-dashboards finished" in line for line in prov), "provision-dashboards finished")
        # The task's own failure lines (tasks/index.js, provisionDashboards/index.js). Not the OTLP
        # exporter's health check, which fails on kind (no node agent at $(HOST_IP):4318).
        bad = [line for line in prov if re.search(
            r"provision-dashboards failed|Failed to (read|parse|provision)|Skipping invalid|Uncaught exception|"
            r"Unhandled rejection|Task execution failed|DASHBOARD_PROVISIONER_\w+ (is|environment)", line)]
        check(not bad, f"no provision-dashboards line reports a failure ({bad[:2] or 'none'})")
        check(any("Connection established to MongoDB" in line for line in prov), "the provisioner process connected to MongoDB")
        ls = kube.kn("exec", name, "-c", "app", "--", "ls", "-A", "/dashboards").stdout.split()
        check(ls == [], f"/dashboards is empty ({ls})")
        ps = kube.kn("exec", name, "-c", "app", "--", "sh", "-c", "ps -eo args 2>/dev/null || ps").stdout
        check("provision-dashboards" in ps, "a provision-dashboards process runs in the app container")
        wlog = kube.kn("logs", name, "-c", "dashboard-watcher").stdout
        check("403" not in wlog and "Forbidden" not in wlog, "dashboard-watcher: no 403 from the API server")
        check("Initial sync complete" in wlog, "dashboard-watcher: initial sync complete")
        # Upstream's 64Mi limit OOMKilled it within a minute of each start (0.1.49's first kind run).
        st = {c["name"]: c for c in kube.json("get", "pod", name)["status"]["containerStatuses"]}
        w = st["dashboard-watcher"]
        check(w["restartCount"] == 0, f"dashboard-watcher never restarted ({w['restartCount']}, last {w.get('lastState') or 'none'})")
        mem = kube.kn("exec", name, "-c", "dashboard-watcher", "--", "sh", "-c",
                      "echo $(cat /sys/fs/cgroup/memory.peak) $(cat /sys/fs/cgroup/memory.max)", check_=False).stdout.split()
        if len(mem) == 2:
            print(f"     dashboard-watcher memory peak {int(mem[0]) >> 20}Mi of limit {int(mem[1]) >> 20}Mi")

        step("3. the provisioner's MongoDB credential")
        env = {e["name"]: e for e in cs["app"].get("env", [])}
        check(env.get("MONGO_URI", {}).get("valueFrom") == {"secretKeyRef": {"name": "mongodb-hyperdx-credentials", "key": "uri"}},
              "app container: MONGO_URI from secretKeyRef mongodb-hyperdx-credentials/uri")
        refs = sorted({e["valueFrom"]["secretKeyRef"]["name"] for c in p["spec"]["containers"] + p["spec"]["initContainers"]
                       for e in c.get("env", []) if "secretKeyRef" in e.get("valueFrom", {})})
        check(refs == ["mongodb-hyperdx-credentials"], f"the pod's only secretKeyRef is mongodb-hyperdx-credentials ({refs})")
        check("MONGO_URI" not in cs["dashboard-watcher"].get("env", []) and not cs["dashboard-watcher"].get("envFrom"),
              "dashboard-watcher has no MongoDB credential")
        cm = kube.json("get", "configmap", "clickstack-config")["data"]
        check("MONGO_URI" not in cm, "clickstack-config still has no MONGO_URI")

        step("4. RBAC")
        role = kube.json("get", "role", f"{SA}-dashboard-provisioner")
        check(role["rules"] == [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["list", "get", "watch"]}],
              f"Role rules: {role['rules']}")
        rb = kube.json("get", "rolebinding", f"{SA}-dashboard-provisioner")
        check(rb["subjects"] == [{"kind": "ServiceAccount", "name": SA, "namespace": NS}]
              and rb["roleRef"]["kind"] == "Role", f"RoleBinding: Role -> ServiceAccount {SA}")
        crs = [x["metadata"]["name"] for x in json.loads(kube.k("get", "clusterroles,clusterrolebindings", "-o", "json").stdout)["items"]
               if "dashboard" in x["metadata"]["name"]]
        check(not crs, f"no dashboard ClusterRole or ClusterRoleBinding ({crs or 'none'})")
        who = f"system:serviceaccount:{NS}:{SA}"
        can = lambda *x: kube.k("auth", "can-i", "--as", who, *x, check_=False).stdout.strip()  # noqa: E731
        check(can("list", "configmaps", "-n", NS) == "yes", "can list configmaps in krateo-system")
        check(can("watch", "configmaps", "-n", NS) == "yes", "can watch configmaps in krateo-system")
        check(can("list", "configmaps", "-n", "default") == "no", "cannot list configmaps in another namespace")
        check(can("get", "secrets", "-n", NS) == "no", "cannot get secrets")
        check(can("create", "configmaps", "-n", NS) == "no", "cannot create configmaps")

        step("5. team mode")
        check(env.get("DASHBOARD_PROVISIONER_ALL_TEAMS", {}).get("value") == "true" and "DASHBOARD_PROVISIONER_TEAM_ID" not in env,
              "DASHBOARD_PROVISIONER_ALL_TEAMS=true, no DASHBOARD_PROVISIONER_TEAM_ID")

        step("6. MongoDB data")
        hx = HyperDX(kube, email, password)
        st, ok = hx.register_or_login(register=False)
        check(ok, f"the seeded admin logs in on 2.39.1 (HTTP {st})")
        team2, me2 = hx.keys()
        check(((team2 or {}).get("_id") or (team2 or {}).get("id")) == team_id, "the same team")
        check(sha((me2 or {}).get("accessKey", "")) == sha(key), "the same access key (hash)")
        st, dash = hx.v2("GET", "/dashboards", key=key)
        by = {d["id"]: d for d in dash} if st == 200 else {}
        check(d1["id"] in by and by[d1["id"]]["tiles"][0]["id"] == tile_id, "alert-provider's dashboard, same id and tile id")
        check(d2["id"] in by and by[d2["id"]]["name"] == "e2e-user-dashboard", "the user's dashboard, same id")
        st, alerts = hx.v2("GET", "/alerts", key=key)
        live = next((x for x in alerts if x["id"] == al["id"]), None) if st == 200 else None
        old_al = next(x for x in mongo_before["alerts"] if x["id"] == al["id"])
        diff = [f for f in ALERT_FIELDS if (live or {}).get(f) != old_al.get(f)]
        check(live is not None and not diff, f"the alert, same id, fields unchanged ({diff or 'none differ'})")
        print(f"     alert fields added by 2.39.1: {sorted(set(live or {}) - set(old_al))}")
        st, srcs2 = hx.v2("GET", "/sources", key=key)
        strip = lambda ss: sorted((json.dumps({k: v for k, v in s.items() if k not in ("updatedAt",)}, sort_keys=True) for s in ss))  # noqa: E731
        check(st == 200 and strip(srcs2) == strip(mongo_before["sources"]), "the sources, unchanged (new default sources apply to new installs only)")
        st, whs = hx.v2("GET", "/webhooks", key=key)
        check(st == 200 and [w["id"] for w in whs] == [w["id"] for w in mongo_before["webhooks"]], "the webhook, same id")

        step("7. alert-provider's calls on 2.39.1")
        st, _ = hx.v2("PUT", f"/alerts/{al['id']}", alert_body("e2e-alert", d1["id"], tile_id, wh["id"]), key=key)
        check(st == 200, f"PUT /api/v2/alerts/:id with the same body (HTTP {st})")
        st, upd = hx.v2("PUT", f"/dashboards/{d1['id']}", {"name": "e2e-alert-tile", "tags": [],
                        "tiles": [tile("e2e-alert-tile", logs["id"], "ServiceName = 'e2e-2'", tile_id)]}, key=key)
        check(st == 200 and upd["tiles"][0]["id"] == tile_id, f"PUT /api/v2/dashboards/:id keeps the tile id (HTTP {st})")
        st, alerts = hx.v2("GET", "/alerts", key=key)
        check(st == 200 and any(x["id"] == al["id"] for x in alerts), "the alert survives the tile update")
        st, d3 = hx.v2("POST", "/dashboards", {"name": "e2e-new-tile", "tags": [],
                                               "tiles": [tile("e2e-new-tile", logs["id"], "ServiceName = 'e2e-3'")]}, key=key)
        st2, al3 = hx.v2("POST", "/alerts", alert_body("e2e-alert-new", d3.get("id", ""), d3["tiles"][0]["id"] if st == 200 else "",
                                                       wh["id"]), key=key)
        check(st == 200 and st2 == 200, f"a new dashboard and alert (HTTP {st}, {st2})")
        end = int(time.time() * 1000)
        st, series = hx.v2("POST", "/charts/series", {"startTime": end - 3 * 3600_000, "endTime": end, "granularity": "5m",
                           "series": [{"sourceId": logs["id"], "aggFn": "count", "where": "ServiceName = 'e2e-1'",
                                       "whereLanguage": "sql", "groupBy": ["ServiceName"]}]}, key=key)
        total = sum(int(b.get("series_0.data") or 0) for b in series) if st == 200 and isinstance(series, list) else -1
        # Three hours hold every seeded row (5000 s, written minutes ago), so neither edge cuts one.
        want = int(ch(kube, "SELECT count() FROM default.otel_logs WHERE ServiceName = 'e2e-1'").strip())
        check(st == 200 and total == want, f"POST /api/v2/charts/series counts what ClickHouse counts ({total} vs {want})")

        step("8. ClickHouse")
        after = ch_snapshot(kube)
        for u in USERS:
            check(after["grants"][u] == before["grants"][u], f"SHOW GRANTS FOR {u}: byte-identical")
        check(after["users"] == before["users"], "the same ClickHouse users")
        check(after["cmp"] == "cmp\tAtomic", "database cmp present")
        for t in ("otel_logs", "otel_traces", "krateo_audit"):
            check(after["data"][t] == before["data"][t], f"{t}: count and content hash unchanged ({after['data'][t]})")
        uids_after = pod_uids(kube)
        for n, (uid, rs) in sorted(uids_before.items()):
            now = uids_after.get(n)
            print(f"     {n}: {'NOT restarted' if now and now[0] == uid and now[1] == rs else 'RESTARTED/replaced'}"
                  f" (restarts {rs} -> {now[1] if now else '?'})")
        cr = json.loads(kube.kn("get", "clickhouseclusters.clickhouse.com", "krateo-clickstack-clickhouse", "-o", "json").stdout)
        check("logger" not in cr["spec"].get("settings", {}), "ClickHouseCluster spec.settings has no logger (upstream's nulled)")

        step("9. one labelled ConfigMap is provisioned (beyond this PR's zero dashboards)")
        dash_json = json.dumps({"name": "e2e-provisioned", "tiles": [], "tags": ["e2e"]})
        kube.kn("apply", "-f", "-", input_=json.dumps({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "e2e-dashboard", "namespace": NS, "labels": {"hyperdx.io/dashboard": "true"}},
            "data": {"e2e-provisioned.json": dash_json}}))
        got = wait("the provisioned dashboard", lambda: next((d for d in hx.v2("GET", "/dashboards", key=key)[1]
                                                               if d["name"] == "e2e-provisioned"), None), timeout=180, every=10)
        check(bool(got), "a ConfigMap labelled hyperdx.io/dashboard=true becomes a dashboard in the team")
        ls = kube.kn("exec", name, "-c", "app", "--", "ls", "-A", "/dashboards").stdout.split()
        print(f"     /dashboards now: {ls}")
        st, dash = hx.v2("GET", "/dashboards", key=key)
        check(any(d["id"] == d2["id"] for d in dash), "the user's dashboard is still there beside it")
    finally:
        if hx:
            hx.close()
        if not a.keep:
            run("kind", "delete", "cluster", "--name", CLUSTER, "--kubeconfig", str(kubeconfig), check_=False)
        else:
            print(f"kept: kind cluster {CLUSTER}, kubeconfig {kubeconfig}")

    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
