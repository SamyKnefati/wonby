-- Prospecting shortlist: supplier_groups with at least target_min_data_tenders
-- distinct "data" tenders in the last target_window_months (anchored on
-- max(award_date), never current_date). Columns are chosen as pitch-angle
-- facts: recent activity, regularity (distinct quarters with a win),
-- who they work for lately, what the work looked like, and how much money
-- (excluding shared/nominal amounts, which carry no real signal per tender).
CREATE OR REPLACE VIEW mart_priority_targets AS
WITH anchor AS (
    SELECT max(award_date) AS anchor_date FROM stg_awards
),
data_awards AS (
    SELECT s.*
    FROM stg_awards s
    JOIN tender_classification t ON t.tender_no = s.tender_no
    WHERE t.is_data AND NOT s.is_no_supplier
),
recent AS (
    SELECT da.*
    FROM data_awards da, anchor, radar_params p
    WHERE da.award_date >= anchor.anchor_date - (p.target_window_months || ' months')::INTERVAL
),
all_time_stats AS (
    SELECT
        supplier_group,
        mode(supplier_name_norm) AS display_name,
        count(DISTINCT tender_no) AS n_all_time_data_tenders,
        max(award_date) AS last_award_date
    FROM data_awards
    GROUP BY supplier_group
),
recent_stats AS (
    SELECT
        supplier_group,
        count(DISTINCT tender_no) AS n_recent_data_tenders,
        count(DISTINCT date_trunc('quarter', award_date)) AS n_recent_quarters,
        string_agg(DISTINCT agency, '; ' ORDER BY agency) AS agencies_recent,
        sum(awarded_amt) FILTER (
            WHERE NOT amt_shared_across_suppliers AND NOT is_nominal_amt
        ) AS recent_amt_excl_shared
    FROM recent
    GROUP BY supplier_group
),
recent_tender_desc AS (
    -- one row per (supplier_group, tender_no): truncate here so the "pitch
    -- angle" description is short by construction, in the mart itself.
    SELECT DISTINCT
        supplier_group,
        tender_no,
        left(tender_description, 140)
            || CASE WHEN length(tender_description) > 140 THEN '…' ELSE '' END AS short_description,
        award_date
    FROM recent
),
recent_desc_agg AS (
    SELECT
        supplier_group,
        list(short_description ORDER BY award_date DESC)[1:2] AS recent_descriptions
    FROM recent_tender_desc
    GROUP BY supplier_group
)
SELECT
    a.supplier_group,
    a.display_name,
    r.n_recent_data_tenders,
    a.n_all_time_data_tenders,
    r.n_recent_quarters,
    a.last_award_date,
    r.agencies_recent,
    d.recent_descriptions,
    r.recent_amt_excl_shared
FROM recent_stats r
JOIN all_time_stats a ON a.supplier_group = r.supplier_group
LEFT JOIN recent_desc_agg d ON d.supplier_group = r.supplier_group
CROSS JOIN radar_params p
WHERE r.n_recent_data_tenders >= p.target_min_data_tenders
ORDER BY r.n_recent_data_tenders DESC, a.last_award_date DESC;
