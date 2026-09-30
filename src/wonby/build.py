"""Rebuilds `stg_awards`, `tender_classification`, `radar_params` from scratch
in one transaction, then runs every `sql/*.sql` mart file in name order.

`clean.py` / `classify.py` stay pure; this module is where they're wired into
DuckDB. Both stg_awards and tender_classification are built by applying the
pure functions in Python then bulk-loading the result via a staged JSON temp
table (`con.create_function` DuckDB UDFs would need numpy, which isn't a
project dependency — see CLAUDE.md).
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import yaml

from . import settings
from .classify import classify, load_classification_config
from .clean import (
    apply_aliases,
    fiscal_year,
    normalize_supplier_name,
    parse_amount,
    parse_award_date,
    supplier_group,
)

logger = logging.getLogger(__name__)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _stage_json_rows(
    con: duckdb.DuckDBPyConnection, records: list[dict[str, Any]], stage_name: str
) -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(records, f, default=str)
        tmp_path = f.name
    try:
        con.execute(f"DROP TABLE IF EXISTS {stage_name}")
        con.execute(
            f"CREATE TEMP TABLE {stage_name} AS SELECT * FROM read_json(?, format = 'array')",
            [tmp_path],
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


_STG_CLEAN_EMPTY_SCHEMA = """
    tender_no VARCHAR, tender_description VARCHAR, agency VARCHAR,
    tender_detail_status VARCHAR, supplier_name_raw VARCHAR,
    supplier_name_norm VARCHAR, supplier_group VARCHAR,
    award_date VARCHAR, fiscal_year BIGINT, awarded_amt VARCHAR,
    is_no_supplier BOOLEAN, first_seen_at VARCHAR
"""


def _clean_active_rows(
    con: duckdb.DuckDBPyConnection, aliases: dict[str, str]
) -> list[dict[str, Any]]:
    """Read active raw rows and apply the pure clean.py functions in Python.

    `con.create_function` (DuckDB Python UDFs) unconditionally requires numpy
    in the installed DuckDB build, which isn't a project dependency — see
    CLAUDE.md > build.py: "register ... as DuckDB Python UDFs ... or apply
    them in Python". This is the Python path; the dataset is ~18.5k rows, so
    a plain Python loop is fast and keeps the numpy dependency out.
    """
    rows = con.execute(
        """
        SELECT tender_no, tender_description, agency, tender_detail_status,
               supplier_name, award_date, awarded_amt, first_seen_at
        FROM raw_awards
        WHERE removed_at IS NULL
        """
    ).fetchall()

    cleaned: list[dict[str, Any]] = []
    for (
        tender_no,
        tender_description,
        agency,
        tender_detail_status,
        supplier_name_raw,
        award_date_raw,
        awarded_amt_raw,
        first_seen_at,
    ) in rows:
        norm = normalize_supplier_name(supplier_name_raw or "")
        group = apply_aliases(supplier_group(norm), aliases)
        parsed_date = parse_award_date(award_date_raw)
        amount = parse_amount(awarded_amt_raw)
        cleaned.append(
            {
                "tender_no": tender_no,
                "tender_description": tender_description,
                "agency": agency,
                "tender_detail_status": tender_detail_status,
                "supplier_name_raw": supplier_name_raw,
                "supplier_name_norm": norm,
                "supplier_group": group,
                "award_date": parsed_date.isoformat() if parsed_date else None,
                "fiscal_year": fiscal_year(parsed_date) if parsed_date else None,
                "awarded_amt": str(amount) if amount is not None else None,
                "is_no_supplier": tender_detail_status == "Awarded to No Suppliers",
                "first_seen_at": first_seen_at.isoformat() if first_seen_at else None,
            }
        )
    return cleaned


def _build_stg_awards(con: duckdb.DuckDBPyConnection, aliases: dict[str, str]) -> None:
    cleaned = _clean_active_rows(con, aliases)
    if cleaned:
        _stage_json_rows(con, cleaned, "stg_clean")
    else:
        con.execute("DROP TABLE IF EXISTS stg_clean")
        con.execute(f"CREATE TEMP TABLE stg_clean ({_STG_CLEAN_EMPTY_SCHEMA})")

    con.execute(
        """
        CREATE OR REPLACE TABLE stg_awards AS
        WITH base AS (
            SELECT
                tender_no,
                tender_description,
                agency,
                tender_detail_status,
                supplier_name_raw,
                supplier_name_norm,
                supplier_group,
                CAST(award_date AS DATE) AS award_date,
                fiscal_year,
                CAST(awarded_amt AS DECIMAL(18,2)) AS awarded_amt,
                is_no_supplier,
                CAST(first_seen_at AS TIMESTAMP) AS first_seen_at
            FROM stg_clean
        )
        SELECT
            tender_no,
            tender_description,
            agency,
            tender_detail_status,
            supplier_name_raw,
            supplier_name_norm,
            supplier_group,
            award_date,
            fiscal_year,
            awarded_amt,
            is_no_supplier,
            count(*) OVER (PARTITION BY tender_no) AS n_suppliers_in_tender,
            (
                count(*) OVER (PARTITION BY tender_no) > 1
                AND count(DISTINCT awarded_amt) OVER (PARTITION BY tender_no) = 1
            ) AS amt_shared_across_suppliers,
            coalesce(awarded_amt = 1, false) AS is_nominal_amt,
            first_seen_at
        FROM base
        """
    )


def _build_tender_classification(con: duckdb.DuckDBPyConnection, config_path: Path) -> None:
    config = load_classification_config(config_path)
    rows = con.execute("SELECT DISTINCT tender_no, tender_description FROM stg_awards").fetchall()

    records = []
    for tender_no, description in rows:
        result = classify(description, config)
        records.append(
            {
                "tender_no": tender_no,
                "is_data": result.is_data,
                "matched_includes": result.matched_includes,
                "matched_excludes": result.matched_excludes,
                "config_hash": config.config_hash,
            }
        )

    if records:
        _stage_json_rows(con, records, "stg_tender_classification")
        con.execute(
            """
            CREATE OR REPLACE TABLE tender_classification AS
            SELECT
                tender_no,
                is_data,
                matched_includes,
                matched_excludes,
                config_hash
            FROM stg_tender_classification
            """
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE TABLE tender_classification (
                tender_no VARCHAR,
                is_data BOOLEAN,
                matched_includes VARCHAR[],
                matched_excludes VARCHAR[],
                config_hash VARCHAR
            )
            """
        )


