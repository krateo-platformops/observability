#!/usr/bin/env python3
"""krateo-observability's HyperDX MongoDB credential: rendered, then used against MongoDB.

1. Render (helm template, default values) and check:
   - clickstack-config has no MONGO_URI and no mongodb://user:password@ anywhere;
   - every consumer (every container that envFroms clickstack-config, every container with a
     MONGO_URI, in any workload kind) gets MONGO_URI from secretKeyRef mongodb-hyperdx-credentials/uri,
     and no check-alerts CronJob renders (it has no env hook);
   - the MongoDBCommunity user hyperdx has passwordSecretRef {mongodb-hyperdx-credentials, password};
   - mongodb-hyperdx-credentials holds exactly username, password, uri (data, not stringData), a
     generated 32-character password, and a uri with the same scheme, user, host, port, database and
     query as upstream's ConfigMap URI (rendered here from the vendored clickstack chart);
   - upstream's default MongoDB password (read at run time from the vendored chart's values.yaml, so
     no literal is in this file) reaches no LIVE consumer: the Secret the MongoDB user's
     passwordSecretRef resolves to, the uri every MONGO_URI resolves to, and no ConfigMap or pod spec;
   - the objects that still hold it as a credential are exactly DEAD_AFTER_ROTATION (upstream's,
     dead once the user is rotated, emptied in the follow-up release) plus CLICKHOUSE_APP (the same
     literal as the ClickHouse `app` user's password, a separate credential): nothing new;
   - the init container waits with the app container's image, and the generation annotation on the
     pod template is the one on the Secret;
   - each override that would undo the wiring fails the render.
2. Run MongoDB in the image MCK runs for this CR, with the hyperdx user on upstream's default
   password (where krateo-057 is today), start the RENDERED wait-for-mongodb-auth init container
   (HyperDX image) with the rendered uri, check it keeps waiting, then set the generated password as
   MCK would and check it finishes, that the rendered uri logs in, and that the default no longer
   does. Passwords come from the render or are generated here, and are never printed.

Run: python3 tests/hyperdx-mongo-credential-docker.py [repo-root]   (--render-only skips step 2)
Exit 0 = every check passed, 1 = at least one did not.
"""
import base64
import json
import os
import pathlib
import re
import secrets
import string
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse

import yaml

CHART = "charts/krateo-observability"
SUBCHART_TGZ = "charts/krateo-observability/charts/clickstack-3.2.0.tgz"
SECRET = "mongodb-hyperdx-credentials"
GEN_KEY = "krateo.io/mongodb-credential-generation"
INIT = "wait-for-mongodb-auth"
# The image MCK 1.10.0 runs for a MongoDBCommunity CR: community.mongodb.{repo, name, imageType}
# (mongodb-operator does not override them) and the CR's spec.version.
MONGO_IMAGE = "quay.io/mongodb/mongodb-community-server:{version}-ubi8"
# Where upstream's default MongoDB password is ALSO the ClickHouse `app` user's password
# (hyperdx.secrets.CLICKHOUSE_APP_PASSWORD, same literal). A separate credential, unchanged here.
CLICKHOUSE_APP = {
    "Secret/clickstack-secret: stringData.CLICKHOUSE_APP_PASSWORD",
    "ClickHouseCluster/krateo-clickstack-clickhouse: spec.settings.extraUsersConfig.users.app.password",
    "Deployment/krateo-clickstack: env DEFAULT_CONNECTIONS[0].password",
}
WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod", "ReplicaSet"}

# Upstream objects that keep the default in 0.1.44. Dead after rotation (MongoDB refuses it, step 2
# shows it), emptied in the follow-up: changing the Secret MCK still watches in the same upgrade as the
# switch could set its value as the user's password until the CR update lands.
DEAD_AFTER_ROTATION = {
    "Secret/krateo-clickstack-mongodb-password: stringData.password",
    "Secret/clickstack-secret: stringData.MONGODB_PASSWORD",
}

failures = []


