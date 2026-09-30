"""Tests for the analysis marts (via a hand-built stg_awards/tender_classification/
radar_params + the real sql/*.sql files) and for report.py's CSV exports,
report_runs logging and the Nouveautés watermark rule.

No network, no real ingest/build pipeline: tables are built directly so each
scenario (shared amounts, no-supplier rows, anchored windows, ...) is explicit
and isolated. See CLAUDE.md > Business rules for the semantics under test.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from wonby import report, settings

TS0 = datetime(2024, 1, 1, 0, 0, 0)  # first_seen_at shared by every row built below

STG_COLUMNS = [
    "tender_no",
    "tender_description",
    "agency",
    "tender_detail_status",
    "supplier_name_raw",
    "supplier_name_norm",
    "supplier_group",
    "award_date",
    "fiscal_year",
    "awarded_amt",
    "is_no_supplier",
    "n_suppliers_in_tender",
    "amt_shared_across_suppliers",
    "is_nominal_amt",
    "first_seen_at",
]
CLASS_COLUMNS = ["tender_no", "is_data", "matched_includes", "matched_excludes", "config_hash"]


def _fiscal_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _stg_row(
    tender_no: str,
    description: str,
    agency: str,
    supplier: str | None,
    award_date: date,
    amount: float,
    *,
    is_no_supplier: bool = False,
    n_suppliers: int = 1,
    shared: bool = False,
    nominal: bool = False,
    first_seen_at: datetime = TS0,
) -> tuple:
    if is_no_supplier:
        supplier_raw, supplier_norm, supplier_group = "Unknown", "UNKNOWN", "UNKNOWN"
    else:
        supplier_raw = supplier_norm = supplier_group = supplier
    return (
        tender_no,
        description,
        agency,
        "Awarded to No Suppliers" if is_no_supplier else "Awarded",
        supplier_raw,
        supplier_norm,
        supplier_group,
        award_date,
        _fiscal_year(award_date),
        amount,
        is_no_supplier,
        n_suppliers,
        shared,
        nominal,
        first_seen_at,
    )


# Anchor across the whole fixture is max(award_date) = 2024-06-30 (WIN01).
# radar_params: recent_window_months=3 (window start 2024-03-30),
#               target_window_months=6 (window start 2023-12-30), target_min=2.
FIXTURE_ROWS = [
    # Group 1 — anchored windows (mart_recent_data_awards)
    _stg_row("WIN01", "Data analytics platform", "WINAG", "WINSUP", date(2024, 6, 30), 1000),
    _stg_row("WIN02", "Data analytics platform", "WINAG", "WINSUP", date(2023, 1, 1), 2000),
    # Group 2 — priority-target threshold (target_min_data_tenders = 2)
    _stg_row("TH2A", "Data analytics dashboard build", "AG_TH", "THRESH2", date(2024, 6, 15), 5000),
    _stg_row("TH2B", "Data warehouse migration", "AG_TH", "THRESH2", date(2024, 4, 1), 5000),
    _stg_row("TH1A", "Business intelligence rollout", "AG_TH", "THRESH1", date(2024, 6, 10), 5000),
    # Group 3 — shared amount dedup (agency) + total_amt_excl_shared (supplier)
    _stg_row(
        "SHARE01",
        "Data platform build, joint award",
        "AG_SHARE",
        "SHSUP_X",
        date(2024, 6, 12),
        90000,
        n_suppliers=2,
        shared=True,
    ),
    _stg_row(
        "SHARE01",
        "Data platform build, joint award",
        "AG_SHARE",
        "SHSUP_Y",
        date(2024, 6, 12),
        90000,
        n_suppliers=2,
        shared=True,
    ),
    _stg_row("SOLOX", "Data quality audit", "AG_SHARE", "SHSUP_X", date(2024, 6, 1), 10000),
    # Group 4 — no-supplier tender, excluded from every supplier ranking
    _stg_row(
        "NOSUP01",
        "Data governance framework",
        "AG_NOSUP",
        None,
        date(2024, 6, 8),
        0,
        is_no_supplier=True,
    ),
    # Group 5 — non-"data" tender (same supplier/agency as group 2): must be fully excluded
    _stg_row(
        "NOTDATA01", "Canteen renovation works", "AG_TH", "THRESH2", date(2024, 6, 20), 999999
    ),
]

DATA_FLAG = {
    "WIN01": True,
    "WIN02": True,
    "TH2A": True,
    "TH2B": True,
    "TH1A": True,
    "SHARE01": True,
    "SOLOX": True,
    "NOSUP01": True,
    "NOTDATA01": False,
}


def _insert_stg_rows(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    con.executemany(
        f"INSERT INTO stg_awards ({', '.join(STG_COLUMNS)}) VALUES ({', '.join(['?'] * len(STG_COLUMNS))})",
        rows,
    )


def _insert_class_rows(con: duckdb.DuckDBPyConnection, flags: dict[str, bool]) -> None:
    con.executemany(
        f"INSERT INTO tender_classification ({', '.join(CLASS_COLUMNS)}) VALUES (?, ?, ?, ?, ?)",
        [(tn, is_data, [], [], "testhash") for tn, is_data in flags.items()],
    )


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "wonby.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE stg_awards (
                tender_no VARCHAR, tender_description VARCHAR, agency VARCHAR,
                tender_detail_status VARCHAR, supplier_name_raw VARCHAR,
                supplier_name_norm VARCHAR, supplier_group VARCHAR,
                award_date DATE, fiscal_year BIGINT, awarded_amt DECIMAL(18,2),
                is_no_supplier BOOLEAN, n_suppliers_in_tender BIGINT,
                amt_shared_across_suppliers BOOLEAN, is_nominal_amt BOOLEAN,
                first_seen_at TIMESTAMP
            )
            """
        )
        con.execute(
            """
            CREATE TABLE tender_classification (
                tender_no VARCHAR, is_data BOOLEAN,
                matched_includes VARCHAR[], matched_excludes VARCHAR[], config_hash VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE radar_params (
                recent_window_months BIGINT, target_window_months BIGINT,
                target_min_data_tenders BIGINT, top_n BIGINT
            )
            """
        )
        con.execute("INSERT INTO radar_params VALUES (3, 6, 2, 10)")

        _insert_stg_rows(con, FIXTURE_ROWS)
        _insert_class_rows(con, DATA_FLAG)

        for sql_file in sorted(settings.SQL_DIR.glob("*.sql")):
            con.execute(sql_file.read_text())
    finally:
        con.close()
    return path


