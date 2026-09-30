-- Per supplier_group, ranking of who wins "data" tenders.
-- Leads with the number of distinct tenders (not rows: a tender can have several
-- supplier rows) and shows both the raw total and the total excluding amounts
-- flagged as shared-across-suppliers or nominal ($1 panel seats) — see
-- CLAUDE.md > Business rules > Amounts. Excludes `is_no_supplier` rows.
CREATE OR REPLACE VIEW mart_top_data_suppliers AS
WITH data_awards AS (
    SELECT s.*
    FROM stg_awards s
    JOIN tender_classification t ON t.tender_no = s.tender_no
    WHERE t.is_data AND NOT s.is_no_supplier
)
SELECT
    supplier_group,
    mode(supplier_name_norm) AS display_name,
    count(DISTINCT tender_no) AS n_data_tenders,
    count(*) AS n_awards,
    sum(awarded_amt) AS total_amt,
    sum(awarded_amt) FILTER (
        WHERE NOT amt_shared_across_suppliers AND NOT is_nominal_amt
    ) AS total_amt_excl_shared,
    min(award_date) AS first_award_date,
    max(award_date) AS last_award_date,
    count(DISTINCT agency) AS n_agencies,
    string_agg(DISTINCT agency, '; ' ORDER BY agency) AS agencies
FROM data_awards
GROUP BY supplier_group
ORDER BY n_data_tenders DESC, total_amt DESC;
