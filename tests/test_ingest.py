from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from wonby.ingest import IngestError, ensure_schema, run


def rec(tender_no, supplier, **overrides):
    base = {
        "_id": 1,
        "tender_no": tender_no,
        "tender_description": "desc",
        "agency": "Agency",
        "award_date": "1/4/2021",
        "tender_detail_status": "Awarded to Suppliers",
        "supplier_name": supplier,
        "awarded_amt": "100",
    }
    base.update(overrides)
    return base


class Clock:
    """Deterministic, strictly-increasing clock for tests."""

    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def con(tmp_path):
    connection = duckdb.connect(str(tmp_path / "test.duckdb"))
    yield connection
    connection.close()


def fail_metadata():
    raise RuntimeError("metadata endpoint down")


def make_records_fn(records):
    return lambda: list(records)


# --- first run / insert -----------------------------------------------------------


def test_first_run_inserts_all_rows(con):
    records = [rec("T1", "SUP1"), rec("T2", "SUP2")]
    result = run(
        con,
        full=True,
        fetch_all_records=make_records_fn(records),
        now=Clock(),
    )
    assert result.status == "success"
    assert result.inserted == 2
    assert result.updated == 0
    assert result.removed == 0
    rows = con.execute("SELECT tender_no, supplier_name, first_seen_at, last_seen_at, removed_at FROM raw_awards ORDER BY tender_no").fetchall()
    assert len(rows) == 2
    assert all(r[4] is None for r in rows)  # removed_at
    assert all(r[2] == r[3] for r in rows)  # first_seen_at == last_seen_at on insert

    run_rows = con.execute("SELECT status FROM ingest_runs").fetchall()
    assert run_rows == [("success",)]


# --- idempotence -------------------------------------------------------------------


def test_rerun_unchanged_source_changes_nothing(con):
    records = [rec("T1", "SUP1")]
    run(con, full=True, fetch_all_records=make_records_fn(records), now=Clock())
    first_seen = con.execute("SELECT first_seen_at FROM raw_awards").fetchone()[0]

    result = run(con, full=True, fetch_all_records=make_records_fn(records), now=Clock())
    assert result.inserted == 0
    assert result.updated == 0
    assert result.removed == 0
    first_seen_after = con.execute("SELECT first_seen_at FROM raw_awards").fetchone()[0]
    assert first_seen == first_seen_after  # preserved


# --- updated row --------------------------------------------------------------------


def test_updated_row_changes_hash_and_last_changed_at(con):
    # One shared, strictly-increasing clock across both runs: two independent
    # `Clock()` instances would each restart at the same base timestamp, so
    # the second run's last_changed_at could coincidentally equal the
    # first's, even though it was correctly re-stamped on the update.
    clock = Clock()
    run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1", awarded_amt="100")]), now=clock)
    before = con.execute("SELECT awarded_amt, first_seen_at, last_changed_at FROM raw_awards").fetchone()

    result = run(
        con, full=True,
        fetch_all_records=make_records_fn([rec("T1", "SUP1", awarded_amt="200")]),
        now=clock,
    )
    assert result.updated == 1
    assert result.inserted == 0
    after = con.execute("SELECT awarded_amt, first_seen_at, last_changed_at FROM raw_awards").fetchone()
    assert after[0] == "200"
    assert after[1] == before[1]  # first_seen_at preserved
    assert after[2] != before[2]  # last_changed_at bumped


def test_source_row_id_change_alone_does_not_count_as_updated(con):
    # _id is reassigned on republish and must not be part of change detection.
    run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1", _id=1)]), now=Clock())
    result = run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1", _id=999)]), now=Clock())
    assert result.updated == 0
    assert result.inserted == 0


# --- removed row, and one that comes back -------------------------------------------


def test_removed_row_then_comes_back(con):
    run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1"), rec("T2", "SUP2")]), now=Clock())

    result = run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1")]), now=Clock())
    assert result.removed == 1
    removed_at = con.execute(
        "SELECT removed_at FROM raw_awards WHERE tender_no = 'T2'"
    ).fetchone()[0]
    assert removed_at is not None

    result = run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1"), rec("T2", "SUP2")]), now=Clock())
    assert result.removed == 0
    removed_at_after = con.execute(
        "SELECT removed_at FROM raw_awards WHERE tender_no = 'T2'"
    ).fetchone()[0]
    assert removed_at_after is None


# --- mass-removal guard --------------------------------------------------------------


def test_mass_removal_guard_blocks_run(con):
    records = [rec(f"T{i}", "SUP") for i in range(10)]
    run(con, full=True, fetch_all_records=make_records_fn(records), now=Clock())

    # Only 7 of 10 remain -> 30% removed, over the 20% threshold.
    remaining = records[:7]
    with pytest.raises(IngestError, match="mass-removal"):
        run(con, full=True, fetch_all_records=make_records_fn(remaining), now=Clock())

    # Raw table untouched by the aborted run.
    active = con.execute("SELECT count(*) FROM raw_awards WHERE removed_at IS NULL").fetchone()[0]
    assert active == 10
    assert con.execute("SELECT status FROM ingest_runs ORDER BY run_id DESC LIMIT 1").fetchone() == ("failed",)