def check(ok, msg):
    print(("ok   " if ok else "FAIL ") + msg)
    if not ok:
        failures.append(msg)
    return ok


def helm_template(*args, expect_fail=False):
    r = subprocess.run(["helm", "template", *args], capture_output=True, text=True)
    if expect_fail:
        return r
    if r.returncode != 0:
        print(r.stderr)
        raise SystemExit("helm template failed")
    return [x for x in yaml.safe_load_all(r.stdout) if x]


def render(root, *extra, expect_fail=False):
    return helm_template("krateo-observability", str(root / CHART), "-n", "krateo-system", *extra,
                         expect_fail=expect_fail)


def render_with(root, overlay, expect_fail=False):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(overlay, f)
    try:
        return render(root, "-f", f.name, expect_fail=expect_fail)
    finally:
        os.unlink(f.name)


def upstream_default(root):
    """hyperdx.secrets.MONGODB_PASSWORD from the vendored chart, never written in this file."""
    with tarfile.open(root / SUBCHART_TGZ) as t:
        values = yaml.safe_load(t.extractfile("clickstack/values.yaml"))
    return str(values["hyperdx"]["secrets"]["MONGODB_PASSWORD"])


def upstream_uri(root):
    """Upstream's MONGO_URI as its ConfigMap renders it, with this chart's fullnameOverride."""
    docs = helm_template("krateo-observability", str(root / SUBCHART_TGZ), "-n", "krateo-system",
                         "--set", "fullnameOverride=krateo-clickstack", "-s", "templates/hyperdx/configmap.yaml")
    return docs[0]["data"]["MONGO_URI"]


def secret_values(doc):
    out = {}
    for k, v in (doc.get("data") or {}).items():
        out[f"data.{k}"] = base64.b64decode(v).decode()
    for k, v in (doc.get("stringData") or {}).items():
        out[f"stringData.{k}"] = "" if v is None else str(v)
    return out


def pod_specs(doc):
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        yield spec["jobTemplate"]["spec"]["template"]["spec"]
    elif doc["kind"] == "Pod":
        yield spec
    elif "template" in spec:
        yield spec["template"]["spec"]


def containers(doc):
    for ps in pod_specs(doc):
        for c in (ps.get("initContainers") or []) + (ps.get("containers") or []):
            yield c


