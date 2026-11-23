{{- define "otel-collector-daemonset.auditSelector" -}}
app.kubernetes.io/name: otel-collector-audit
app.kubernetes.io/instance: {{ .Release.Name }}
component: audit-collector
{{- end }}
{{- define "otel-collector-daemonset.auditLabels" -}}
{{ include "otel-collector-daemonset.auditSelector" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: krateo
{{- end }}
