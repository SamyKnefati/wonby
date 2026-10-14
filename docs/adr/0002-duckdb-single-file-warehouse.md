# 0002 - DuckDB single-file warehouse, no pandas

Status: Accepted (2026-09-29)

## Context

The whole dataset is ~18.5k rows (~4 MB). The pipeline needs transactional
upserts, window functions for the analysis views, and CSV exports. It runs on a
laptop today and will later run as a scheduled job behind a small API.

## Decision

- Store everything in one DuckDB file (`data/wonby.duckdb`, overridable with
  `WONBY_DB`): raw, cleaned, classification, parameters, analysis views, run logs.
- Keep the tabular work in SQL; Python holds only the logic that is easier to test
  as pure functions (`clean.py`, `classify.py`), applied row by row and bulk-loaded
  back through a JSON staging file.
- No pandas; no DuckDB Python UDFs (they require numpy on the installed version).
- Analysis views live in `src/wonby/sql/mart_*.sql`, one view per file, executed
  in name order by `build`.

## Consequences

- Zero infrastructure: the database is a file, tests use a `tmp_path` copy.
- One writer at a time: DuckDB does not allow a writing process alongside other
  readers, so concurrent jobs must work on copies (as the agents do).
- Moving to a hosted service later means either shipping the file with the app
  (read-mostly workloads) or switching the storage layer (e.g. Postgres or
  MotherDuck); the SQL views keep the analysis portable.
