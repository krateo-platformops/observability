{{/*
This chart renders the collector's ConfigMap itself (configmap.yaml), and the upstream
opentelemetry-collector subchart mounts it (values.yaml: configMap.create false,
configMap.existingName). Why: the config used to drop upstream's default receivers, otlp's http
protocol and the logs and metrics pipelines with `null` in values.yaml, and Helm 4.2 and later drop
a parent chart's own nulls before the subchart's defaults merge in (upstream Helm change 31979), so
under Helm 4 they came back.

Every template here takes the SUBCHART's context: (index .Subcharts "opentelemetry-collector") from
configmap.yaml, and the subchart's own `.` from the podAnnotations checksum (values.yaml), which
upstream renders with tpl. Named templates are shared between a chart and its subcharts, so both
reach these, and both reach upstream's own helpers.
*/}}

{{/*
Upstream 0.170.0's default config entries this collector does not run (upstream values.yaml,
config.receivers.jaeger/otlp/prometheus/zipkin at lines 236-260, config.service.pipelines.logs and
.metrics at lines 280 and 288). Each is left out of the config only while it still holds exactly
upstream's default, i.e. nobody set it: a value set under opentelemetry-collector.config.<path> is
kept, as Helm 3 kept a user's value over this chart's old null. If a later upstream version changes
one of these defaults, it no longer matches and appears in the config, and
tests/collector-helm4-render.py fails on it.
*/}}
{{- define "otel-collector-tailsampling.upstreamDefaultsNotRun" -}}
- path: [receivers, jaeger]
  default:
    protocols:
      grpc:
        endpoint: ${env:MY_POD_IP}:14250
      thrift_http:
        endpoint: ${env:MY_POD_IP}:14268
      thrift_compact:
        endpoint: ${env:MY_POD_IP}:6831
- path: [receivers, otlp, protocols, http]
  default:
    endpoint: ${env:MY_POD_IP}:4318
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
- path: [service, pipelines, logs]
  default:
    exporters: [debug]
    processors: [memory_limiter, batch]
    receivers: [otlp]
- path: [service, pipelines, metrics]
  default:
    exporters: [debug]
    processors: [memory_limiter, batch]
    receivers: [otlp, prometheus]
{{- end }}

{{/*
The collector config: opentelemetry-collector.config (this chart's values.yaml, upstream's defaults
and any override, merged by Helm as before) without the entries above, then built by upstream's own
config template, so presets, internalTelemetryViaOTLP, rewriteDeprecatedComponentNames and its tpl
pass apply exactly as when upstream rendered the ConfigMap.
*/}}
{{- define "otel-collector-tailsampling.collectorConfig" -}}
{{- $ctx := deepCopy . }}
{{- range $drop := include "otel-collector-tailsampling.upstreamDefaultsNotRun" . | fromYamlArray }}
  {{- $parent := $ctx.Values.config }}
  {{- range $key := initial $drop.path }}
    {{- if and (kindIs "map" $parent) (hasKey $parent $key) }}
      {{- $parent = index $parent $key }}
    {{- else }}
      {{- $parent = dict }}
    {{- end }}
  {{- end }}
  {{- $leaf := last $drop.path }}
  {{- if and (kindIs "map" $parent) (hasKey $parent $leaf) (eq (toJson (index $parent $leaf)) (toJson $drop.default)) }}
    {{- $_ := unset $parent $leaf }}
  {{- end }}
{{- end }}
{{- if eq $ctx.Values.mode "daemonset" }}
{{- include "opentelemetry-collector.daemonsetConfig" $ctx }}
{{- else }}
{{- include "opentelemetry-collector.deploymentConfig" $ctx }}
{{- end }}
{{- end }}

{{/*
The ConfigMap's name: the one upstream gives its own ConfigMap for the mode (configmap.yaml,
configmap-agent.yaml, configmap-statefulset.yaml), so the object keeps its name.
*/}}
{{- define "otel-collector-tailsampling.configMapName" -}}
{{- include "opentelemetry-collector.fullname" . }}{{ get (dict "daemonset" "-agent" "statefulset" "-statefulset") .Values.mode }}
{{- end }}

{{/*
The ConfigMap, written as upstream's configmap.yaml writes it, so for the same config its text, and
the checksum/config annotation that hashes it, is the same as before.
*/}}
{{- define "otel-collector-tailsampling.configMap" -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "otel-collector-tailsampling.configMapName" . }}
  namespace: {{ template "opentelemetry-collector.namespace" . }}
  labels:
    {{- include "opentelemetry-collector.labels" . | nindent 4 }}
data:
  relay: |
    {{- include "otel-collector-tailsampling.collectorConfig" . | nindent 4 }}
{{ end -}}
