"""Incremental MERGE of the datastore into `raw_awards`, with `ingest_runs` log.

Change-detection + full fetch + MERGE — never offset-based incremental. See
CLAUDE.md > Ingestion semantics for the full contract.
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import duckdb

from . import api
from .settings import DATASET_ID

logger = logging.getLogger(__name__)

MASS_REMOVAL_FRACTION = 0.2
# Below this many active rows, a percentage-based guard is meaningless (1 row
# out of 2 is "50% removed" but not an anomaly) — skip the guard entirely
# rather than let a tiny/early dataset trip it. The real dataset is ~18.5k
# active rows, so this threshold is far below anything that matters there.
MASS_REMOVAL_MIN_ACTIVE_ROWS = 10

FetchMetadataFn = Callable[[], str]
FetchAllRecordsFn = Callable[[], list[dict[str, Any]]]
NowFn = Callable[[], datetime]


class IngestError(RuntimeError):
    """A run failed; the failure is already logged in `ingest_runs`."""


def _utcnow() -> datetime:
    # DuckDB TIMESTAMP columns drop tzinfo without converting: only ever store UTC.
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class IngestRunResult:
    status: str  # 'success' | 'skipped'
    source_last_updated_at: str | None
    api_total: int | None
    fetched: int | None
    inserted: int | None
    updated: int | None
    removed: int | None


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_awards (
            tender_no VARCHAR NOT NULL,
            supplier_name VARCHAR NOT NULL,
            tender_description VARCHAR,
            agency VARCHAR,
            award_date VARCHAR,
            tender_detail_status VARCHAR,
            awarded_amt VARCHAR,
            source_row_id INTEGER,
            row_hash VARCHAR,
            first_seen_at TIMESTAMP,
            last_seen_at TIMESTAMP,
            last_changed_at TIMESTAMP,
            removed_at TIMESTAMP,
            PRIMARY KEY (tender_no, supplier_name)
        )
        """
    )
    con.execute("CREATE SEQUENCE IF NOT EXISTS ingest_runs_id_seq")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_runs (
            run_id BIGINT PRIMARY KEY DEFAULT nextval('ingest_runs_id_seq'),
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            status VARCHAR,
            source_last_updated_at VARCHAR,
            api_total INTEGER,
            fetched_count INTEGER,
            inserted_count INTEGER,
            updated_count INTEGER,
            removed_count INTEGER,
            error VARCHAR
        )
        """
    )


def _last_successful_source_last_updated_at(con: duckdb.DuckDBPyConnection) -> str | None:
    row = con.execute(
        "SELECT source_last_updated_at FROM ingest_runs WHERE status = 'success' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _insert_run_row(
    con: duckdb.DuckDBPyConnection,
    *,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    source_last_updated_at: str | None,
    api_total: int | None = None,
    fetched: int | None = None,
    inserted: int | None = None,
    updated: int | None = None,
    removed: int | None = None,
    error: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO ingest_runs (
            started_at, finished_at, status, source_last_updated_at,
            api_total, fetched_count, inserted_count, updated_count, removed_count, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            started_at, finished_at, status, source_last_updated_at,
            api_total, fetched, inserted, updated, removed, error,
        ],
    )


def _validate_records(records: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for r in records:
        tender_no = r.get("tender_no")
        supplier_name = r.get("supplier_name")
        if not tender_no or not supplier_name:
            raise IngestError(f"record missing tender_no/supplier_name: {r!r}")
        key = (tender_no, supplier_name)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if duplicates:
        sample = sorted(duplicates)[:5]
        raise IngestError(f"{len(duplicates)} duplicate (tender_no, supplier_name) keys, e.g. {sample}")


def _stage_records(con: duckdb.DuckDBPyConnection, records: list[dict[str, Any]]) -> None:
    """Load fetched records into a TEMP table without row-by-row inserts, via
    a staged JSON file (avoids ~18k executemany round trips)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(records, f)
        tmp_path = f.name
    try:
        con.execute("DROP TABLE IF EXISTS stg_fetch")
        con.execute(
            "CREATE TEMP TABLE stg_fetch AS "
            "SELECT * FROM read_json(?, format = 'array')",
            [tmp_path],
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    con.execute("DROP TABLE IF EXISTS stg_typed")
    con.execute(
        """
        CREATE TEMP TABLE stg_typed AS
        SELECT
            tender_no,
            supplier_name,
            tender_description,
            agency,
            award_date,
            tender_detail_status,
            CAST(awarded_amt AS VARCHAR) AS awarded_amt,
            CAST(_id AS INTEGER) AS source_row_id,
            md5(
                coalesce(tender_description, '') || '|' ||
                coalesce(agency, '') || '|' ||
                coalesce(award_date, '') || '|' ||
                coalesce(tender_detail_status, '') || '|' ||
                coalesce(CAST(awarded_amt AS VARCHAR), '')
            ) AS row_hash
        FROM stg_fetch
        """
    )


def run(
    con: duckdb.DuckDBPyConnection,
    *,
    full: bool = False,
    accept_removals: bool = False,
    fetch_metadata: FetchMetadataFn | None = None,
    fetch_all_records: FetchAllRecordsFn | None = None,
    now: NowFn = _utcnow,
) -> IngestRunResult:
    fetch_metadata = fetch_metadata or api.fetch_metadata
    fetch_all_records = fetch_all_records or (lambda: api.fetch_all_records(dataset_id=DATASET_ID))

    ensure_schema(con)
    started_at = now()

    # Fetched even under --full: the value recorded in ingest_runs is the baseline
    # the next plain run compares against.
    source_last_updated_at: str | None = None
    try:
        source_last_updated_at = fetch_metadata()
    except Exception as exc:  # noqa: BLE001 - any metadata failure falls back to full fetch
        logger.warning("metadata check failed (%s); doing a full fetch instead", exc)
    else:
        if not full:
            last_success = _last_successful_source_last_updated_at(con)
            if last_success is not None and source_last_updated_at == last_success:
                finished_at = now()
                _insert_run_row(
                    con,
                    started_at=started_at,
                    finished_at=finished_at,
                    status="skipped",
                    source_last_updated_at=source_last_updated_at,
                )
                logger.info("ingest skipped: source unchanged since last successful run")
                return IngestRunResult(
                    status="skipped",
                    source_last_updated_at=source_last_updated_at,
                    api_total=None,
                    fetched=None,
                    inserted=None,
                    updated=None,
                    removed=None,
                )

    try:
        records = fetch_all_records()
        _validate_records(records)

        con.execute("BEGIN TRANSACTION")
        try:
            _stage_records(con, records)

            active_before = con.execute(
                "SELECT count(*) FROM raw_awards WHERE removed_at IS NULL"
            ).fetchone()[0]

            inserted = con.execute(
                """
                SELECT count(*) FROM stg_typed s
                WHERE NOT EXISTS (
                    SELECT 1 FROM raw_awards t
                    WHERE t.tender_no = s.tender_no AND t.supplier_name = s.supplier_name
                )
                """
            ).fetchone()[0]

            updated = con.execute(
                """
                SELECT count(*) FROM stg_typed s
                JOIN raw_awards t
                  ON t.tender_no = s.tender_no AND t.supplier_name = s.supplier_name
                WHERE t.row_hash != s.row_hash OR t.removed_at IS NOT NULL
                """
            ).fetchone()[0]

            removed = con.execute(
                """
                SELECT count(*) FROM raw_awards t
                WHERE t.removed_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM stg_typed s
                    WHERE s.tender_no = t.tender_no AND s.supplier_name = t.supplier_name
                  )
                """
            ).fetchone()[0]

            if (
                active_before >= MASS_REMOVAL_MIN_ACTIVE_ROWS
                and removed > MASS_REMOVAL_FRACTION * active_before
                and not accept_removals
            ):
                raise IngestError(
                    f"mass-removal guard: {removed} of {active_before} active rows "
                    f"({removed / active_before:.0%}) would be marked removed; "
                    f"re-run with --accept-removals if this is expected"
                )

            now_ts = now()

            con.execute(
                """
                MERGE INTO raw_awards AS t
                USING stg_typed AS s
                ON t.tender_no = s.tender_no AND t.supplier_name = s.supplier_name
                WHEN MATCHED AND t.row_hash != s.row_hash THEN UPDATE SET
                    tender_description = s.tender_description,
                    agency = s.agency,
                    award_date = s.award_date,
                    tender_detail_status = s.tender_detail_status,
                    awarded_amt = s.awarded_amt,
                    source_row_id = s.source_row_id,
                    row_hash = s.row_hash,
                    last_seen_at = ?,
                    last_changed_at = ?,
                    removed_at = NULL
                WHEN MATCHED THEN UPDATE SET
                    source_row_id = s.source_row_id,
                    last_seen_at = ?,
                    removed_at = NULL
                WHEN NOT MATCHED THEN INSERT (
                    tender_no, supplier_name, tender_description, agency, award_date,
                    tender_detail_status, awarded_amt, source_row_id, row_hash,
                    first_seen_at, last_seen_at, last_changed_at, removed_at
                ) VALUES (
                    s.tender_no, s.supplier_name, s.tender_description, s.agency, s.award_date,
                    s.tender_detail_status, s.awarded_amt, s.source_row_id, s.row_hash,
                    ?, ?, ?, NULL
                )
                """,
                [now_ts, now_ts, now_ts, now_ts, now_ts, now_ts],
            )

            if removed > 0:
                con.execute(
                    """
                    UPDATE raw_awards
                    SET removed_at = ?
                    WHERE removed_at IS NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM stg_typed s
                        WHERE s.tender_no = raw_awards.tender_no
                          AND s.supplier_name = raw_awards.supplier_name
                      )
                    """,
                    [now_ts],
                )
                logger.warning("%d rows marked removed this run", removed)

            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception as exc:  # noqa: BLE001 - log a failed run row, then re-raise
        finished_at = now()
        _insert_run_row(
            con,
            started_at=started_at,
            finished_at=finished_at,
            status="failed",
            source_last_updated_at=source_last_updated_at,
            error=str(exc)[:2000],
        )
        raise IngestError(str(exc)) from exc

    finished_at = now()
    _insert_run_row(
        con,
        started_at=started_at,
        finished_at=finished_at,
        status="success",
        source_last_updated_at=source_last_updated_at,
        api_total=len(records),
        fetched=len(records),
        inserted=inserted,
        updated=updated,
        removed=removed,
    )
    logger.info(
        "ingest success: fetched=%d inserted=%d updated=%d removed=%d",
        len(records), inserted, updated, removed,
    )
    return IngestRunResult(
        status="success",
        source_last_updated_at=source_last_updated_at,
        api_total=len(records),
        fetched=len(records),
        inserted=inserted,
        updated=updated,
        removed=removed,
    )
