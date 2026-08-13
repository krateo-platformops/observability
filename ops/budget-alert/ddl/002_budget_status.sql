-- Current-period spend vs. budget, per budget definition.
-- status: 'ok' | 'warning' (>= warn_ratio) | 'breached' (>= amount).
--
-- Non-tag-scoped budgets aggregate showback_daily; tag-scoped budgets
-- aggregate showback_daily_by_tag (the rollups maintained by the showback
-- engine). The budgets table is tiny, so the CROSS JOIN + filter over the
-- daily rollups stays cheap.
CREATE OR REPLACE VIEW {{database}}.budget_status AS
WITH spend AS
(
    -- budgets without a tag scope
    SELECT
        b.budget_id AS budget_id,
        sum(d.cost) AS spend
    FROM {{database}}.budgets AS b FINAL
    CROSS JOIN {{database}}.showback_daily AS d
    WHERE b.enabled = 1
      AND b.tag_key = ''
      AND d.org = b.org
      AND (b.tenant  = '' OR d.tenant  = b.tenant)
      AND (b.service = '' OR d.service = b.service)
      AND d.currency = b.currency
      AND d.day >= if(b.period = 'daily', today(), toStartOfMonth(today()))
    GROUP BY b.budget_id

    UNION ALL

    -- tag-scoped budgets
    SELECT
        b.budget_id,
        sum(d.cost)
    FROM {{database}}.budgets AS b FINAL
    CROSS JOIN {{database}}.showback_daily_by_tag AS d
    WHERE b.enabled = 1
      AND b.tag_key != ''
      AND d.org = b.org
      AND d.tag_key = b.tag_key
      AND (b.tag_value = '' OR d.tag_value = b.tag_value)
      AND (b.tenant    = '' OR d.tenant   = b.tenant)
      AND (b.service   = '' OR d.service  = b.service)
      AND d.currency = b.currency
      AND d.day >= if(b.period = 'daily', today(), toStartOfMonth(today()))
    GROUP BY b.budget_id
)
SELECT
    b.budget_id,
    b.org,
    b.tenant,
    b.service,
    b.tag_key,
    b.tag_value,
    b.period,
    b.amount,
    b.currency,
    b.warn_ratio,
    coalesce(s.spend, 0)                                    AS spend,
    if(b.amount > 0, coalesce(s.spend, 0) / b.amount, 0)    AS spend_ratio,
    multiIf(
        coalesce(s.spend, 0) >= b.amount,                'breached',
        coalesce(s.spend, 0) >= b.amount * b.warn_ratio, 'warning',
                                                         'ok')  AS status,
    now64(3, 'UTC')                                         AS evaluated_at
FROM {{database}}.budgets AS b FINAL
LEFT JOIN spend AS s ON s.budget_id = b.budget_id
WHERE b.enabled = 1
