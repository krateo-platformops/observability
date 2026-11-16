#!/usr/bin/env python3
"""krateo-observability's ClickHouse users: the rendered config, then loaded into ClickHouse.

1. Render (helm template, default values plus a componentValues-shaped overlay) and check the shape:
   - every chart user is in extraUsersConfig.users with password_sha256_hex {"@from_env": <var>},
     a profile that exists, and exactly ONE grants item holding ONE GRANT statement (users.d applies
     only the first item and drops the rest without a word);
   - each statement grants exactly the per-object privileges below, never ON *.*, default.* or
     system.* wholesale, never WITH GRANT OPTION, no ALTER/DROP/TRUNCATE on an audit table;
   - the profiles carry the limits below;
   - containerTemplate.env maps each variable to <Secret>/password_sha256_hex;
   - one Secret per user with exactly username, password, password_sha256_hex, and the hash is the
     sha256 of the password; no generated password appears in the ClickHouseCluster CR;
   - default, app and otelcollector are untouched (no users.default entry, no defaultUserPassword),
     and otel-clickhouse-credentials still names otelcollector: no client is switched;
   - each user's variable reads its own Secret and no other variable reads it;
   - the render fails if an override of containerTemplate.env drops an entry, or points another
     variable at a user's Secret;
   - extraConfig.startup_scripts holds exactly the one CREATE DATABASE IF NOT EXISTS cmp.
2. Load the rendered users.d YAML and startup_scripts into the ClickHouse version krateo-057 runs,
   with the hashes from a 0600 env file, and check per user: login, readonly, SHOW GRANTS (one line
   per object, as expected), CHECK GRANT on the audit tables, and the per-query settings each client
   sends. Then usage_collector against `cmp`, which must exist before it ever logs in: its own
   CREATE TABLE IF NOT EXISTS and INSERT work, SELECT, any other table and CREATE DATABASE are
   refused, and a restart leaves `cmp` and its rows as they were.
   Passwords are generated here and never printed.

Run: python3 tests/clickhouse-users-docker.py [repo-root]   (--render-only skips step 2)
Exit 0 = every check passed, 1 = at least one did not.
"""
import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import yaml

CHART = "charts/krateo-observability"
IMAGE = "clickhouse/clickhouse-server:26.10.3.7"  # SELECT version() on krateo-057 (since 2026-11-11)

TELEMETRY6 = ["otel_logs", "otel_traces", "otel_metrics_gauge", "otel_metrics_sum",
              "otel_metrics_histogram", "krateo_audit"]
OTEL_TABLES = ["otel_logs", "otel_traces", "otel_traces_trace_id_ts", "otel_metrics_gauge",
               "otel_metrics_sum", "otel_metrics_histogram", "otel_metrics_exponential_histogram",
               "otel_metrics_summary", "krateo_audit"]


def d(*tables, privs="SELECT"):
    return {f"default.{t}": privs for t in tables}


