{{/*
This chart renders the collector's ConfigMap itself (configmap.yaml), and the upstream
opentelemetry-collector subchart mounts it (values.yaml: configMap.create false,
configMap.existingName), as otel-collector-deployment and otel-collector-tailsampling do. Why: to
leave out upstream's default jaeger, prometheus and zipkin receivers, which this agent never runs.
A `null` in values.yaml cannot do it: Helm 4.2 and later drop a parent chart's own nulls before the subchart's
defaults merge in (upstream Helm change 31979), so under Helm 4 they would come back.

Every template here takes the SUBCHART's context: (index .Subcharts "opentelemetry-collector") from
configmap.yaml, and the subchart's own `.` from the podAnnotations checksum (values.yaml), which
upstream renders with tpl. Named templates are shared between a chart and its subcharts, so both
reach these, and both reach upstream's own helpers.
*/}}

{{/*
Upstream 0.170.0's default config entries this agent does not run (upstream values.yaml,
config.receivers.jaeger at lines 236-243, config.receivers.prometheus at lines 251-258 and
config.receivers.zipkin at lines 259-260). No pipeline names them, so the collector never started
them. jaeger's and zipkin's ports are closed (values.yaml ports): leaving them declared would be a
trap, since a pipeline that names one starts a receiver that binds inside the pod but no port
reaches. prometheus is upstream's self-scrape of the collector's own telemetry on :8888; nothing
here consumes it. A prometheus receiver set under config (a scrape config of your own, as in the
Prometheus-to-OTLP procedure in docs/configuration.md) differs from this default and is kept;
prometheus/agentgateway is a different id and is never touched.

Each is left out of the config only while it still holds exactly upstream's default, i.e. nobody
set it: a value set under opentelemetry-collector.config.<path> is kept. If a later upstream version
changes one of these defaults, it no longer matches and appears in the config, and
tests/collector-helm4-render.py fails on it.

A whole receiver is also kept, default or not, while a pipeline names it (an extension: while
service.extensions names it): the collector refuses to start on a pipeline naming an undeclared
receiver. Default pipelines in the list are left out first, so their receivers do not count.
*/}}
{{- define "otel-collector-daemonset.upstreamDefaultsNotRun" -}}
- path: [receivers, jaeger]
  default:
    protocols:
      grpc:
        endpoint: ${env:MY_POD_IP}:14250
      thrift_http:
        endpoint: ${env:MY_POD_IP}:14268
      thrift_compact:
        endpoint: ${env:MY_POD_IP}:6831
- path: [receivers, prometheus]
  default:
    config:
      scrape_configs:
        - job_name: opentelemetry-collector
          scrape_interval: 10s
          static_configs:
            - targets:
                - ${env:MY_POD_IP}:8888
- path: [receivers, zipkin]
  default:
    endpoint: ${env:MY_POD_IP}:9411
{{- end }}

