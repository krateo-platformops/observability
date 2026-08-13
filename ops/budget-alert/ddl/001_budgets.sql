-- Budget definitions: a threshold on rated showback data, per scope.
-- Empty string = wildcard (tenant '' = whole org, service '' = all services,
-- tag_key '' = not tag-scoped, tag_value '' = any value of tag_key).
-- ReplacingMergeTree on updated_at so a budget update supersedes older rows.
--
-- The `{{database}}` token is replaced at apply time (same convention as the
-- showback engine DDL); it must be the database hosting the showback tables.
CREATE TABLE IF NOT EXISTS {{database}}.budgets
(
    budget_id  String,
    org        LowCardinality(String),
    tenant     LowCardinality(String) DEFAULT '',
    service    LowCardinality(String) DEFAULT '',
    tag_key    LowCardinality(String) DEFAULT '',
    tag_value  String                 DEFAULT '',
    period     LowCardinality(String) DEFAULT 'monthly',  -- 'monthly' | 'daily'
    amount     Float64,
    currency   LowCardinality(String),
    warn_ratio Float64 DEFAULT 0.8,   -- early-warning threshold as fraction of amount
    enabled    UInt8   DEFAULT 1,
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (org, tenant, service, tag_key, tag_value, budget_id)
