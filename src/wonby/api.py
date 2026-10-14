"""HTTP API: read-only JSON over the DuckDB warehouse (FastAPI).

Every request opens its own short read-only connection and closes it, so
`wonby ingest` / `wonby build` can take DuckDB's write lock between requests.
While they hold it, requests get a 503 with Retry-After. See docs/adr/0006.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Literal

import duckdb
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import settings

logger = logging.getLogger(__name__)

# ingest/build hold the write lock for a few seconds on the current dataset size.
RETRY_AFTER_SECONDS = 5

DESCRIPTION = """\
Who wins Singapore public-sector tenders: awards, suppliers, agencies and
prospecting targets. Read-only.

Independent project, not affiliated with GeBIZ, the Ministry of Finance or the
Government of Singapore. Contains information from "Government Procurement via
GeBIZ" accessed from [data.gov.sg](https://data.gov.sg) (source version:
`/v1/health`), which is made available under the terms of the
[Singapore Open Data Licence version 1.0](https://data.gov.sg/open-data-licence).
"""


def get_con(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    db_path: Path = request.app.state.db_path
    if not db_path.exists():
        raise HTTPException(503, "Database not built yet.")
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.IOException as exc:
        # Another process holds the write lock: ingest or build is running.
        logger.warning("database unavailable: %s", exc)
        raise HTTPException(
            503,
            "Data is being refreshed, retry shortly.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from exc
    try:
        yield con
    finally:
        con.close()


Con = Annotated[duckdb.DuckDBPyConnection, Depends(get_con)]

router = APIRouter(prefix="/v1")


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    n_awards: int = Field(description="Active award rows (one per tender and supplier).")
    n_tenders: int = Field(description="Distinct tenders.")
    first_award_date: date | None
    last_award_date: date | None = Field(
        description="Latest award in the data. The source is published by fiscal year, "
        "so this lags today by months."
    )
    source_last_updated_at: datetime | None = Field(
        description="When data.gov.sg last updated the dataset, as of the last successful ingest."
    )


@router.get("/health", tags=["meta"])
def health(con: Con) -> Health:
    n_awards, n_tenders, first_award_date, last_award_date = con.execute(
        """
        SELECT count(*), count(DISTINCT tender_no), min(award_date), max(award_date)
        FROM stg_awards
        """
    ).fetchone()
    # run_id, not finished_at: run timestamps are stored without a time zone.
    source = con.execute(
        """
        SELECT source_last_updated_at FROM ingest_runs
        WHERE status = 'success'
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    return Health(
        n_awards=n_awards,
        n_tenders=n_tenders,
        first_award_date=first_award_date,
        last_award_date=last_award_date,
        source_last_updated_at=source[0] if source else None,
    )


def create_app(db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="WonBy API", version=version("wonby"), description=DESCRIPTION)
    app.state.db_path = db_path or settings.DB_PATH
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    return app
