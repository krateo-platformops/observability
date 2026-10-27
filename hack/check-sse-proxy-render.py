#!/usr/bin/env python3
"""Render-gate for krateo-sse-proxy's feature knobs.

The repo's canonical lint runs `helm template smoke <dir>` with DEFAULT values. That
proves the chart renders; it says nothing about a knob, because every knob added here is
default-off and so every conditional branch is unrendered in the only case lint exercises.
A gate that cannot see the field you changed is not a gate.

This renders the REAL chart under explicit value sets and asserts, per case, the WHOLE env
key set rather than only the key under test — a test that asserts "my new var is present"
passes just as happily when the four vars beside it have vanished.

Run: python3 hack/check-sse-proxy-render.py [repo-root]
Exit 0 = every case as specified, 1 = at least one case wrong.
"""

import json
import pathlib
import subprocess
import sys
import tempfile

CHART = "charts/krateo-sse-proxy"
NS = "krateo-system"

# The five env vars the chart has always set. Every case below states its full expected
# key set, so a regression that DROPS one of these fails too.
BASE = {
    "CLICKHOUSE_URL",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "LISTEN_ADDR",
    "URL_AUTHN",
}
DAEMONSET_OTLP = f"http://otel-collector-daemonset-opentelemetry-collector.{NS}.svc:4318"


def render(root: pathlib.Path, values: dict, namespace: str = NS) -> list[dict]:
    """helm template the chart with `values`, returning the rendered objects."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        json.dump(values, fh)  # JSON is valid YAML; helm reads it as values
        vf = fh.name
    out = subprocess.run(
        ["helm", "template", "sse", str(root / CHART), "-n", namespace, "-f", vf],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip())
    import yaml
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def env_of(objs: list[dict]) -> dict[str, object]:
    """The Deployment container's env, as name -> value (or the valueFrom dict)."""
    for o in objs:
        if o.get("kind") == "Deployment":
            env = o["spec"]["template"]["spec"]["containers"][0].get("env", [])
            return {e["name"]: e.get("value", e.get("valueFrom")) for e in env}
    raise AssertionError("no Deployment rendered")


def kinds(objs: list[dict]) -> list[str]:
    return sorted(f"{o['kind']}/{o['metadata']['name']}" for o in objs)


