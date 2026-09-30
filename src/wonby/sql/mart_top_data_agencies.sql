-- Per agency, which buyers award the most "data" tenders. A shared tender's
-- amount (same total repeated on every supplier row, see CLAUDE.md > Business
-- rules > Amounts) is counted once at the tender level: max(awarded_amt) when
-- shared, sum(awarded_amt) otherwise (distinct amounts on distinct lots).
CREATE OR REPLACE VIEW mart_top_data_agencies AS
WITH data_awards AS (
    SELECT s.*
    FROM stg_awards s
    JOIN tender_classification t ON t.tender_no = s.tender_no
    WHERE t.is_data
),
tender_level AS (
    SELECT
        tender_no,
        agency,
        any_value(amt_shared_across_suppliers) AS is_shared,
        max(awarded_amt) AS max_amt,
        sum(awarded_amt) AS sum_amt,
        max(award_date) AS award_date
    FROM data_awards
    GROUP BY tender_no, agency
),
supplier_counts AS (
    SELECT agency, count(DISTINCT supplier_group) AS n_distinct_suppliers
    FROM data_awards
    WHERE NOT is_no_supplier
    GROUP BY agency
)
SELECT
    tl.agency,
    count(*) AS n_data_tenders,
    sum(CASE WHEN tl.is_shared THEN tl.max_amt ELSE tl.sum_amt END) AS total_amt,
    coalesce(sc.n_distinct_suppliers, 0) AS n_distinct_suppliers,
    max(tl.award_date) AS last_award_date
FROM tender_level tl
LEFT JOIN supplier_counts sc ON sc.agency = tl.agency
GROUP BY tl.agency, sc.n_distinct_suppliers
ORDER BY n_data_tenders DESC, total_amt DESC;
