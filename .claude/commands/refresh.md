---
description: Incremental ingest from data.gov.sg, then rebuild stg, classification and analysis views
argument-hint: "[--full]"
allowed-tools: Bash(uv run wonby ingest:*), Bash(uv run wonby build:*), Bash(duckdb -readonly:*)
---

Refresh the GeBIZ radar database.

1. Run `uv run wonby ingest $ARGUMENTS` (arguments are passed through; `--full`
   forces a fetch even if the source's `lastUpdatedAt` is unchanged).
2. If ingest fails, stop — do not build. Show the error and diagnose it (rate
   limit exhausted, network, API schema drift, validation failure) with the next
   step to take.
3. Run `uv run wonby build`.
4. Reply with a short summary, from the command output and, if needed, read-only
   queries (`duckdb -readonly data/wonby.duckdb`; `describe` a table before
   querying it):
   - source `lastUpdatedAt` and whether the run fetched or skipped;
   - rows inserted / updated / removed;
   - data coverage (min → max `award_date`) and number of data tenders;
   - data awards first seen in this run, if any.
   Suggest `/radar` if there is anything new.
