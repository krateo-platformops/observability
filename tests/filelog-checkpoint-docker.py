#!/usr/bin/env python3
"""A restarted otel-collector-daemonset agent loses no container log lines (0.1.37).

The chart is rendered, and its `logs` pipeline runs in the pinned collector image with the rendered
container securityContext (root, no capabilities, read-only root filesystem), with two changes for
Docker: the clickhouse exporter becomes a file exporter, and k8sattributes (which needs the API
server) is left out. A writer appends numbered CRI log lines without pausing while the agent is
stopped for several seconds and started again; a second container starts AND finishes while the
agent is down. Every line must reach the exporter.

Controls, so a pass means something:
  * the same run without checkpoints must LOSE lines (the defect this fixes);
  * the agent as the image's own user must NOT be able to use a root-owned checkpoint directory,
    which is what kubelet creates for the hostPath (why the chart sets runAsUser 0).

Not covered, and printed rather than asserted: a file rotated while the agent is down, and an agent
killed instead of stopped. The clickhouse exporter's own in-memory queue is not in this test.

Run: python3 tests/filelog-checkpoint-docker.py [repo-root]
Needs docker and helm. Exit 0 = all passed.
"""
import copy
import json
import pathlib
import subprocess
import sys
import time
import uuid

import yaml

CHART = "charts/otel-collector-daemonset"
HELPER = "python:3.12-alpine"
CKPT = "/var/lib/otelcol"
FAILS, CLEAN = [], []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)


def sh(*args, check=True, **kw):
    r = subprocess.run(list(args), capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(args[:6])}... rc={r.returncode}\n{r.stdout[-800:]}{r.stderr[-800:]}")
    return r


def render(root):
    if not list((root / CHART / "charts").glob("*")):
        sh("helm", "dependency", "build", str(root / CHART))
    out = sh("helm", "template", "ckpt", str(root / CHART), "-n", "krateo-system").stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "relay" in d.get("data", {}))
    pod = next(d for d in docs if d["kind"] == "DaemonSet")["spec"]["template"]["spec"]
    return yaml.safe_load(cm["data"]["relay"]), pod


def agent_config(relay, storage):
    pipe = copy.deepcopy(relay["service"]["pipelines"]["logs"])
    pipe["processors"] = [p for p in pipe.get("processors", []) if not p.startswith("k8sattributes")]
    pipe["exporters"] = ["file"]
    flog = copy.deepcopy(relay["receivers"]["filelog"])
    cfg = {
        "receivers": {"filelog": flog},
        "processors": {p: relay["processors"][p] for p in pipe["processors"]},
        "exporters": {"file": {"path": "/out/logs.jsonl", "append": True, "flush_interval": "200ms"}},
        "service": {"pipelines": {"logs": pipe}},
    }
    if storage:
        cfg["extensions"] = {"file_storage": relay["extensions"]["file_storage"]}
        cfg["service"]["extensions"] = ["file_storage"]
    else:
        flog.pop("storage", None)
    return cfg


WRITER = """
import os, sys, time, datetime
tag, path, count, rate = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
os.makedirs(os.path.dirname(path), exist_ok=True)
f = open(path, "a", buffering=1); n = 0
while n < count and not os.path.exists("/ctl/stop"):
    if os.path.exists("/ctl/rotate"):
        f.close(); os.rename(path, path + ".20260101-000000"); os.remove("/ctl/rotate")
        f = open(path, "a", buffering=1)
    n += 1
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")
    f.write('%s stdout F {"tag":"%s","n":%d}\\n' % (ts, tag, n)); time.sleep(rate)
open("/ctl/last-" + tag, "w").write(str(n))
"""