def diff(got: set, want: set) -> str:
    """Both directions. A one-sided report reads as 'nothing wrong' on a MISSING key."""
    missing, extra = sorted(want - got), sorted(got - want)
    if not missing and not extra:
        return "exactly as specified"
    return f"missing={missing or 'none'} unexpected={extra or 'none'}"


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures: list[str] = []

    def check(case: str, cond: bool, detail: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {case}: {detail}")
        if not cond:
            failures.append(f"{case}: {detail}")

    # ---- A. defaults: both features off, nothing extra rendered ----
    objs = render(root, {})
    env = env_of(objs)
    check("A defaults", set(env) == BASE,
          f"env keys == the 5 base vars ({diff(set(env), BASE)})")
    check("A defaults", not any("event-scoping" in k for k in kinds(objs)),
          "no scoping ClusterRole/ClusterRoleBinding rendered")

    # ---- B. master gate on ----
    env = env_of(render(root, {"otel": {"enabled": True}}))
    check("B otel.enabled", set(env) == BASE | {"OTEL_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT"},
          f"adds exactly OTEL_ENABLED + endpoint ({diff(set(env), BASE | {'OTEL_ENABLED', 'OTEL_EXPORTER_OTLP_ENDPOINT'})})")
    check("B otel.enabled", env.get("OTEL_ENABLED") == "true", "OTEL_ENABLED == 'true'")
    check("B otel.enabled", env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == DAEMONSET_OTLP,
          f"endpoint defaults to the daemonset collector in-namespace ({env.get('OTEL_EXPORTER_OTLP_ENDPOINT')})")

    # ---- C. THE BLACK-HOLE GUARD: a per-signal flag on while the master is off ----
    # telemetry.go turns tracing ON here (the per-signal flag overrides the master), so the
    # endpoint MUST still be emitted. If it is not, the exporter falls back to
    # localhost:4318 and every span is lost silently.
    env = env_of(render(root, {"otel": {"enabled": False, "tracing": True}}))
    check("C tracing-without-master",
          set(env) == BASE | {"OTEL_TRACING_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT"},
          f"adds exactly OTEL_TRACING_ENABLED + endpoint ({diff(set(env), BASE | {'OTEL_TRACING_ENABLED', 'OTEL_EXPORTER_OTLP_ENDPOINT'})})")
    check("C tracing-without-master", env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == DAEMONSET_OTLP,
          "endpoint IS emitted even though the master gate is false")
    check("C tracing-without-master", "OTEL_ENABLED" not in env,
          "OTEL_ENABLED is NOT emitted (the master stays off; the binary's default governs)")

    # ---- D. per-signal flag diverging downward from an on master ----
    env = env_of(render(root, {"otel": {"enabled": True, "metrics": False}}))
    check("D metrics-off", set(env) == BASE | {"OTEL_ENABLED", "OTEL_METRICS_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT"},
          f"adds OTEL_ENABLED + OTEL_METRICS_ENABLED + endpoint ({diff(set(env), BASE | {'OTEL_ENABLED', 'OTEL_METRICS_ENABLED', 'OTEL_EXPORTER_OTLP_ENDPOINT'})})")
    check("D metrics-off", env.get("OTEL_METRICS_ENABLED") == "false",
          "OTEL_METRICS_ENABLED == 'false' (explicit, so it overrides the master in-binary)")

    # ---- E. an explicit endpoint wins over the derived default ----
    env = env_of(render(root, {"otel": {"enabled": True, "endpoint": "http://gw.obs.svc:4318"}}))
    check("E endpoint-override", env.get("OTEL_EXPORTER_OTLP_ENDPOINT") == "http://gw.obs.svc:4318",
          "explicit endpoint is used verbatim")

    # ---- F. scoping flag carries its own grants ----
    objs = render(root, {"rbacScoping": {"enabled": True}})
    env = env_of(objs)
    check("F scoping", set(env) == BASE | {"RBAC_SCOPING_ENABLED"},
          f"adds exactly RBAC_SCOPING_ENABLED ({diff(set(env), BASE | {'RBAC_SCOPING_ENABLED'})})")
    cr = [o for o in objs if o["kind"] == "ClusterRole"]
    crb = [o for o in objs if o["kind"] == "ClusterRoleBinding"]
    check("F scoping", len(cr) == 1 and len(crb) == 1,
          f"exactly one ClusterRole and one ClusterRoleBinding (got {len(cr)}/{len(crb)})")
    if cr and crb:
        rules = {(tuple(r["apiGroups"]), tuple(r["resources"]), tuple(r["verbs"])) for r in cr[0]["rules"]}
        want = {
            (("authorization.k8s.io",), ("subjectaccessreviews",), ("create",)),
            (("",), ("namespaces",), ("list",)),
        }
        check("F scoping", rules == want,
              f"grants are exactly SAR/create + namespaces/list (got {sorted(rules)})")
        # The engine already owns a ClusterRole named exactly `krateo-sse-proxy`.
        check("F scoping", cr[0]["metadata"]["name"] == "sse-krateo-sse-proxy-event-scoping",
              f"ClusterRole name is suffixed, not the bare fullname ({cr[0]['metadata']['name']})")
        subj = crb[0]["subjects"]
        check("F scoping",
              len(subj) == 1 and subj[0]["kind"] == "ServiceAccount"
              and subj[0]["name"] == "sse-krateo-sse-proxy" and subj[0]["namespace"] == NS,
              f"binding subject is this chart's SA in the release namespace ({subj})")
        check("F scoping", crb[0]["roleRef"]["name"] == cr[0]["metadata"]["name"],
              "roleRef points at the ClusterRole this chart created")

    # ---- G. every scoping override reaches the pod ----
    env = env_of(render(root, {"rbacScoping": {
        "enabled": True, "verb": "watch", "resource": "pods",
        "apiGroup": "apps", "cacheTTL": "5m"}}))
    check("G scoping-overrides",
          set(env) == BASE | {"RBAC_SCOPING_ENABLED", "RBAC_SCOPING_VERB",
                              "RBAC_SCOPING_RESOURCE", "RBAC_SCOPING_APIGROUP",
                              "RBAC_SCOPING_CACHE_TTL"},
          f"all five scoping vars present ({diff(set(env), BASE | {'RBAC_SCOPING_ENABLED', 'RBAC_SCOPING_VERB', 'RBAC_SCOPING_RESOURCE', 'RBAC_SCOPING_APIGROUP', 'RBAC_SCOPING_CACHE_TTL'})})")
    check("G scoping-overrides",
          (env.get("RBAC_SCOPING_VERB"), env.get("RBAC_SCOPING_RESOURCE"),
           env.get("RBAC_SCOPING_APIGROUP"), env.get("RBAC_SCOPING_CACHE_TTL"))
          == ("watch", "pods", "apps", "5m"),
          "each override is passed through verbatim")

    # ---- H. a non-default namespace follows through ----
    objs = render(root, {"otel": {"enabled": True}, "rbacScoping": {"enabled": True}}, namespace="krateo")
    env = env_of(objs)
    check("H other-namespace",
          env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
          == "http://otel-collector-daemonset-opentelemetry-collector.krateo.svc:4318",
          "the derived endpoint tracks .Release.Namespace")
    crb = [o for o in objs if o["kind"] == "ClusterRoleBinding"][0]
    check("H other-namespace", crb["subjects"][0]["namespace"] == "krateo",
          "the binding subject tracks .Release.Namespace")

    # ---- I. the schema actually rejects what it should ----
    for bad, why in [
        ({"otel": {"protocol": "grpc"}}, "otel.protocol (removed: the binary ignores it)"),
        ({"rbacScoping": {"enabledd": True}}, "a typo'd key under rbacScoping"),
        ({"otel": {"enabled": "yes"}}, "a non-boolean otel.enabled"),
    ]:
        try:
            render(root, bad)
            check("I schema", False, f"rejects {why}")
        except RuntimeError:
            check("I schema", True, f"rejects {why}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} assertion(s)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All cases as specified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
