{{/*
Image reference honoring an optional global.imageRegistry override (host-only; repository paths
preserved). Canonical helper — identical across all Krateo charts.
*/}}
{{- define "krateo.image" -}}
{{- $g := .global | default dict -}}
{{- $registry := $g.imageRegistry | default .img.registry -}}
{{- $tag := .img.tag | default .defaultTag -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .img.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .img.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "krateo-sse-proxy.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "krateo-sse-proxy.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "krateo-sse-proxy.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "krateo-sse-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: sse-proxy
{{- end -}}

{{- define "krateo-sse-proxy.selectorLabels" -}}
app: {{ include "krateo-sse-proxy.fullname" . }}
app.kubernetes.io/name: {{ include "krateo-sse-proxy.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "krateo-sse-proxy.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "krateo-sse-proxy.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Does ANY OTel signal resolve to ON?

This mirrors telemetry.go's envEnabledOr exactly: OTEL_TRACING_ENABLED and
OTEL_METRICS_ENABLED each DEFAULT to the master OTEL_ENABLED and OVERRIDE it when
set. It exists so OTEL_EXPORTER_OTLP_ENDPOINT is emitted whenever a signal is on,
rather than only when the master is. Gating the endpoint on `otel.enabled` alone
would make `enabled: false` + `tracing: true` a silent black hole: the binary
turns tracing on, no endpoint is set, and the exporter falls back to its own
default of localhost:4318 — which inside a pod is the pod itself, so every span
is exported to nothing and the only symptom is a connection-refused line.
*/}}
{{- define "krateo-sse-proxy.otelAnySignal" -}}
{{- $o := .Values.otel -}}
{{- $tracing := $o.enabled -}}
{{- if not (kindIs "invalid" $o.tracing) -}}{{- $tracing = $o.tracing -}}{{- end -}}
{{- $metrics := $o.enabled -}}
{{- if not (kindIs "invalid" $o.metrics) -}}{{- $metrics = $o.metrics -}}{{- end -}}
{{- if or $tracing $metrics -}}true{{- end -}}
{{- end -}}
