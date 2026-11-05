{{- define "krateo-slo-rollup.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "krateo-slo-rollup.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" (include "krateo-slo-rollup.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "krateo-slo-rollup.labels" -}}
app.kubernetes.io/name: {{ include "krateo-slo-rollup.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: krateo-observability-stack
app.kubernetes.io/component: slo-rollup
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* ClickHouse connection env, shared by every job. Credentials from the Secret. */}}
{{- define "krateo-slo-rollup.chEnv" -}}
- name: CH_HOST
  value: {{ .Values.clickhouse.host | quote }}
- name: CH_PORT
  value: {{ .Values.clickhouse.port | quote }}
- name: CH_DB
  value: {{ .Values.clickhouse.database | quote }}
- name: CH_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.clickhouse.credentialsSecret.name | quote }}
      key: {{ .Values.clickhouse.credentialsSecret.usernameKey | quote }}
- name: CH_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.clickhouse.credentialsSecret.name | quote }}
      key: {{ .Values.clickhouse.credentialsSecret.passwordKey | quote }}
{{- end -}}