{{/*
The collector config: opentelemetry-collector.config (this chart's values.yaml, upstream's defaults
and any override, merged by Helm as before) without the entries above, then built by upstream's own
config template, so presets (logsCollection, hostMetrics, kubernetesAttributes, kubeletMetrics),
internalTelemetryViaOTLP, rewriteDeprecatedComponentNames and its tpl pass apply exactly as when
upstream rendered the ConfigMap.
*/}}
{{- define "otel-collector-daemonset.collectorConfig" -}}
{{- $ctx := deepCopy . }}
{{- $drops := include "otel-collector-daemonset.upstreamDefaultsNotRun" . | fromYamlArray }}
{{- /* Pipelines first, so a receiver named only by a default pipeline left out here counts as
       unused; then receivers and extensions. */}}
{{- range $pass := list "service" "components" }}
{{- range $drop := $drops }}
  {{- if eq (eq (first $drop.path) "service") (eq $pass "service") }}
  {{- $parent := $ctx.Values.config }}
  {{- range $key := initial $drop.path }}
    {{- if and (kindIs "map" $parent) (hasKey $parent $key) }}
      {{- $parent = index $parent $key }}
    {{- else }}
      {{- $parent = dict }}
    {{- end }}
  {{- end }}
  {{- $leaf := last $drop.path }}
  {{- /* A whole receiver or extension the config uses stays, default or not: a pipeline that names
         prometheus, or service.extensions naming an extension, must find it declared. A protocol
         under a receiver (a longer path) is not a component and is left out regardless. */}}
  {{- $used := false }}
  {{- $service := $ctx.Values.config.service | default dict }}
  {{- if and (eq (len $drop.path) 2) (eq (first $drop.path) "receivers") }}
    {{- range $pipeline := ($service.pipelines | default dict) }}
      {{- if and (kindIs "map" $pipeline) (has $leaf ($pipeline.receivers | default list)) }}
        {{- $used = true }}
      {{- end }}
    {{- end }}
  {{- else if and (eq (len $drop.path) 2) (eq (first $drop.path) "extensions") }}
    {{- $used = has $leaf ($service.extensions | default list) }}
  {{- end }}
  {{- if and (not $used) (kindIs "map" $parent) (hasKey $parent $leaf) (eq (toJson (index $parent $leaf)) (toJson $drop.default)) }}
    {{- $_ := unset $parent $leaf }}
  {{- end }}
  {{- end }}
{{- end }}
{{- end }}
{{- if eq $ctx.Values.mode "daemonset" }}
{{- include "opentelemetry-collector.daemonsetConfig" $ctx }}
{{- else if eq $ctx.Values.mode "deployment" }}
{{- include "otel-collector-daemonset.childConfig" $ctx }}
{{- else }}
{{- include "opentelemetry-collector.deploymentConfig" $ctx }}
{{- end }}
{{- end }}