def leaves(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from leaves(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from leaves(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def default_password_positions(docs, default):
    """Every place the default is used AS A CREDENTIAL: a URI password, a Secret value, a field
    named like a password (also inside JSON-valued strings, such as DEFAULT_CONNECTIONS). The
    literal is also the MongoDB user and database name, so a plain substring search would flag
    names, not credentials."""
    found = set()
    userinfo = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]*:([^@/\s]*)@", re.I)
    pw_key = re.compile(r"(pass|pwd)", re.I)
    last = lambda path: re.split(r"[.\[]", path)[-1]  # noqa: E731

    def uri_hits(s):
        return any(urllib.parse.unquote(m.group(1)) == default for m in userinfo.finditer(s))

    for d in docs:
        where = f"{d['kind']}/{d['metadata']['name']}"
        if d["kind"] == "Secret":
            for k, v in secret_values(d).items():
                # A Secret value is a credential unless it is the user name, which here is the literal.
                if v == default and not re.search(r"\.(user|username)$", k, re.I):
                    found.add(f"{where}: {k}")
                if uri_hits(v):
                    found.add(f"{where}: URI password in {k}")
            continue
        envname = env_paths(d) if d["kind"] in WORKLOADS else {}
        for path, s in leaves(d):
            label = f"env {envname[path]}" if path in envname else path
            if uri_hits(s):
                found.add(f"{where}: URI password at {label}")
            if s == default and pw_key.search(last(path)):
                found.add(f"{where}: {label}")
            if s[:1] in "[{":
                try:
                    inner = json.loads(s)
                except ValueError:
                    continue
                for ipath, iv in leaves(inner):
                    if iv == default and pw_key.search(last(ipath)):
                        found.add(f"{where}: {label}{ipath if ipath.startswith('[') else '.' + ipath}")
                    if uri_hits(iv):
                        found.add(f"{where}: URI password at {label}{ipath}")
    return found


def env_paths(doc):
    """path of each env value string -> its variable name, for readable positions."""
    out = {}
    for path, _ in leaves(doc):
        m = re.match(r"(.*env\[\d+\])\.value$", path)
        if m:
            node = doc
            for part in re.findall(r"[^.\[\]]+|\[\d+\]", m.group(1)):
                node = node[int(part[1:-1])] if part.startswith("[") else node[part]
            out[path] = node["name"]
    return out


def mongo_uri_ref(c):
    for e in c.get("env") or []:
        if e.get("name") == "MONGO_URI":
            return e
    return None


def check_render(root, docs, default):
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    cm = by.get(("ConfigMap", "clickstack-config"), {})
    check("MONGO_URI" not in (cm.get("data") or {}), "clickstack-config has no MONGO_URI")
    creds_in_cm = [k for k, v in (cm.get("data") or {}).items()
                   if re.search(r"mongodb(\+srv)?://[^/\s@]*:[^/\s@]*@", str(v), re.I)]
    check(not creds_in_cm, f"clickstack-config has no mongodb://user:password@ anywhere ({creds_in_cm or 'none'})")
    other_cms = [d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap"
                 and re.search(r"mongodb(\+srv)?://[^/\s@]*:[^/\s@]*@", json.dumps(d.get("data") or {}), re.I)]
    check(not other_cms, f"no ConfigMap holds a MongoDB URI with credentials ({other_cms or 'none'})")

    # Consumers: whoever envFroms clickstack-config (where MONGO_URI used to come from) or names it.
    want = {"name": "MONGO_URI", "valueFrom": {"secretKeyRef": {"name": SECRET, "key": "uri"}}}
    consumers = []
    for d in docs:
        if d["kind"] not in WORKLOADS:
            continue
        for c in containers(d):
            from_cm = any((ef.get("configMapRef") or {}).get("name") == "clickstack-config" for ef in c.get("envFrom") or [])
            if from_cm or mongo_uri_ref(c):
                consumers.append((f"{d['kind']}/{d['metadata']['name']}", c))
    check(len(consumers) >= 2, f"found the MONGO_URI consumers ({', '.join(sorted(f'{w}:{c['name']}' for w, c in consumers))})")
    for where, c in consumers:
        check(mongo_uri_ref(c) == want, f"{where} container {c['name']}: MONGO_URI from secretKeyRef {SECRET}/uri")
    check(not [d for d in docs if d["kind"] == "CronJob"], "no check-alerts CronJob renders (hyperdx.tasks stays off)")

    mdb = next((d for d in docs if d["kind"] == "MongoDBCommunity"), None)
    users = {u["name"]: u for u in ((mdb or {}).get("spec") or {}).get("users", [])}
    hx = users.get("hyperdx", {})
    check(hx.get("db") == "hyperdx" and hx.get("passwordSecretRef") == {"name": SECRET, "key": "password"},
          f"MongoDBCommunity user hyperdx: passwordSecretRef {SECRET}/password")
    check(hx.get("scramCredentialsSecretName") == "krateo-clickstack-mongodb-scram"
          and [r["name"] for r in hx.get("roles", [])] == ["dbOwner", "clusterMonitor"],
          "MongoDBCommunity user hyperdx: upstream's roles and scram Secret name, unchanged")

    s = by.get(("Secret", SECRET))
    if not check(s is not None, f"Secret {SECRET} rendered"):
        return None
    data = s.get("data") or {}
    check(set(data) == {"username", "password", "uri"} and "stringData" not in s,
          f"Secret {SECRET}: keys username, password, uri (data, not stringData)")
    v = secret_values(s)
    pw, uri = v.get("data.password", ""), v.get("data.uri", "")
    check(v.get("data.username") == "hyperdx", f"Secret {SECRET}: username hyperdx")
    check(len(pw) >= 32 and all(ch in string.ascii_letters + string.digits for ch in pw),
          f"Secret {SECRET}: a generated password of at least 32 alphanumerics")
    check(pw != default, f"Secret {SECRET}: the password is not upstream's default")
    got, up = urllib.parse.urlsplit(uri), urllib.parse.urlsplit(upstream_uri(root))
    shape = lambda u: (u.scheme, u.username, u.hostname, u.port, u.path, u.query)  # noqa: E731
    check(shape(got) == shape(up), f"Secret {SECRET}: uri has upstream's shape "
          f"({got.scheme}://{got.username}:***@{got.hostname}:{got.port}{got.path}?{got.query})")
    check(got.password == pw, f"Secret {SECRET}: uri carries the Secret's password")
    check(s["metadata"].get("annotations", {}).get(GEN_KEY) == "1", f"Secret {SECRET}: annotated {GEN_KEY}=1")

    dep = by.get(("Deployment", "krateo-clickstack"), {})
    tpl = dep["spec"]["template"]
    check((tpl["metadata"].get("annotations") or {}).get(GEN_KEY) == s["metadata"]["annotations"].get(GEN_KEY),
          "HyperDX pod template carries the Secret's credential generation")
    inits = {c["name"]: c for c in tpl["spec"].get("initContainers") or []}
    app = tpl["spec"]["containers"][0]
    check(list(inits) == ["wait-for-mongodb", INIT], f"init containers: wait-for-mongodb, then {INIT}")
    check(inits.get(INIT, {}).get("image") == app["image"], f"{INIT} runs the app container's image ({app['image']})")

    # (a) No live consumer uses the default: resolve every reference to the rendered Secret it names.
    def resolve(name, key):
        sec = by.get(("Secret", name))
        return None if sec is None else secret_values(sec).get(f"data.{key}", secret_values(sec).get(f"stringData.{key}"))
    ref = hx.get("passwordSecretRef") or {}
    live = resolve(ref.get("name"), ref.get("key", "password"))
    check(live is not None and live != default and ref.get("name") != "krateo-clickstack-mongodb-password",
          f"live: the MongoDB user's password ({ref.get('name')}/{ref.get('key')}) is not upstream's default")
    for where, c in consumers:
        r = (mongo_uri_ref(c) or {}).get("valueFrom", {}).get("secretKeyRef", {})
        u = resolve(r.get("name"), r.get("key"))
        check(u is not None and urllib.parse.urlsplit(u).password not in (None, default),
              f"live: {where} container {c['name']}: MONGO_URI resolves to a uri without upstream's default")
    found = default_password_positions(docs, default)
    in_specs = sorted(p for p in found - CLICKHOUSE_APP if not p.startswith("Secret/"))
    check(not in_specs, f"live: no ConfigMap or pod spec holds upstream's default as a credential ({in_specs or 'none'})")
    # (b) Where it is still held: exactly the two dead upstream objects, nothing new.
    held = found - CLICKHOUSE_APP
    check(held == DEAD_AFTER_ROTATION, "upstream's default is held only by the objects dead after rotation "
          f"({sorted(held - DEAD_AFTER_ROTATION) or 'nothing new'}; missing {sorted(DEAD_AFTER_ROTATION - held) or 'none'})")
    for p in sorted(found & DEAD_AFTER_ROTATION):
        print(f"     note: dead after rotation; emptied in the follow-up: {p}")
    for p in sorted(found & CLICKHOUSE_APP):
        print(f"     note: the same literal is still the ClickHouse app user's password (lockdown S9/S10): {p}")
    return pw, uri, inits.get(INIT)


def check_guards(root):
    env_without = {"clickstack": {"hyperdx": {"deployment": {"env": [{"name": "HDX_NODE_CONSOLE_CAPTURE", "value": "false"}]}}}}
    cases = [
        ("an env override that drops MONGO_URI", env_without, "must contain MONGO_URI from secretKeyRef"),
        ("an initContainers override that drops the auth wait",
         {"clickstack": {"hyperdx": {"deployment": {"initContainers": []}}}}, "must contain wait-for-mongodb-auth"),
        ("MONGO_URI back in clickstack-config",
         {"clickstack": {"hyperdx": {"config": {"MONGO_URI": "mongodb://h/x"}}}}, "MONGO_URI must stay null"),
        ("an emptied MONGODB_PASSWORD (in this release, the Secret MCK still watches)",
         {"clickstack": {"hyperdx": {"secrets": {"MONGODB_PASSWORD": ""}}}}, "MONGODB_PASSWORD must keep upstream's value"),
        ("hyperdx.tasks enabled (the CronJob has no env hook)",
         {"clickstack": {"hyperdx": {"tasks": {"enabled": True}}}}, "tasks.enabled must stay false"),
        ("a users override back on upstream's password Secret",
         {"clickstack": {"mongodb": {"spec": {"users": [{"name": "hyperdx", "db": "hyperdx",
                                                          "passwordSecretRef": {"name": "krateo-clickstack-mongodb-password"},
                                                          "roles": [{"name": "dbOwner", "db": "hyperdx"}],
                                                          "scramCredentialsSecretName": "x"}]}}}},
         "passwordSecretRef {name: mongodb-hyperdx-credentials, key: password}"),
        ("the generation annotation removed",
         {"clickstack": {"hyperdx": {"deployment": {"podAnnotations": {GEN_KEY: None}}}}}, f"must carry {GEN_KEY}"),
    ]
    for label, overlay, needle in cases:
        r = render_with(root, overlay, expect_fail=True)
        check(r.returncode != 0 and needle in r.stderr, f"guard: {label} fails the render")


# --- step 2: MongoDB ------------------------------------------------------------------------------

def docker(*args, env=None, check_=True, timeout=None):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check_,
                          env={**os.environ, **(env or {})}, timeout=timeout)


def mongosh(name, js, env):
    """Run JS in the MongoDB container; secrets reach it as environment variables only."""
    flags = [x for k in env for x in ("-e", k)]
    return docker("exec", *flags, name, "mongosh", "--quiet", "--norc", "--eval", js, env=env, check_=False)


def check_mongo(docs, pw, uri, init, default):
    mdb = next(d for d in docs if d["kind"] == "MongoDBCommunity")
    image = MONGO_IMAGE.format(version=mdb["spec"]["version"])
    host = urllib.parse.urlsplit(uri).hostname
    tag = secrets.token_hex(4)
    net, mongo, waiter = f"mongo-cred-{tag}", f"mongo-cred-db-{tag}", f"mongo-cred-init-{tag}"
    admin_pw = secrets.token_urlsafe(24)
    # Where the live user is today: upstream's default password, before the operator gets the new one.
    old_uri = uri.replace(f":{pw}@", f":{urllib.parse.quote(default, safe='')}@", 1)
    docker("network", "create", net)
    try:
        # The rendered uri names the MCK Service; the container answers to that name on this network.
        # mongod directly, as MCK's agent starts it: the image's docker-entrypoint.py is not what runs
        # in the cluster, and it refuses to start on a v6.19+ kernel (a MongoDB 8.0 tcmalloc issue).
        docker("run", "-d", "--name", mongo, "--network", net, "--network-alias", host,
               "--entrypoint", "mongod", image, "--auth", "--bind_ip_all")
        for _ in range(60):
            if mongosh(mongo, "db.runCommand({ping: 1}).ok", {}).stdout.strip() == "1":
                break
            time.sleep(1)
        else:
            return check(False, f"[mongo] {image} answered")
        check(True, f"[mongo] {image} answered")
        r = mongosh(mongo, "db.getSiblingDB('admin').createUser({user: 'root', pwd: process.env.ADMIN_PW,"
                    " roles: ['root']}).ok", {"ADMIN_PW": admin_pw})
        check(r.stdout.strip() == "1", "[mongo] admin user created (localhost exception)")
        as_admin = "db.getSiblingDB('admin').auth('root', process.env.ADMIN_PW);"
        # The user as MCK defines it: db hyperdx, dbOwner on hyperdx, clusterMonitor on admin, SCRAM-SHA-1 and -256.
        r = mongosh(mongo, as_admin + "db.getSiblingDB('hyperdx').createUser({user: 'hyperdx', pwd: process.env.OLD_PW,"
                    " roles: [{role: 'dbOwner', db: 'hyperdx'}, {role: 'clusterMonitor', db: 'admin'}],"
                    " mechanisms: ['SCRAM-SHA-1', 'SCRAM-SHA-256']}).ok", {"ADMIN_PW": admin_pw, "OLD_PW": default})
        check(r.stdout.strip() == "1", "[mongo] user hyperdx created with upstream's default password (today's state)")

        # The rendered init container, in the HyperDX image, with the rendered uri.
        docker("run", "-d", "--name", waiter, "--network", net, "-e", "MONGO_URI", "-w", init["workingDir"],
               "--entrypoint", init["command"][0], init["image"], *init["command"][1:], env={"MONGO_URI": uri})
        time.sleep(12)
        st = docker("inspect", "-f", "{{.State.Running}}", waiter).stdout.strip()
        logs = docker("logs", waiter, check_=False)
        out = logs.stdout + logs.stderr
        check(st == "true" and "AuthenticationFailed" in out,
              f"[init] {INIT} keeps waiting while MongoDB has the old password ({out.strip().splitlines()[-1] if out.strip() else 'no output'})")
        check(pw not in out, f"[init] {INIT} does not print the password")

        # What MCK does when the password Secret changes: new SCRAM credentials for the same user, in place.
        r = mongosh(mongo, as_admin + "db.getSiblingDB('hyperdx').updateUser('hyperdx', {pwd: process.env.NEW_PW,"
                    " mechanisms: ['SCRAM-SHA-1', 'SCRAM-SHA-256']}); 1", {"ADMIN_PW": admin_pw, "NEW_PW": pw})
        check(r.returncode == 0, "[mongo] user hyperdx gets the generated password")
        code = docker("wait", waiter, timeout=60).stdout.strip()
        logs = docker("logs", waiter, check_=False)
        out = logs.stdout + logs.stderr
        check(code == "0" and "MongoDB accepts the hyperdx credentials" in out,
              f"[init] {INIT} finishes once MongoDB accepts the rendered uri (exit {code})")
        check(pw not in out, f"[init] {INIT} still does not print the password")

        # The rendered uri, from a client: a write and a read in the hyperdx database.
        probe = ("const c = new Mongo(process.env.URI); const d = c.getDB('hyperdx');"
                 " d.cred_probe.insertOne({ok: 1}); print(d.cred_probe.countDocuments({}));")
        r = mongosh(mongo, probe, {"URI": uri})
        check(r.returncode == 0 and r.stdout.strip() == "1", "[mongo] the rendered uri logs in and reads/writes hyperdx")
        r = mongosh(mongo, "try { new Mongo(process.env.URI).getDB('hyperdx').cred_probe.countDocuments({}); print('in') }"
                    " catch (e) { print(e.codeName || e.name) }", {"URI": old_uri})
        check("AuthenticationFailed" in r.stdout, "[mongo] upstream's default, the value left in the dead objects, is refused after the change")
    finally:
        for c in (waiter, mongo):
            docker("rm", "-f", c, check_=False)
        docker("network", "rm", net, check_=False)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = pathlib.Path(args[0] if args else ".").resolve()
    subprocess.run(["helm", "dependency", "build", str(root / CHART)], check=True, capture_output=True)
    default = upstream_default(root)
    docs = render(root)
    res = check_render(root, docs, default)
    again = {d["metadata"]["name"]: d for d in render(root) if d["kind"] == "Secret"}.get(SECRET)
    if res and again:
        check(secret_values(again)["data.password"] != res[0],
              "helm template (no cluster, lookup returns nothing) generates a fresh password each run")
    check_guards(root)
    if res and "--render-only" not in sys.argv:
        check_mongo(docs, res[0], res[1], res[2], default)
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