# user -> (env variable, Secret, profile, {object: privileges}). The S0 kind run (2026-11-10,
# users-test-C) is where these were proven against 057's DDL, the real seal/verify Jobs and the MVs.
USERS = {
    "sse_proxy": ("CLICKHOUSE_USER_SSE_PROXY_SHA256", "clickhouse-sse-proxy-credentials",
                  "portal_reader", d("otel_logs", "krateo_audit")),
    "snowplow_reader": ("CLICKHOUSE_USER_SNOWPLOW_READER_SHA256", "clickhouse-snowplow-reader-credentials",
                        "portal_reader_snowplow", d(*TELEMETRY6)),
    "analyst": ("CLICKHOUSE_USER_ANALYST_SHA256", "clickhouse-analyst-credentials", "analyst",
                d("otel_logs", "otel_traces", "otel_metrics_gauge", "otel_metrics_sum", "otel_metrics_histogram",
                  "otel_metrics_exponential_histogram", "otel_metrics_summary", "krateo_audit")),
    "mcp_reader": ("CLICKHOUSE_USER_MCP_READER_SHA256", "clickhouse-mcp-reader-credentials", "mcp_reader",
                   d(*TELEMETRY6)),
    "nightly_reader": ("CLICKHOUSE_USER_NIGHTLY_READER_SHA256", "clickhouse-nightly-reader-credentials",
                       "batch_reader", d("otel_logs")),
    "audit_sealer": ("CLICKHOUSE_USER_AUDIT_SEALER_SHA256", "clickhouse-audit-sealer-credentials", "default",
                     {"default.krateo_audit": "SELECT",
                      "default.krateo_audit_worm": "SELECT, INSERT, CREATE TABLE",
                      "default.krateo_audit_seal": "SELECT, INSERT, CREATE TABLE",
                      "default.krateo_audit_worm_mv": "CREATE VIEW"}),
    "audit_verifier": ("CLICKHOUSE_USER_AUDIT_VERIFIER_SHA256", "clickhouse-audit-verifier-credentials",
                       "audit_readonly",
                       {**d("krateo_audit", "krateo_audit_worm", "krateo_audit_seal"),
                        "default.krateo_audit_worm_mv": "SHOW TABLES"}),
    "otel_writer": ("CLICKHOUSE_USER_OTEL_WRITER_SHA256", "clickhouse-otel-writer-credentials", "default",
                    {**d(*OTEL_TABLES, privs="SELECT, INSERT, CREATE TABLE"),
                     "default.otel_traces_trace_id_ts_mv": "CREATE VIEW"}),
    "hyperdx": ("CLICKHOUSE_USER_HYPERDX_SHA256", "clickhouse-hyperdx-credentials", "hyperdx",
                {**d(*TELEMETRY6), "system.data_skipping_indices": "SELECT"}),
    "slo_rollup": ("CLICKHOUSE_USER_SLO_ROLLUP_SHA256", "clickhouse-slo-rollup-credentials", "default",
                   {**d("otel_metrics_sum", "otel_metrics_histogram"),
                    "default.krateo_slo_daily": "SELECT, INSERT, CREATE TABLE, ALTER MODIFY TTL",
                    "default.krateo_slo_daily_mv": "CREATE VIEW"}),
    "usage_collector": ("CLICKHOUSE_USER_USAGE_COLLECTOR_SHA256", "clickhouse-usage-collector-credentials",
                        "default", {"cmp.health_records": "INSERT, CREATE TABLE"}),
}
# The platform creates `cmp` (extraConfig.startup_scripts); usage_collector never can.
STARTUP_SCRIPTS = {"throw_on_error": False,
                   "scripts": [{"query": "CREATE DATABASE IF NOT EXISTS cmp ENGINE = Atomic"}]}
# cmp-usage-collector's own DDL (internal/health/clickhouse.go TableDDL), for cmp.health_records.
HEALTH_DDL = ("CREATE TABLE IF NOT EXISTS cmp.health_records ( record_id String, org LowCardinality(String),"
              " tenant LowCardinality(String), service LowCardinality(String), component LowCardinality(String),"
              " status Enum8('OK' = 1, 'Warning' = 2, 'Critical' = 3, 'Unknown' = 4), reason String,"
              " checked_at DateTime64(3, 'UTC') DEFAULT now64(3)) ENGINE = ReplacingMergeTree(checked_at)"
              " PARTITION BY toYYYYMM(checked_at) ORDER BY (org, tenant, service, component, record_id)")
# `default`'s future hash: a Secret and an env variable, no users.default entry yet.
DEFAULT_PW = ("CLICKHOUSE_USER_DEFAULT_SHA256", "clickhouse-default-password")
SYSTEM_GRANTS_ALLOWED = {"system.data_skipping_indices"}

GiB = 1024 ** 3


def caps(met, threads=None, conc=None, mem=None, ro=2, cancel=True):
    p = {"readonly": ro}
    if cancel:
        p["cancel_http_readonly_queries_on_client_close"] = 1
    p["max_execution_time"] = met
    c = {"max_execution_time": {"max": met}}
    if threads:
        p["max_threads"] = threads
        c["max_threads"] = {"max": threads}
    if conc:
        p["max_concurrent_queries_for_user"] = conc
        c["max_concurrent_queries_for_user"] = {"const": True}
    if mem:
        p["max_memory_usage"] = mem
        c["max_memory_usage"] = {"max": mem}
    p["constraints"] = c
    return p


