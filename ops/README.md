# ops/ — ClickStack deployment config & ops artifacts

Deployment-adjacent configuration and operational scripts for the Krateo ClickStack, folded
in from the former `krateo-clickstack` code repo when its two images were split into
[`krateo-sse-proxy`](https://github.com/krateo-platformops/sse-proxy) and
[`krateo-otel-collector`](https://github.com/krateo-platformops/otel-collector).

- `clickhouse-config/` — ClickHouse configmaps/secrets/HTTP handlers
- `otel-collectors/` — reference OTel collector manifests (the live charts are under `charts/`)
- `ha/` — network policies, pod-disruption-budgets, canary heartbeat
- `pod-restart-alert/` — alert bootstrap scripts
- `clickstack/` — clickstack values reference
