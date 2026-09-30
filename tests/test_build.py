import json
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pytest

from wonby import build, ingest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def loaded_con(tmp_path):
    con = duckdb.connect(str(tmp_path / "test.duckdb"))
    records = json.loads((FIXTURES / "api_records.json").read_text())
    ingest.run(con, full=True, fetch_all_records=lambda: records, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    yield con
    con.close()


@pytest.fixture
def classification_config_path(tmp_path):
    p = tmp_path / "classification.yaml"
    p.write_text(
        """
include:
  - pattern: '\\bdata\\s+analytics?\\b'
    why: test
  - pattern: '\\bbusiness\\s+intelligence\\b'
    why: test
  - pattern: '\\bdashboards?\\b'
    why: test
exclude:
  - pattern: '\\bdata\\s*cent(re|er)s?\\b'
    why: test
  - pattern: '\\bsupervisory\\s+control\\s+and\\s+data\\s+acquisition\\b'
    why: test
sample_signals:
  - '\\bdata\\b'
"""
    )
    return p


@pytest.fixture
def aliases_path(tmp_path):
    p = tmp_path / "aliases.yaml"
    p.write_text("aliases: {}\n")
    return p


@pytest.fixture
def radar_params_path(tmp_path):
    p = tmp_path / "radar.yaml"
    p.write_text("recent_window_months: 6\ntarget_min_data_tenders: 2\ntop_n: 25\n")
    return p


@pytest.fixture
def empty_sql_dir(tmp_path):
    d = tmp_path / "sql"
    d.mkdir()
    return d


def do_build(con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    return build.run(
        con,
        classification_config_path=classification_config_path,
        aliases_path=aliases_path,
        radar_params_path=radar_params_path,
        sql_dir=empty_sql_dir,
    )


def test_build_produces_expected_row_count(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    n = loaded_con.execute("SELECT count(*) FROM stg_awards").fetchone()[0]
    n_raw = loaded_con.execute("SELECT count(*) FROM raw_awards WHERE removed_at IS NULL").fetchone()[0]
    assert n == n_raw
    assert n > 15


def test_build_empty_sql_dir_does_not_error(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    # sql/ has no files yet — must not raise.
    summary = do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    assert summary["sql_files"] == []


def test_amt_shared_across_suppliers_on_repeated_tender_total(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    rows = loaded_con.execute(
        "SELECT n_suppliers_in_tender, amt_shared_across_suppliers FROM stg_awards WHERE tender_no = 'CCY000ETT21000005'"
    ).fetchall()
    assert len(rows) == 4
    assert all(r[0] == 4 for r in rows)
    assert all(r[1] is True for r in rows)


def test_is_nominal_amt_on_dollar_panel(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    rows = loaded_con.execute(
        "SELECT is_nominal_amt, amt_shared_across_suppliers FROM stg_awards WHERE tender_no = 'IMD000ETT23000009'"
    ).fetchall()
    assert len(rows) == 4
    assert all(r[0] is True for r in rows)  # all $1
    assert all(r[1] is True for r in rows)  # single distinct amount (1) across >1 suppliers


def test_is_no_supplier_flag(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    row = loaded_con.execute(
        "SELECT is_no_supplier, supplier_name_raw, awarded_amt FROM stg_awards WHERE tender_no = 'AGC000ETT23000006'"
    ).fetchone()
    assert row[0] is True
    assert row[1] == "Unknown"
    assert row[2] == 0


def test_supplier_group_merges_analysys_mason_pair(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    groups = loaded_con.execute(
        "SELECT DISTINCT supplier_group FROM stg_awards "
        "WHERE tender_no = 'IMD000ETT23000009' AND supplier_name_raw LIKE 'ANALYSYS MASON%'"
    ).fetchall()
    assert groups == [("ANALYSYS MASON",)]


def test_fiscal_year_and_award_date_typed(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    row = loaded_con.execute(
        "SELECT award_date, fiscal_year FROM stg_awards WHERE tender_no = 'ACR000ETT21000001'"
    ).fetchone()
    assert row[0] == date(2021, 9, 6)  # day-first 6/9/2021
    assert row[1] == 2021


def test_decimal_amount_precision(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    row = loaded_con.execute(
        "SELECT awarded_amt FROM stg_awards WHERE tender_no = 'ACR000ETT23000002'"
    ).fetchone()
    assert str(row[0]) == "310676.63"


def test_tender_classification_positive_and_negative(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)

    positive = loaded_con.execute(
        "SELECT is_data, matched_includes FROM tender_classification WHERE tender_no = 'HDB000ETT21000129'"
    ).fetchone()
    assert positive[0] is True
    assert len(positive[1]) > 0

    # "Comprehensive Maintenance Services of AGC Data Centers" — masked out by the
    # data-centre exclude, nothing left for includes to match.
    negative = loaded_con.execute(
        "SELECT is_data FROM tender_classification WHERE tender_no = 'AGC000ETT24000003'"
    ).fetchone()
    assert negative[0] is False

    scada = loaded_con.execute(
        "SELECT is_data FROM tender_classification WHERE tender_no = 'PUB000ETT21000065'"
    ).fetchone()
    assert scada[0] is False


def test_tender_classification_one_row_per_tender(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    total_tenders = loaded_con.execute("SELECT count(DISTINCT tender_no) FROM stg_awards").fetchone()[0]
    classification_rows = loaded_con.execute("SELECT count(*) FROM tender_classification").fetchone()[0]
    assert total_tenders == classification_rows


def test_radar_params_single_row_one_column_per_key(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    row = loaded_con.execute(
        "SELECT recent_window_months, target_min_data_tenders, top_n FROM radar_params"
    ).fetchone()
    assert row == (6, 2, 25)
    n_rows = loaded_con.execute("SELECT count(*) FROM radar_params").fetchone()[0]
    assert n_rows == 1


def test_build_is_rerunnable_from_scratch(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    n1 = loaded_con.execute("SELECT count(*) FROM stg_awards").fetchone()[0]
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    n2 = loaded_con.execute("SELECT count(*) FROM stg_awards").fetchone()[0]
    assert n1 == n2


def test_failed_build_rolls_back_to_previous_tables(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir, tmp_path):
    do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, empty_sql_dir)
    n_before = loaded_con.execute("SELECT count(*) FROM stg_awards").fetchone()[0]
    loaded_con.execute("UPDATE raw_awards SET removed_at = now() WHERE tender_no = 'CCY000ETT21000005'")

    broken_sql_dir = tmp_path / "broken_sql"
    broken_sql_dir.mkdir()
    (broken_sql_dir / "mart_broken.sql").write_text("create or replace view mart_broken as select * from no_such_table")
    with pytest.raises(duckdb.Error):
        do_build(loaded_con, classification_config_path, aliases_path, radar_params_path, broken_sql_dir)

    # The rebuild that would have dropped CCY000ETT21000005 was rolled back.
    assert loaded_con.execute("SELECT count(*) FROM stg_awards").fetchone()[0] == n_before