PROFILES = {
    "portal_reader": caps(8, 2, 16),
    "portal_reader_snowplow": caps(8, 2, 8),
    "analyst": caps(30, 4, 4, 4 * GiB),
    "mcp_reader": caps(30, 2, 4, 2 * GiB, ro=0, cancel=False),
    "batch_reader": caps(60, 2, 2),
    "hyperdx": caps(60),
    "audit_readonly": {"readonly": 2},
}

# A componentValues-shaped overlay: an extra user the installer adds (057's chops), which must
# deep-merge with the chart's users and leave them alone. No credential in it.
OVERLAY = {"clickstack": {"clickhouse": {"cluster": {"spec": {"settings": {"extraUsersConfig": {"users": {
    "chops": {"profile": "default", "grants": [{"query": "GRANT SELECT ON system.disks"}]}}}}}}}}}

failures = []


def check(ok, msg):
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        failures.append(msg)
    return ok


def parse_grant(stmt):
    """'GRANT a, b ON x, c ON y' -> {x: {a, b}, y: {c}}. Privilege lists never contain ' ON '."""
    body = re.sub(r"\s+", " ", stmt).strip()
    if not body.startswith("GRANT "):
        return None
    body = body[len("GRANT "):]
    out = {}
    for m in re.finditer(r"\s*(.+?)\s+ON\s+([\w.*]+)\s*(?:,|$)", body):
        out.setdefault(m.group(2), set()).update(p.strip() for p in m.group(1).split(","))
    return out


def privset(s):
    return {p.strip().replace("ALTER MODIFY TTL", "ALTER TTL") for p in s.split(",")}


def render(root, *extra, expect_fail=False):
    cmd = ["helm", "template", "krateo-observability", str(root / CHART), "-n", "krateo-system", *extra]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if expect_fail:
        return r
    if r.returncode != 0:
        print(r.stderr)
        raise SystemExit("helm template failed")
    return [x for x in yaml.safe_load_all(r.stdout) if x]


