-- supplier_group x agency, over "data" tenders — which buyers each prospecting
-- target has already worked for. A supplier ranking, so excludes is_no_supplier.
CREATE OR REPLACE VIEW mart_supplier_agencies AS
WITH data_awards AS (
    SELECT s.*
    FROM stg_awards s
    JOIN tender_classification t ON t.tender_no = s.tender_no
    WHERE t.is_data AND NOT s.is_no_supplier
)
SELECT
    supplier_group,
    agency,
    count(DISTINCT tender_no) AS n_data_tenders,
    sum(awarded_amt) AS total_amt,
    max(award_date) AS last_award_date
FROM data_awards
GROUP BY supplier_group, agency
ORDER BY supplier_group, n_data_tenders DESC, last_award_date DESC;
