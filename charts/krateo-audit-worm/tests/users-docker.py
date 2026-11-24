#!/usr/bin/env python3
"""The rendered seal.sh and verify.sh, as audit_sealer and audit_verifier, against a real ClickHouse.

The grants are read from krateo-observability's values, so a grant that changes there is tested here.
The server is the version krateo-observability pins; the scripts run from this chart's own client image.

Run: python3 charts/krateo-audit-worm/tests/users-docker.py [repo-root]
Needs docker, helm, PyYAML. Exit 0 = all passed.
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import uuid

import yaml

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[3]).resolve()
CHART, OBS = ROOT / "charts/krateo-audit-worm", ROOT / "charts/krateo-observability"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())
OBS_VALUES = yaml.safe_load((OBS / "values.yaml").read_text())["clickstack"]["clickhouse"]["cluster"]["spec"]
CLIENT = f"{VALUES['image']['repository']}:{VALUES['image']['tag']}"
SERVER = "clickhouse/clickhouse-server:" + OBS_VALUES["containerTemplate"]["image"]["tag"]
USERS = OBS_VALUES["settings"]["extraUsersConfig"]["users"]
PROFILES = OBS_VALUES["settings"]["extraUsersConfig"]["profiles"]
DDL = ("CREATE TABLE krateo_audit (Timestamp DateTime64(9), TraceId String, SpanId String, SeverityText LowCardinality(String),"
       " ServiceName LowCardinality(String), Body String, ResourceAttributes Map(LowCardinality(String), String),"
       " LogAttributes Map(LowCardinality(String), String)) ENGINE = MergeTree ORDER BY Timestamp")
fails = 0


def sh(*a, check=True, **k):
    return subprocess.run(list(a), check=check, capture_output=True, text=True, **k)


def check(ok, what, detail=""):
    global fails
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {what}{'' if ok else '  ' + str(detail)[:400]}", flush=True)


net, srv = "awu-" + uuid.uuid4().hex[:8], "awu-srv-" + uuid.uuid4().hex[:8]
with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    tmp.chmod(0o755)
    out = sh("helm", "template", "t", str(CHART), "--set", "seal.lagSeconds=0").stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    for n, body in next(d for d in docs if d["kind"] == "ConfigMap")["data"].items():
        (tmp / n).write_text(body)
        (tmp / n).chmod(0o644)

    def secret_of(job):
        c = next(d for d in docs if d["kind"] == "CronJob" and d["metadata"]["name"].endswith("-" + job))
        env = c["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]
        return {e["valueFrom"]["secretKeyRef"]["name"] for e in env if e["name"] in ("CH_USER", "CH_PASSWORD")}

    check(secret_of("seal") == {"clickhouse-audit-sealer-credentials"}, "the sealer reads audit_sealer's Secret", secret_of("seal"))
    check(secret_of("verify") == {"clickhouse-audit-verifier-credentials"}, "the verifier reads audit_verifier's Secret", secret_of("verify"))
    one = sh("helm", "template", "t", str(CHART), "--set", "clickhouse.credentialsSecret.name=mine").stdout
    check(one.count('name: "mine"') == 4 and "clickhouse-audit-" not in one, "a named clickhouse.credentialsSecret overrides both jobs", one.count("mine"))
    users = {"audit_sealer": "sealer", "audit_verifier": "verifier"}
    (tmp / "users.yaml").write_text(yaml.safe_dump({
        "profiles": {p: PROFILES[p] for p in {USERS[u]["profile"] for u in users} if p in PROFILES},
        "users": {"admin": {"password": "admin", "profile": "default", "quota": "default", "access_management": 1},
                  **{u: {"password": pw, "profile": USERS[u]["profile"], "grants": USERS[u]["grants"]} for u, pw in users.items()}}}))
    (tmp / "users.yaml").chmod(0o644)
    try:
        sh("docker", "network", "create", net)
        sh("docker", "run", "-d", "--name", srv, "--network", net, "-v", f"{tmp}/users.yaml:/etc/clickhouse-server/users.d/t.yaml:ro", SERVER)

        def ch(q, user="admin", pw="admin"):
            r = sh("docker", "exec", srv, "clickhouse-client", "--user", user, "--password", pw, "-q", q, check=False)
            m = re.search(r"Code: (\d+)", r.stderr)
            return (r.stdout.strip() if r.returncode == 0 else "ERR " + (m.group(1) if m else r.stderr[:80]))

        def script(name, user, pw):
            r = sh("docker", "run", "--rm", "--network", net, "-v", f"{tmp}:/scripts:ro", "--entrypoint", "sh", "-e", f"CH_HOST={srv}", "-e", "CH_PORT=9000",
                   "-e", "CH_DB=default", "-e", f"CH_USER={user}", "-e", f"CH_PASSWORD={pw}", "-e", "LAG_SECONDS=0", CLIENT, f"/scripts/{name}.sh", check=False)
            return r.returncode, r.stdout + r.stderr

        def status(user="audit_verifier", pw="verifier"):
            rc, o = script("verify", user, pw)
            lines = [x for x in o.splitlines() if '"krateo.audit.status"' in x]
            return (rc, json.loads(lines[-1])["attributes"]["krateo.audit.status"] if lines else o[-300:])

        for _ in range(90):
            if ch("SELECT 1") == "1":
                break
            time.sleep(1)
        ch(DDL)
        print(f"server {SERVER}, client {CLIENT}")
        for u, pw in users.items():
            check(ch("SELECT currentUser()", u, pw) == u, f"{u} logs in", ch("SELECT currentUser()", u, pw))
        # The collectors' insert: the materialized view must feed the WORM table without the sealer's help.
        put = lambda n: ch(f"INSERT INTO krateo_audit SELECT now64(9), lower(hex(rand64())), '', 'INFO', 'audit', toString(number), map('a','1'), map('b','2') FROM numbers({n})")
        rc, o = script("seal", "audit_sealer", "sealer")
        check(rc == 0, "first seal creates the tables, the view and the first seal", o[-400:])
        check(ch("SELECT arrayStringConcat(arraySort(groupArray(name)), ',') FROM system.tables WHERE database = 'default'")
              == "krateo_audit,krateo_audit_seal,krateo_audit_worm,krateo_audit_worm_mv", "all four objects exist")
        for n in (3, 0, 2):
            put(n) if n else None
            time.sleep(1.1)
            rc, o = script("seal", "audit_sealer", "sealer")
            check(rc == 0, f"seal after {n} rows", o[-400:])
        check(ch("SELECT sum(row_count) FROM krateo_audit_seal") == "5", "the seals cover the 5 rows", ch("SELECT sum(row_count) FROM krateo_audit_seal"))
        check(status() == (0, "ok"), "audit_verifier: ok", status())
        # What each must NOT be able to do.
        for u, pw, q in (("audit_sealer", "sealer", "ALTER TABLE krateo_audit_seal DELETE WHERE 1"), ("audit_sealer", "sealer", "TRUNCATE TABLE krateo_audit_worm"),
                         ("audit_sealer", "sealer", "DROP TABLE krateo_audit_seal"), ("audit_sealer", "sealer", "INSERT INTO krateo_audit (Body) VALUES ('x')"),
                         ("audit_verifier", "verifier", "INSERT INTO krateo_audit_seal (seal_seq) VALUES (9)"), ("audit_verifier", "verifier", "DROP VIEW krateo_audit_worm_mv")):
            got = ch(q, u, pw)
            check(got in ("ERR 497", "ERR 164"), f"{u} refused: {q[:46]}", got)
        # The verifier sees damage, and an unprotected trail, as this user.
        ch("CREATE TABLE keep AS krateo_audit_seal")
        ch("INSERT INTO keep SELECT * FROM krateo_audit_seal")
        ch("ALTER TABLE krateo_audit_seal UPDATE segment_hash = repeat('0', 64) WHERE seal_seq = 2 SETTINGS mutations_sync = 2")
        check(status()[1] == "tampered", "audit_verifier: an edited seal is tampered", status())
        ch("TRUNCATE TABLE krateo_audit_seal")
        ch("INSERT INTO krateo_audit_seal SELECT * FROM keep")
        check(status() == (0, "ok"), "restored: ok", status())
        ch("DROP VIEW krateo_audit_worm_mv")
        check(status()[1] == "unprotected", "audit_verifier: a missing view is unprotected", status())
        # Controls: without one grant, each script fails instead of reporting success.
        rc, o = script("seal", "audit_verifier", "verifier")
        check(rc != 0, "CONTROL the sealer script as audit_verifier fails", rc)
        rc, o = script("seal", "audit_sealer", "wrong")
        check(rc != 0, "CONTROL a wrong password fails", rc)
    finally:
        sh("docker", "rm", "-f", srv, check=False)
        sh("docker", "network", "rm", net, check=False)
print(f"{fails} failed")
sys.exit(1 if fails else 0)
