# 0006 - Serve the API with a short read-only DuckDB connection per request

Status: Accepted (2026-10-14)

## Context

WonBy gets a read-only HTTP API (FastAPI), first as a public demo. It reads the
same DuckDB file that `wonby ingest` and `wonby build` write (ADR 0002).

DuckDB locks the file per process. Measured on the real database (2026-10-14,
DuckDB 1.5):

- a process holding a read-write connection makes any other process fail to open
  the file, even read-only (`IOException: Could not set lock on file`);
- a process holding a read-only connection makes a writer fail the same way;
- several read-only processes coexist;
- connect read-only + one query + close takes about 3.4 ms.

So an API that keeps a connection open for its whole life would block the
pipeline for as long as it runs, and the pipeline would stop the API from
starting.

## Decision

- Every request opens its own read-only connection and closes it when the
  response is done (a FastAPI dependency, `get_con` in `api.py`).
- If the file is locked by a writer, the API answers `503` with `Retry-After: 5`
  instead of failing with a 500. A missing database is a `503` as well.
- Rankings read the existing `mart_*` views, so the API and the report show the
  same numbers; their parameters stay in `config/radar.yaml`.
- Everything is under `/v1`, read-only, JSON in English. FastAPI generates the
  OpenAPI schema and the docs at `/docs`.

## Alternatives considered

- **One long-lived connection**: no per-request cost, but it blocks ingest and
  build, as measured above.
- **Copy the file to a snapshot at startup** and serve the copy: no lock
  conflict, but the API serves stale data until restarted, and a second file has
  to be managed.
- **Postgres or MotherDuck**: real concurrent reads and writes, but a database
  server to run for ~18.5k rows, and the SQL views would need porting or a sync.

## Consequences

- About 3 ms of overhead per request, negligible next to the network.
- While ingest or build runs (a few seconds on the current data), the API returns
  503 with `Retry-After`. Making build write a new file and swap it in atomically
  would remove that window; not needed yet.
- The planned Cloud Run deployment ships the database file inside the image,
  where nothing writes to it. There, per-request connections cost little and
  guard nothing; they matter for the local setup, where API and pipeline share
  one file.
- The API depends on the views keeping their columns: a change to a `mart_*`
  view is now also an API change, covered by the API tests.