{{/*
CHILD MODE: opentelemetry-collector.mode: deployment. For a cluster that cannot run the daemonset: a
vcluster child enforces the baseline Pod Security Standard, which refuses hostPath volumes and
hostPorts, and the node's /var/log/pods and kubelet would show it other tenants' data anyway. One
Deployment takes OTLP and writes it to the child's own ClickHouse; nothing reads a node. Selected by
the mode alone, so no new value exists for a schema default to stamp into an existing instance. The
daemonset render does not pass through here.

It is built from upstream's own deploymentConfig, then pruned HERE rather than in values.yaml:
removing a key needs a null, and Helm 4.2 and later drop a parent chart's own nulls (see the top of
this file). Lists replace wholesale, so a pipeline's receivers could not be trimmed from values
either.

1. The host presets must be off, or this fails the render: upstream adds their hostPath volumes in
   whatever the mode, and filelog, hostmetrics and kubeletstats read the node.
2. Receivers filelog, hostmetrics and kubeletstats are removed, and so is every pipeline left with
   no receiver (`logs`, which only filelog fed). `metrics` keeps otlp.
3. ${env:K8S_NODE_NAME} is set only by the daemonset's presets, so a Deployment has none: a
   kubernetes_sd_configs selector `field: spec.nodeName=${env:K8S_NODE_NAME}` is removed from every
   prometheus receiver (it scoped each agent to the pods on its own node; one collector serves the
   whole child), and the render fails if a reference to it is still left. upstream's k8sattributes
   preset adds its own node filter only in daemonset mode, so none is pruned here.
4. A pipeline `logs/otlp-pods` is added. In the daemonset, filelog already collects every pod's
   stdout, which is why logs/gateway keeps only the gateway's access log and a producer's other
   OTLP logs land nowhere (0.1.30). A child has no filelog, so the same rule would drop them, and a
   process that logs over OTLP (core-provider with otel.logs on) would reach no table. This
   pipeline keeps what logs/audit and logs/gateway do not take: filter/otlp-pods drops the audit
   records (event.name audit) and the gateway's access log, so nothing is written twice.
   `transform` stays, for the JWT redaction every log pipeline here carries.
What stays: the otlp receiver and every pipeline it feeds, including logs/audit and its
clickhouse/audit exporter, which is what creates krateo_audit in the child.
*/}}
{{- define "otel-collector-daemonset.childConfig" -}}
{{- $presets := .Values.presets | default dict }}
{{- $on := list }}
{{- range $p := list "logsCollection" "hostMetrics" "kubeletMetrics" }}
  {{- if (index $presets $p | default dict).enabled }}{{ $on = append $on $p }}{{ end }}
{{- end }}
{{- if $on }}
{{- fail (printf "opentelemetry-collector.mode is deployment (a child cluster), and presets %s are enabled. They read the node: hostPath volumes, hostmetrics, kubeletstats, which a child's baseline Pod Security Standard refuses. Set opentelemetry-collector.presets.<name>.enabled to false for logsCollection, hostMetrics and kubeletMetrics (and opentelemetry-collector.clusterRole.rules to [] to drop nodes/proxy)." (join ", " $on)) }}
{{- end }}
{{- $cfg := include "opentelemetry-collector.deploymentConfig" . | fromYaml }}
{{- if hasKey $cfg "Error" }}{{ fail (printf "child mode: the collector config does not parse: %s" $cfg.Error) }}{{ end }}
{{- $hostReceivers := list "filelog" "hostmetrics" "kubeletstats" }}
{{- $receivers := $cfg.receivers | default dict }}
{{- range $r := $hostReceivers }}{{ $_ := unset $receivers $r }}{{ end }}
{{- $pipelines := $cfg.service.pipelines | default dict }}
{{- range $name, $pipeline := $pipelines }}
  {{- $left := list }}
  {{- range $r := ($pipeline.receivers | default list) }}
    {{- if not (has (first (splitList "/" $r)) $hostReceivers) }}{{ $left = append $left $r }}{{ end }}
  {{- end }}
  {{- if $left }}{{ $_ := set $pipeline "receivers" $left }}{{ else }}{{ $_ := unset $pipelines $name }}{{ end }}
{{- end }}
{{- range $id, $receiver := $receivers }}
  {{- if and (hasPrefix "prometheus" $id) (kindIs "map" $receiver) }}
  {{- range $job := (($receiver.config | default dict).scrape_configs | default list) }}
    {{- range $sd := ($job.kubernetes_sd_configs | default list) }}
      {{- range $sel := ($sd.selectors | default list) }}
        {{- if contains "${env:K8S_NODE_NAME}" ($sel.field | default "") }}{{ $_ := unset $sel "field" }}{{ end }}
      {{- end }}
    {{- end }}
  {{- end }}
  {{- end }}
{{- end }}
{{- $processors := $cfg.processors | default dict }}
{{- $_ := set $processors "filter/otlp-pods" (dict "error_mode" "ignore" "logs" (dict "log_record" (list
      "attributes[\"event.name\"] == \"audit\""
      "(instrumentation_scope.name == \"request\" or instrumentation_scope.name == \"agentgateway.access\") and attributes[\"gateway\"] != nil"))) }}
{{- $_ := set $cfg "processors" $processors }}
{{- $_ := set $pipelines "logs/otlp-pods" (dict "receivers" (list "otlp") "processors" (list "memory_limiter" "filter/otlp-pods" "k8sattributes" "transform" "batch") "exporters" (list "clickhouse")) }}
{{- $out := toYaml $cfg }}
{{- if regexMatch "\\$\\{env:K8S_NODE_NAME\\}" $out }}
{{- fail "child mode: the collector config still references ${env:K8S_NODE_NAME}, which only the daemonset sets. Remove the reference (a node-scoped selector or endpoint) from the values." }}
{{- end }}
{{- $out }}
{{- end }}

{{/*
The ConfigMap's name: the one upstream gives its own ConfigMap for the mode (configmap-agent.yaml,
configmap.yaml, configmap-statefulset.yaml), so the object keeps its name.
*/}}
{{- define "otel-collector-daemonset.configMapName" -}}
{{- include "opentelemetry-collector.fullname" . }}{{ get (dict "daemonset" "-agent" "statefulset" "-statefulset") .Values.mode }}
{{- end }}

{{/*
The ConfigMap, written as upstream's configmap-agent.yaml writes it.
*/}}
{{- define "otel-collector-daemonset.configMap" -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "otel-collector-daemonset.configMapName" . }}
  namespace: {{ template "opentelemetry-collector.namespace" . }}
  labels:
    {{- include "opentelemetry-collector.labels" . | nindent 4 }}
data:
  relay: |
    {{- include "otel-collector-daemonset.collectorConfig" . | nindent 4 }}
{{ end -}}
