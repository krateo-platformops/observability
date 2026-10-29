{{- define "krateo-audit-worm.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "krateo-audit-worm.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" (include "krateo-audit-worm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "krateo-audit-worm.labels" -}}
app.kubernetes.io/name: {{ include "krateo-audit-worm.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: krateo-observability-stack
app.kubernetes.io/component: audit-worm
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* ClickHouse connection env, shared by every job. Credentials from the Secret. */}}
{{- define "krateo-audit-worm.chEnv" -}}
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

{{/*
The row hash: SHA256 over every immutable field of an audit row (maps mapSort'd, so it
is key-order independent). ONE definition, used by the mirroring MV (the ingest-time
witness stored in row_hash) and by the verifier (which recomputes it from the row's
content). Changing it changes every hash: existing deployments keep the MV they have.
*/}}
{{- define "krateo-audit-worm.rowHash" -}}
lower(hex(SHA256(concat(toString(Timestamp), '|', TraceId, '|', SpanId, '|', toString(SeverityText), '|', toString(ServiceName), '|', Body, '|', toString(mapSort(ResourceAttributes)), '|', toString(mapSort(LogAttributes))))))
{{- end -}}
