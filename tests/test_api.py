import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from wonby import api, build, ingest

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_LAST_UPDATED_AT = "2026-04-26T21:48:27+08:00"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "wonby.duckdb"
    records = json.loads((FIXTURES / "api_records.json").read_text())
    con = duckdb.connect(str(path))
    try:
        ingest.run(
            con,
            full=True,
            fetch_metadata=lambda: SOURCE_LAST_UPDATED_AT,
            fetch_all_records=lambda: records,
        )
        build.run(con)
    finally:
        con.close()
    return path


@pytest.fixture
def client(db_path):
    return TestClient(api.create_app(db_path))


def test_health_reports_coverage_and_source_version(client):
    response = client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "n_awards": 21,
        "n_tenders": 15,
        "first_award_date": "2021-04-30",
        "last_award_date": "2025-08-14",
        "source_last_updated_at": SOURCE_LAST_UPDATED_AT,
    }


def test_connection_is_released_after_each_request(client, db_path):
    client.get("/v1/health")

    # A read-only connection still open in this process would make this fail.
    duckdb.connect(str(db_path)).close()


def test_missing_database_returns_503(tmp_path):
    client = TestClient(api.create_app(tmp_path / "missing.duckdb"))

    response = client.get("/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database not built yet."}


def test_database_locked_by_pipeline_returns_503_with_retry_after(client, db_path):
    # A separate process holding the write lock, like `wonby build` mid-run.
    writer = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import duckdb, sys; con = duckdb.connect(sys.argv[1]); print('locked', flush=True);"
            " sys.stdin.read()",
            str(db_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout.readline().strip() == "locked"

        response = client.get("/v1/health")
    finally:
        writer.communicate()

    assert response.status_code == 503
    assert response.headers["retry-after"] == str(api.RETRY_AFTER_SECONDS)
    assert client.get("/v1/health").status_code == 200


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