def test_mass_removal_guard_bypassed_with_accept_removals(con):
    records = [rec(f"T{i}", "SUP") for i in range(10)]
    run(con, full=True, fetch_all_records=make_records_fn(records), now=Clock())

    remaining = records[:7]
    result = run(
        con, full=True, fetch_all_records=make_records_fn(remaining),
        accept_removals=True, now=Clock(),
    )
    assert result.removed == 3


def test_small_removal_under_threshold_does_not_need_flag(con):
    records = [rec(f"T{i}", "SUP") for i in range(10)]
    run(con, full=True, fetch_all_records=make_records_fn(records), now=Clock())

    remaining = records[:9]  # 10% removed, under 20%
    result = run(con, full=True, fetch_all_records=make_records_fn(remaining), now=Clock())
    assert result.removed == 1


# --- duplicate keys --------------------------------------------------------------------


def test_duplicate_keys_in_fetch_fails_run_and_leaves_raw_untouched(con):
    run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1")]), now=Clock())

    dupes = [rec("T2", "SUP2"), rec("T2", "SUP2")]
    with pytest.raises(IngestError, match="duplicate"):
        run(con, full=True, fetch_all_records=make_records_fn(dupes), now=Clock())

    rows = con.execute("SELECT tender_no FROM raw_awards").fetchall()
    assert rows == [("T1",)]
    assert con.execute("SELECT status, error FROM ingest_runs ORDER BY run_id DESC LIMIT 1").fetchone()[0] == "failed"


# --- failed run leaves raw untouched (generic) ------------------------------------------


def test_failed_fetch_leaves_raw_untouched_and_logs_failed_run(con):
    run(con, full=True, fetch_all_records=make_records_fn([rec("T1", "SUP1")]), now=Clock())

    def boom():
        raise RuntimeError("API on fire")

    with pytest.raises(IngestError):
        run(con, full=True, fetch_all_records=boom, now=Clock())

    rows = con.execute("SELECT tender_no FROM raw_awards").fetchall()
    assert rows == [("T1",)]
    last_run = con.execute(
        "SELECT status, error FROM ingest_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert last_run[0] == "failed"
    assert "API on fire" in last_run[1]


# --- metadata skip path ----------------------------------------------------------------


def test_skip_when_metadata_unchanged(con):
    calls = {"n": 0}

    def fetch_all():
        calls["n"] += 1
        return [rec("T1", "SUP1")]

    run(con, fetch_metadata=lambda: "2026-01-01", fetch_all_records=fetch_all, now=Clock())
    assert calls["n"] == 1

    result = run(con, fetch_metadata=lambda: "2026-01-01", fetch_all_records=fetch_all, now=Clock())
    assert result.status == "skipped"
    assert calls["n"] == 1  # datastore never called again

    run_statuses = con.execute("SELECT status FROM ingest_runs ORDER BY run_id").fetchall()
    assert run_statuses == [("success",), ("skipped",)]


def test_full_flag_forces_fetch_even_if_metadata_unchanged(con):
    calls = {"n": 0}

    def fetch_all():
        calls["n"] += 1
        return [rec("T1", "SUP1")]

    run(con, fetch_metadata=lambda: "2026-01-01", fetch_all_records=fetch_all, now=Clock())
    result = run(con, full=True, fetch_metadata=lambda: "2026-01-01", fetch_all_records=fetch_all, now=Clock())
    assert result.status == "success"
    assert calls["n"] == 2


def test_plain_run_after_full_skips_when_source_unchanged(con):
    # --full must still record lastUpdatedAt, or the next plain run can't skip.
    calls = {"n": 0}

    def fetch_all():
        calls["n"] += 1
        return [rec("T1", "SUP1")]

    clock = Clock()
    full = run(con, full=True, fetch_metadata=lambda: "2026-01-01", fetch_all_records=fetch_all, now=clock)
    assert full.source_last_updated_at == "2026-01-01"
    plain = run(con, fetch_metadata=lambda: "2026-01-01", fetch_all_records=fetch_all, now=clock)
    assert plain.status == "skipped"
    assert calls["n"] == 1


def test_metadata_failure_falls_back_to_full_fetch(con):
    calls = {"n": 0}

    def fetch_all():
        calls["n"] += 1
        return [rec("T1", "SUP1")]

    result = run(con, fetch_metadata=fail_metadata, fetch_all_records=fetch_all, now=Clock())
    assert result.status == "success"
    assert calls["n"] == 1
    assert result.source_last_updated_at is None


def test_different_metadata_triggers_full_fetch(con):
    calls = {"n": 0}

    def fetch_all():
        calls["n"] += 1
        return [rec("T1", "SUP1")]

    run(con, fetch_metadata=lambda: "V1", fetch_all_records=fetch_all, now=Clock())
    result = run(con, fetch_metadata=lambda: "V2", fetch_all_records=fetch_all, now=Clock())
    assert result.status == "success"
    assert calls["n"] == 2


def test_ensure_schema_idempotent(con):
    ensure_schema(con)
    ensure_schema(con)
    assert con.execute("SELECT count(*) FROM raw_awards").fetchone()[0] == 0