@pytest.fixture
def con(db_path):
    connection = duckdb.connect(str(db_path), read_only=True)
    yield connection
    connection.close()


# --- mart logic (queried directly against the views) --------------------------------


def test_anchored_window_excludes_old_row_and_includes_recent_one(con):
    # Anchor is 2024-06-30 (max award_date in the fixture), not today's real date.
    # If the SQL wrongly used current_date, WIN01 (2024-06-30) would be excluded
    # too (today is years later) — so asserting WIN01 IS present is what actually
    # proves anchoring, not just that the far-older WIN02 is excluded.
    tenders = {
        r[0] for r in con.execute("SELECT tender_no FROM mart_recent_data_awards").fetchall()
    }
    assert "WIN01" in tenders
    assert "WIN02" not in tenders


def test_priority_target_threshold(con):
    groups = {
        r[0] for r in con.execute("SELECT supplier_group FROM mart_priority_targets").fetchall()
    }
    assert "THRESH2" in groups  # 2 distinct recent data tenders, meets target_min_data_tenders=2
    assert "THRESH1" not in groups  # only 1

    row = con.execute(
        "SELECT n_recent_data_tenders FROM mart_priority_targets WHERE supplier_group = 'THRESH2'"
    ).fetchone()
    assert row[0] == 2  # NOTDATA01 (same supplier/agency) must not inflate this


def test_agency_total_counts_shared_tender_amount_once(con):
    row = con.execute(
        "SELECT n_data_tenders, total_amt, n_distinct_suppliers FROM mart_top_data_agencies WHERE agency = 'AG_SHARE'"
    ).fetchone()
    assert row == (2, 100000, 2)  # SHARE01 (90000, shared, counted once) + SOLOX (10000)


def test_total_amt_excl_shared_drops_shared_and_nominal_rows(con):
    x = con.execute(
        "SELECT total_amt, total_amt_excl_shared FROM mart_top_data_suppliers WHERE supplier_group = 'SHSUP_X'"
    ).fetchone()
    assert x == (100000, 10000)  # SHARE01 (shared, 90000) excluded; SOLOX (10000) kept

    y = con.execute(
        "SELECT total_amt, total_amt_excl_shared FROM mart_top_data_suppliers WHERE supplier_group = 'SHSUP_Y'"
    ).fetchone()
    assert y[0] == 90000
    assert y[1] is None  # every row for this supplier is shared -> nothing left to sum