_PARAM_TYPE_MAP = {bool: "BOOLEAN", int: "BIGINT", float: "DOUBLE", str: "VARCHAR"}


def _build_radar_params(con: duckdb.DuckDBPyConnection, params: dict[str, Any]) -> None:
    if not params:
        con.execute("CREATE OR REPLACE TABLE radar_params (_empty BOOLEAN)")
        con.execute("INSERT INTO radar_params VALUES (true)")
        return
    columns = []
    values = []
    for key, value in params.items():
        duck_type = _PARAM_TYPE_MAP.get(type(value), "VARCHAR")
        columns.append(f'"{key}" {duck_type}')
        values.append(value)
    con.execute(f"CREATE OR REPLACE TABLE radar_params ({', '.join(columns)})")
    placeholders = ", ".join(["?"] * len(values))
    con.execute(f"INSERT INTO radar_params VALUES ({placeholders})", values)


def _run_sql_dir(con: duckdb.DuckDBPyConnection, sql_dir: Path) -> list[str]:
    if not sql_dir.exists():
        return []
    executed = []
    for sql_file in sorted(sql_dir.glob("*.sql")):
        logger.info("running %s", sql_file.name)
        con.execute(sql_file.read_text())
        executed.append(sql_file.name)
    return executed


def run(
    con: duckdb.DuckDBPyConnection,
    *,
    classification_config_path: Path = settings.CLASSIFICATION_CONFIG_PATH,
    aliases_path: Path = settings.SUPPLIER_ALIASES_PATH,
    radar_params_path: Path = settings.RADAR_PARAMS_PATH,
    sql_dir: Path = settings.SQL_DIR,
) -> dict[str, Any]:
    aliases = (_load_yaml(aliases_path) or {}).get("aliases") or {}
    radar_params = _load_yaml(radar_params_path)

    con.execute("BEGIN TRANSACTION")
    try:
        _build_stg_awards(con, aliases)
        _build_tender_classification(con, classification_config_path)
        _build_radar_params(con, radar_params)
        executed_sql = _run_sql_dir(con, sql_dir)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    summary = _summary(con)
    summary["sql_files"] = executed_sql
    _print_summary(summary)
    return summary


def _summary(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute("SELECT count(*) FROM stg_awards").fetchone()[0]
    tenders = con.execute("SELECT count(DISTINCT tender_no) FROM stg_awards").fetchone()[0]
    data_tenders = con.execute(
        "SELECT count(*) FROM tender_classification WHERE is_data"
    ).fetchone()[0]
    date_range = con.execute(
        "SELECT min(award_date), max(award_date) FROM stg_awards WHERE award_date IS NOT NULL"
    ).fetchone()
    return {
        "rows": rows,
        "tenders": tenders,
        "data_tenders": data_tenders,
        "date_min": date_range[0],
        "date_max": date_range[1],
    }


def _print_summary(summary: dict[str, Any]) -> None:
    print(
        f"stg_awards: {summary['rows']} rows, {summary['tenders']} tenders "
        f"({summary['date_min']} .. {summary['date_max']}); "
        f"{summary['data_tenders']} data tenders; "
        f"{len(summary['sql_files'])} mart(s) built"
    )
