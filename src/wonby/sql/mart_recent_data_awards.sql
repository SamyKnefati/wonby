-- Row-level feed of "data" award rows within recent_window_months of the data
-- anchor (max(award_date) in stg_awards, never current_date), newest first.
-- Not a supplier ranking, so is_no_supplier rows are kept (amount is 0) and
-- surfaced via the flag rather than dropped, same for shared/nominal amounts.
CREATE OR REPLACE VIEW mart_recent_data_awards AS
WITH anchor AS (
    SELECT max(award_date) AS anchor_date FROM stg_awards
),
data_awards AS (
    SELECT s.*
    FROM stg_awards s
    JOIN tender_classification t ON t.tender_no = s.tender_no
    WHERE t.is_data
)
SELECT
    da.tender_no,
    da.tender_description,
    da.agency,
    da.supplier_group,
    da.supplier_name_norm,
    da.award_date,
    da.awarded_amt,
    da.is_no_supplier,
    da.amt_shared_across_suppliers,
    da.is_nominal_amt
FROM data_awards da, anchor, radar_params p
WHERE da.award_date >= anchor.anchor_date - (p.recent_window_months || ' months')::INTERVAL
ORDER BY da.award_date DESC, da.tender_no, da.supplier_group;