def test_no_supplier_rows_excluded_from_supplier_rankings(con):
    assert (
        con.execute(
            "SELECT count(*) FROM mart_top_data_suppliers WHERE supplier_group = 'UNKNOWN'"
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT count(*) FROM mart_supplier_agencies WHERE supplier_group = 'UNKNOWN'"
        ).fetchone()[0]
        == 0
    )
    assert (
        con.execute(
            "SELECT count(*) FROM mart_priority_targets WHERE supplier_group = 'UNKNOWN'"
        ).fetchone()[0]
        == 0
    )

    # but it's still a real award row, kept (and flagged) in the raw recent-awards feed
    flagged = con.execute(
        "SELECT is_no_supplier FROM mart_recent_data_awards WHERE tender_no = 'NOSUP01'"
    ).fetchone()
    assert flagged == (True,)


def test_non_data_tender_fully_excluded(con):
    for mart in (
        "mart_top_data_suppliers",
        "mart_top_data_agencies",
        "mart_recent_data_awards",
        "mart_supplier_agencies",
        "mart_priority_targets",
    ):
        rows = con.execute(f"SELECT * FROM {mart}").fetchall()
        cols = [d[0] for d in con.description]
        if "tender_no" in cols:
            idx = cols.index("tender_no")
            assert all(r[idx] != "NOTDATA01" for r in rows)

    total = con.execute(
        "SELECT total_amt FROM mart_top_data_suppliers WHERE supplier_group = 'THRESH2'"
    ).fetchone()[0]
    assert total == 10000  # TH2A + TH2B only; NOTDATA01's 999999 must not leak in


# --- report.py: CSV exports, report_runs, Nouveautés --------------------------------


def _section(markdown: str, heading: str) -> str:
    m = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", markdown, re.MULTILINE | re.DOTALL
    )
    assert m, f"section {heading!r} not found in report"
    return m.group(1)


def test_csv_exports_written(db_path, tmp_path):
    output_dir = tmp_path / "output"
    report.run(db_path, output_dir)
    exports_dir = output_dir / "exports"
    for mart in report.MARTS:
        csv_path = exports_dir / f"{mart}.csv"
        assert csv_path.exists()
        lines = [line for line in csv_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) >= 2, f"{mart}.csv has no data rows"  # header + >=1 row


def test_report_runs_row_created(db_path, tmp_path):
    output_dir = tmp_path / "output"
    report_path = report.run(db_path, output_dir)

    con2 = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con2.execute(
            "SELECT generated_at, data_watermark, anchor_date, report_path FROM report_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        con2.close()

    assert row[0] is not None
    assert row[1] == TS0
    assert row[2] == date(2024, 6, 30)
    assert row[3] == str(report_path)


def test_nouveautes_first_report_says_so(db_path, tmp_path):
    output_dir = tmp_path / "output"
    report_path = report.run(db_path, output_dir)
    section = _section(report_path.read_text(encoding="utf-8"), "Nouveautés")
    assert "Premier rapport" in section
    assert (
        "attributions data récentes" in section.lower() or "attributions data r" in section.lower()
    )


def test_nouveautes_stable_then_new_rows_appear(db_path, tmp_path):
    output_dir = tmp_path / "output"

    text_1 = report.run(db_path, output_dir).read_text(encoding="utf-8")
    section_1 = _section(text_1, "Nouveautés")
    assert "NEWSUP" not in section_1

    # Regenerating immediately on unchanged data must give the identical Nouveautés.
    text_2 = report.run(db_path, output_dir).read_text(encoding="utf-8")
    section_2 = _section(text_2, "Nouveautés")
    assert section_2 == section_1

    # Simulate a fresh ingest: a new data award with a later first_seen_at.
    con2 = duckdb.connect(str(db_path))
    try:
        _insert_stg_rows(
            con2,
            [
                _stg_row(
                    "NEW01",
                    "New data analytics award",
                    "AG_NEW",
                    "NEWSUP",
                    date(2024, 6, 29),
                    7000,
                    first_seen_at=TS0 + timedelta(days=1),
                )
            ],
        )
        _insert_class_rows(con2, {"NEW01": True})
    finally:
        con2.close()

    text_3 = report.run(db_path, output_dir).read_text(encoding="utf-8")
    section_3 = _section(text_3, "Nouveautés")
    assert "NEWSUP" in section_3
    assert "WINSUP" not in section_3  # pre-existing rows aren't "new" again
    assert "THRESH2" not in section_3
