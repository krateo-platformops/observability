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
{{- else }}
{{- include "opentelemetry-collector.deploymentConfig" $ctx }}
{{- end }}
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