def check_render(root, docs):
    chc = next(x for x in docs if x["kind"] == "ClickHouseCluster")
    spec = chc["spec"]
    euc = spec["settings"]["extraUsersConfig"]
    users, profiles = euc["users"], euc.get("profiles", {})
    env = {e["name"]: e for e in spec["containerTemplate"].get("env", [])}
    secrets_ = {x["metadata"]["name"]: x for x in docs if x["kind"] == "Secret"}
    chc_text = json.dumps(chc)

    for prof, want in PROFILES.items():
        check(profiles.get(prof) == want, f"profile {prof} carries its limits and constraints")

    for user, (var, sec, prof, objects) in USERS.items():
        u = users.get(user) or {}
        check(u.get("password_sha256_hex") == {"@from_env": var}, f"{user}: password_sha256_hex from env {var}")
        check(not ({"password", "no_password", "password_double_sha1_hex"} & set(u)), f"{user}: no plaintext or empty password key")
        check(u.get("profile") == prof and (prof == "default" or prof in profiles), f"{user}: profile {prof}")
        g = u.get("grants") or []
        one = len(g) == 1 and set(g[0]) == {"query"} and g[0]["query"].count("GRANT") == 1
        check(one, f"{user}: exactly one grants item, one GRANT statement")
        stmt = g[0]["query"] if g else ""
        got = parse_grant(stmt) or {}
        check(got == {o: {x.strip() for x in p.split(",")} for o, p in objects.items()},
              f"{user}: grants exactly {sorted(objects)}")
        bad = [o for o in got if o.endswith(".*") or o == "*" or (o.startswith("system.") and o not in SYSTEM_GRANTS_ALLOWED)]
        check(not bad, f"{user}: nothing on *.*, default.* or system.* wholesale ({bad or 'none'})")
        check("GRANT OPTION" not in stmt.upper(), f"{user}: no WITH GRANT OPTION")
        audit_bad = [(o, p) for o, ps in got.items() if o.startswith("default.krateo_audit")
                     for p in ps if re.match(r"(ALTER|DROP|TRUNCATE|ALL)", p)]
        check(not audit_bad, f"{user}: no ALTER/DROP/TRUNCATE on an audit table")
        if user != "audit_sealer":
            check("INSERT" not in got.get("default.krateo_audit_seal", set())
                  and "INSERT" not in got.get("default.krateo_audit_worm", set()),
                  f"{user}: no INSERT on krateo_audit_seal / krateo_audit_worm")

    env_list = spec["containerTemplate"].get("env", [])
    for user, (var, sec, *_rest) in list(USERS.items()) + [("default", DEFAULT_PW)]:
        e = env.get(var, {})
        ref = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
        check(ref == {"name": sec, "key": "password_sha256_hex"}, f"env {var} -> {sec}/password_sha256_hex")
        readers = [x["name"] for x in env_list
                   if ((x.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name") == sec]
        check(readers == [var], f"Secret {sec} is read by {var} alone ({readers})")
        s = secrets_.get(sec)
        if not check(s is not None, f"Secret {sec} rendered"):
            continue
        data = s.get("data") or {}
        check(set(data) == {"username", "password", "password_sha256_hex"} and "stringData" not in s,
              f"Secret {sec}: keys username, password, password_sha256_hex (data, not stringData)")
        pw = base64.b64decode(data.get("password", "")).decode()
        check(base64.b64decode(data.get("username", "")).decode() == user, f"Secret {sec}: username {user}")
        check(len(pw) == 32 and all(c in string.ascii_letters + string.digits for c in pw),
              f"Secret {sec}: a generated 32-character password")
        check(base64.b64decode(data.get("password_sha256_hex", "")).decode() == hashlib.sha256(pw.encode()).hexdigest(),
              f"Secret {sec}: password_sha256_hex is sha256(password)")
        check(pw not in chc_text and hashlib.sha256(pw.encode()).hexdigest() not in chc_text,
              f"Secret {sec}: neither password nor hash appears in the ClickHouseCluster CR")

    # Nothing about default, app, otelcollector or the clients changes.
    check(spec["settings"].get("extraConfig", {}).get("startup_scripts") == STARTUP_SCRIPTS,
          "extraConfig.startup_scripts is exactly CREATE DATABASE IF NOT EXISTS cmp ENGINE = Atomic")
    check(all("CREATE DATABASE" not in (u.get("grants") or [{}])[0].get("query", "").upper()
              for u in users.values() if u.get("grants")),
          "no user is granted CREATE DATABASE")
    check("default" not in users, "users.default is not overridden (default stays as the operator makes it)")
    check("defaultUserPassword" not in spec["settings"], "no settings.defaultUserPassword")
    check({"app", "otelcollector"} <= set(users) and all("password" in users[u] for u in ("app", "otelcollector")),
          "app and otelcollector keep upstream's definition")
    otel = secrets_.get("otel-clickhouse-credentials") or {}
    check((otel.get("stringData") or {}).get("username") == "otelcollector",
          "otel-clickhouse-credentials still names otelcollector (the collectors are not switched)")
    return chc, euc


def env_entry(var, sec):
    return {"name": var, "valueFrom": {"secretKeyRef": {"name": sec, "key": "password_sha256_hex"}}}


def render_env(root, env):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump({"clickstack": {"clickhouse": {"cluster": {"spec": {"containerTemplate": {"env": env}}}}}}, f)
    try:
        return render(root, "-f", f.name, expect_fail=True)
    finally:
        os.unlink(f.name)


def check_guard(root):
    r = render_env(root, [env_entry("CLICKHOUSE_USER_SSE_PROXY_SHA256", "clickhouse-sse-proxy-credentials")])
    check(r.returncode != 0 and "must contain CLICKHOUSE_USER_SNOWPLOW_READER_SHA256" in r.stderr,
          "a containerTemplate.env override that drops entries fails the render")
    full = [env_entry(var, sec) for var, sec, *_r in list(USERS.values()) + [DEFAULT_PW]]
    r = render_env(root, full)
    check(r.returncode == 0, "the chart's own env list, repeated in an override, renders")
    # usage_collector's variable borrowing another user's Secret (the krateo-slo-rollup 0.1.1 shape).
    borrowed = [env_entry(v, "clickhouse-otel-writer-credentials" if v == "CLICKHOUSE_USER_USAGE_COLLECTOR_SHA256" else s)
                for v, s in ((e["name"], e["valueFrom"]["secretKeyRef"]["name"]) for e in full)]
    r = render_env(root, borrowed)
    check(r.returncode != 0 and "CLICKHOUSE_USER_USAGE_COLLECTOR_SHA256 reads Secret clickhouse-otel-writer-credentials" in r.stderr,
          "usage_collector's variable reading another user's Secret fails the render")
    # A second variable reading usage_collector's Secret.
    r = render_env(root, full + [env_entry("CLICKHOUSE_USER_OTHER_SHA256", "clickhouse-usage-collector-credentials")])
    check(r.returncode != 0 and "entry CLICKHOUSE_USER_OTHER_SHA256 reads Secret clickhouse-usage-collector-credentials" in r.stderr,
          "another variable reading usage_collector's Secret fails the render")


# --- step 2: ClickHouse ---------------------------------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ch(port, sql, user="default", key="", database=None):
    headers = {"X-ClickHouse-User": user, "X-ClickHouse-Key": key}
    if database:
        headers["X-ClickHouse-Database"] = database
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", data=sql.encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return True, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        m = re.search(r"Code: (\d+)", body)
        return False, m.group(1) if m else body[:120]
    except Exception as e:  # noqa: BLE001 - connection not ready yet
        return False, type(e).__name__


def wait_ready(port):
    for _ in range(90):
        if ch(port, "SELECT 1") == (True, "1"):
            return True
        time.sleep(1)
    return False


def check_usage_collector(port, name, k):
    """cmp exists before usage_collector's first login; then its whole life cycle, and a restart."""
    u = "usage_collector"
    # The first query the server answers: startup_scripts ran before the ports opened.
    check(ch(port, "SELECT engine FROM system.databases WHERE name = 'cmp'") == (True, "Atomic"),
          "[ch] database cmp exists (Atomic) when the server first answers, before usage_collector logs in")
    check(ch(port, "SELECT count() FROM system.query_log WHERE user = 'usage_collector'") in ((True, "0"), (False, "60")),
          "[ch] usage_collector has run nothing yet")
    check(ch(port, "SELECT value FROM system.metrics WHERE metric = 'StartupScriptsExecutionState'") == (True, "1"),
          "[ch] StartupScriptsExecutionState = 1 (Success)")
    # A real table usage_collector must not reach, so a refusal is ACCESS_DENIED, not UNKNOWN_TABLE.
    check(ch(port, "CREATE TABLE default.otel_logs (Body String) ENGINE = MergeTree ORDER BY tuple()")[0],
          "[ch] (setup) default.otel_logs exists")
    ok, out = ch(port, "SHOW GRANTS", u, k)
    check((ok, out) == (True, "GRANT INSERT, CREATE TABLE ON cmp.health_records TO usage_collector"),
          f"[ch] {u}: SHOW GRANTS is exactly 'GRANT INSERT, CREATE TABLE ON cmp.health_records TO usage_collector'")
    check(ch(port, HEALTH_DDL, u, k)[0], f"[ch] {u}: CREATE TABLE IF NOT EXISTS cmp.health_records (the collector's DDL)")
    check(ch(port, HEALTH_DDL, u, k)[0], f"[ch] {u}: the same CREATE TABLE IF NOT EXISTS again, on the existing table")
    row = '{"record_id":"r1","org":"o","tenant":"t","service":"s","component":"c","status":"OK","reason":""}'
    check(ch(port, "INSERT INTO cmp.health_records FORMAT JSONEachRow\n" + row, u, k)[0],
          f"[ch] {u}: INSERT INTO cmp.health_records")
    check(ch(port, "INSERT INTO health_records FORMAT JSONEachRow\n" + row, u, k, database="cmp")[0],
          f"[ch] {u}: INSERT with database=cmp (unqualified table)")
    check(ch(port, "SELECT count() FROM cmp.health_records") == (True, "2"), "[ch] (as default) the two rows landed")
    refused = [
        ("SELECT * FROM cmp.health_records", "SELECT on cmp.health_records"),
        ("SELECT count() FROM cmp.health_records", "SELECT count() on cmp.health_records"),
        ("INSERT INTO default.otel_logs FORMAT JSONEachRow\n{\"Body\":\"x\"}", "INSERT into default.otel_logs"),
        ("SELECT * FROM default.otel_logs", "SELECT on default.otel_logs"),
        ("CREATE DATABASE cmp2", "CREATE DATABASE cmp2"),
        ("CREATE DATABASE IF NOT EXISTS cmp", "CREATE DATABASE IF NOT EXISTS cmp (an existing one)"),
        ("CREATE TABLE cmp.other (x UInt8) ENGINE = MergeTree ORDER BY x", "CREATE TABLE cmp.other"),
        ("CREATE TABLE default.health_records (x UInt8) ENGINE = MergeTree ORDER BY x", "CREATE TABLE default.health_records"),
        ("DROP TABLE cmp.health_records", "DROP TABLE cmp.health_records"),
        ("TRUNCATE TABLE cmp.health_records", "TRUNCATE cmp.health_records"),
        ("ALTER TABLE cmp.health_records DELETE WHERE 1", "ALTER DELETE on cmp.health_records"),
        ("DROP DATABASE cmp", "DROP DATABASE cmp"),
    ]
    for sql, what in refused:
        check(ch(port, sql, u, k) == (False, "497"), f"[ch] {u}: refused (497 ACCESS_DENIED): {what}")
    # Restart: the startup script meets an existing cmp; nothing is lost and nothing fails.
    subprocess.run(["docker", "restart", name], check=True, capture_output=True)
    if not check(wait_ready(port), "[ch] ClickHouse answered after a restart"):
        return
    check(ch(port, "SELECT value FROM system.metrics WHERE metric = 'StartupScriptsExecutionState'") == (True, "1"),
          "[ch] after the restart: StartupScriptsExecutionState = 1 (Success), on an existing cmp")
    check(ch(port, "SELECT count() FROM cmp.health_records") == (True, "2"), "[ch] after the restart: cmp.health_records keeps its rows")
    check(ch(port, HEALTH_DDL, u, k)[0] and ch(port, "INSERT INTO cmp.health_records FORMAT JSONEachRow\n" + row, u, k)[0],
          f"[ch] after the restart: {u} creates-if-not-exists and inserts again")


def check_clickhouse(euc, startup_scripts):
    pws = {u: secrets.token_urlsafe(24) for u in USERS}
    work = tempfile.mkdtemp(prefix="ch-users-")
    os.chmod(work, 0o700)
    usersd, envfile = os.path.join(work, "99-extra-users-config.yaml"), os.path.join(work, "env")
    configd = os.path.join(work, "99-extra-config.yaml")
    with open(usersd, "w") as f:
        yaml.safe_dump(euc, f, sort_keys=True)  # the operator writes this map with its keys sorted
    os.chmod(usersd, 0o644)
    # The rendered startup_scripts, as the operator writes extraConfig to config.d (the rest of
    # extraConfig, http_handlers among it, would replace the HTTP routes this test queries through).
    with open(configd, "w") as f:
        yaml.safe_dump({"startup_scripts": startup_scripts}, f, sort_keys=True)
    os.chmod(configd, 0o644)
    fd = os.open(envfile, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        for u, (var, *_r) in USERS.items():
            f.write(f"{var}={hashlib.sha256(pws[u].encode()).hexdigest()}\n")
        f.write("CLICKHOUSE_SKIP_USER_SETUP=1\n")
    port = free_port()
    name = f"ch-users-test-{port}"
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8123", "--env-file", envfile,
                    "-v", f"{usersd}:/etc/clickhouse-server/users.d/99-extra-users-config.yaml:ro",
                    "-v", f"{configd}:/etc/clickhouse-server/config.d/99-extra-config.yaml:ro", IMAGE],
                   check=True, capture_output=True)
    try:
        if not wait_ready(port):
            return check(False, "ClickHouse answered")
        check_usage_collector(port, name, pws["usage_collector"])
        for user, (var, sec, prof, objects) in USERS.items():
            k = pws[user]
            check(ch(port, "SELECT currentUser()", user, k) == (True, user), f"[ch] {user}: logs in")
            check(ch(port, "SELECT 1", user, "wrong")[0] is False, f"[ch] {user}: a wrong password is refused")
            ro = str((PROFILES.get(prof) or {}).get("readonly", 0))
            check(ch(port, "SELECT getSetting('readonly')", user, k) == (True, ro), f"[ch] {user}: readonly={ro}")
            ok, out = ch(port, "SHOW GRANTS", user, k)
            want = sorted(f"GRANT {', '.join(sorted(privset(p)))} ON {o} TO {user}" for o, p in objects.items())
            got = sorted(re.sub(r"^GRANT (.+?) ON ", lambda m: "GRANT " + ", ".join(sorted(privset(m.group(1)))) + " ON ", l)
                         for l in out.splitlines()) if ok else [out]
            check(got == want, f"[ch] {user}: SHOW GRANTS is one line per granted object ({len(want)})")
            for g, allowed in (("INSERT ON default.krateo_audit_seal", user == "audit_sealer"),
                               ("INSERT ON default.krateo_audit_worm", user == "audit_sealer"),
                               ("ALTER DELETE ON default.krateo_audit", False),
                               ("ALTER DELETE ON default.krateo_audit_worm", False),
                               ("DROP TABLE ON default.krateo_audit_seal", False),
                               ("SELECT ON system.query_log", False)):
                check(ch(port, f"CHECK GRANT {g}", user, k) == (True, "1" if allowed else "0"),
                      f"[ch] {user}: CHECK GRANT {g} = {int(allowed)}")
        # The per-query settings each client sends, under its profile.
        probes = [
            ("sse_proxy", "SELECT 1 SETTINGS readonly=2, cancel_http_readonly_queries_on_client_close=1, max_execution_time=8", True),
            ("sse_proxy", "SELECT 1 SETTINGS max_execution_time=9", False),
            ("mcp_reader", "SELECT 1 SETTINGS readonly=1", True),
            ("mcp_reader", "SELECT 1 SETTINGS max_threads=3", False),
            ("analyst", "SELECT 1 SETTINGS log_comment='q', max_block_size=1000, max_execution_time=30", True),
            ("analyst", "SELECT 1 SETTINGS readonly=1", False),
            ("analyst", "SELECT 1 SETTINGS max_memory_usage=5000000000", False),
            ("audit_verifier", "SELECT 1 SETTINGS log_comment='verify'", True),
            ("nightly_reader", "SELECT 1 SETTINGS max_result_rows=10, result_overflow_mode='break'", True),
            ("hyperdx", "SELECT 1 SETTINGS max_execution_time=61", False),
        ]
        for user, sql, want in probes:
            ok, _ = ch(port, sql, user, pws[user])
            check(ok == want, f"[ch] {user}: {'accepted' if want else 'refused'}: {sql.split('SETTINGS ')[1]}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        for p in (usersd, envfile, configd):
            os.unlink(p)
        os.rmdir(work)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = pathlib.Path(args[0] if args else ".").resolve()
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    chc, euc = check_render(root, render(root))
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(OVERLAY, f)
    try:
        docs = render(root, "-f", f.name)
    finally:
        os.unlink(f.name)
    users = next(x for x in docs if x["kind"] == "ClickHouseCluster")["spec"]["settings"]["extraUsersConfig"]["users"]
    check("chops" in users and set(USERS) <= set(users), "a componentValues user deep-merges beside the chart's users")
    check_guard(root)
    if "--render-only" not in sys.argv:
        check_clickhouse(euc, chc["spec"]["settings"]["extraConfig"]["startup_scripts"])
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