class Case:
    def __init__(self, image, sc, cfg):
        self.id, self.image, self.sc = "ckpt-" + uuid.uuid4().hex[:8], image, sc
        self.vols = {k: f"{self.id}-{k}" for k in ("pods", "ckpt", "out", "ctl", "conf")}
        CLEAN.append(self)
        for v in self.vols.values():
            sh("docker", "volume", "create", v)
        # The directories as kubelet makes them: root:root 0755.
        self.helper("import os;[os.chmod(p,0o755) for p in ('/pods','/ckpt','/out','/ctl')];"
                    "open('/conf/relay.yaml','w').write(__import__('sys').stdin.read())",
                    stdin=yaml.safe_dump(cfg))

    def helper(self, code, *argv, stdin=None, detach=None):
        mounts = [x for k, v in self.vols.items() for x in ("-v", f"{v}:/{k}")]
        base = ["docker", "run", "--pull=missing"]
        base += ["-d", "--name", f"{self.id}-{detach}"] if detach else ["--rm", "-i"]
        return sh(*base, *mounts, HELPER, "python3", "-c", code, *argv, input=stdin).stdout

    def agent(self):
        sc, args = self.sc, []
        if "runAsUser" in sc:
            args += ["--user", f"{sc['runAsUser']}:{sc.get('runAsGroup', 0)}"]
        args += [x for c in sc.get("capabilities", {}).get("drop", []) for x in ("--cap-drop", c)]
        if sc.get("readOnlyRootFilesystem"):
            args.append("--read-only")
        if sc.get("allowPrivilegeEscalation") is False:
            args += ["--security-opt", "no-new-privileges"]
        sh("docker", "run", "-d", "--name", f"{self.id}-agent", *args,
           "-v", f"{self.vols['pods']}:/var/log/pods:ro", "-v", f"{self.vols['ckpt']}:{CKPT}",
           "-v", f"{self.vols['out']}:/out", "-v", f"{self.vols['conf']}:/conf:ro",
           self.image, "--config", "/conf/relay.yaml")

    def logs(self):
        r = sh("docker", "logs", f"{self.id}-agent", check=False)
        return r.stdout + r.stderr

    def ready(self, times=1):
        for _ in range(60):
            if self.logs().count("Everything is ready") >= times:
                return True
            if sh("docker", "inspect", "-f", "{{.State.Running}}", f"{self.id}-agent").stdout.strip() != "true":
                return False
            time.sleep(0.5)
        return False

    def got(self):
        raw = self.helper("import sys,os\nif os.path.exists('/out/logs.jsonl'): sys.stdout.write(open('/out/logs.jsonl').read())")
        out = {}
        for line in raw.splitlines():
            for rl in json.loads(line).get("resourceLogs", []):
                for sl in rl.get("scopeLogs", []):
                    for rec in sl.get("logRecords", []):
                        body = json.loads(rec["body"]["stringValue"])
                        out.setdefault(body["tag"], []).append(body["n"])
        return out

    def last(self, tag):
        return int(self.helper(f"print(open('/ctl/last-{tag}').read())").strip())

    def run(self, stop="stop", rotate=False, down=6):
        self.agent()
        if not self.ready():
            raise SystemExit("agent did not start:\n" + self.logs()[-1500:])
        self.helper(WRITER, "a", "/pods/ns_app-a_0a/app/0.log", "1000000", "0.01", detach="wa")
        time.sleep(5)
        sh("docker", stop, *(["-t", "30"] if stop == "stop" else []), f"{self.id}-agent")
        # A container that starts and finishes while the agent is down.
        self.helper(WRITER, "b", "/pods/ns_app-b_0b/app/0.log", "200", "0.005")
        if rotate:
            self.helper("open('/ctl/rotate','w')")
        time.sleep(down)
        sh("docker", "start", f"{self.id}-agent")
        check(self.ready(2), f"{self.id}: agent ready again after {stop}")
        time.sleep(5)
        self.helper("open('/ctl/stop','w')")
        sh("docker", "wait", f"{self.id}-wa")
        last, prev = {"a": self.last("a"), "b": 200}, -1
        for _ in range(40):
            got = self.got()
            size = sum(map(len, got.values()))
            if size == prev:
                break
            prev = size
            time.sleep(2)
        res = {}
        for t, n in last.items():
            seen = got.get(t, [])
            res[t] = {"written": n, "missing": n - len(set(seen)), "duplicates": len(seen) - len(set(seen))}
        return res

    def close(self):
        for s in ("agent", "wa"):
            sh("docker", "rm", "-f", f"{self.id}-{s}", check=False)
        for v in self.vols.values():
            sh("docker", "volume", "rm", "-f", v, check=False)


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    relay, pod = render(root)
    ctr = pod["containers"][0]
    image, sc = ctr["image"], ctr["securityContext"]
    mount = [m for m in ctr["volumeMounts"] if m["mountPath"] == CKPT]
    vol = [v for v in pod["volumes"] if mount and v["name"] == mount[0]["name"]]
    check(len(mount) == 1 and not mount[0].get("readOnly") and vol[0].get("hostPath", {}).get("type") == "DirectoryOrCreate",
          f"rendered: one writable hostPath mount at {CKPT}, created if absent")
    check(relay["extensions"]["file_storage"]["directory"] == CKPT and "file_storage" in relay["service"]["extensions"]
          and relay["receivers"]["filelog"].get("storage") == "file_storage", "rendered: filelog checkpoints in file_storage")
    check(sc.get("runAsUser") == 0 and sc.get("capabilities") == {"drop": ["ALL"]} and sc.get("readOnlyRootFilesystem") is True
          and sc.get("allowPrivilegeEscalation") is False, "rendered: root with no capabilities, read-only root, no escalation")
    writable = [m["mountPath"] for m in ctr["volumeMounts"] if not m.get("readOnly") and
                any(v["name"] == m["name"] and "hostPath" in v for v in pod["volumes"])]
    check(writable == [CKPT], f"rendered: the only writable hostPath is {CKPT} ({writable})")
    try:
        res = Case(image, sc, agent_config(relay, True)).run()
        check(all(r["missing"] == 0 for r in res.values()) and res["a"]["written"] > 500, f"stop/start: no line lost {res}")
        ctl = Case(image, sc, agent_config(relay, False)).run()
        check(ctl["a"]["missing"] > 100 and ctl["b"]["missing"] == 200, f"CONTROL without checkpoints loses lines {ctl}")
        nr = Case(image, {}, agent_config(relay, True))
        nr.agent()
        check(not nr.ready() and "permission denied" in nr.logs().lower(), "CONTROL the image's own user cannot use a root-owned checkpoint directory")
        print("INFO killed, not stopped:", Case(image, sc, agent_config(relay, True)).run(stop="kill"))
        print("INFO rotated while down:", Case(image, sc, agent_config(relay, True)).run(rotate=True))
    finally:
        for c in CLEAN:
            c.close()
    print(f"{len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
