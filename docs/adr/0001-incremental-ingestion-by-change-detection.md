# 0001 - Incremental ingestion by change detection and MERGE

Status: Accepted (2026-09-29)

## Context

The source is the data.gov.sg datastore API for "Government Procurement via
GeBIZ": ~18.5k rows, paginated with `limit`/`offset`. Profiling showed that:

- rows come back sorted by `tender_no`, not by award date, so a new award lands
  in the middle of the pagination, not at the end;
- `_id` is a row index that is reassigned when the dataset is republished;
- `(tender_no, supplier_name)` is unique across the whole dataset;
- the dataset is republished per fiscal year, and the metadata endpoint exposes
  `lastUpdatedAt`;
- bursts get HTTP 429 without a `Retry-After` header.

The classic "resume from the last offset" or "fetch rows with `_id` > max" would
silently miss inserted rows and mis-key republished ones.

## Decision

- Read `lastUpdatedAt`; if it equals the last successful run's value, log a
  `skipped` run and make no datastore call. `--full` forces a fetch but still
  records `lastUpdatedAt`, so the next plain run can skip.
- Otherwise fetch every page (2 requests at `limit=10000`), validate the whole
  fetch (stable `total` across pages, fetched = total, unique keys, expected
  fields), then apply it in **one transaction**: `MERGE` on
  `(tender_no, supplier_name)` with a hash of the payload fields, keep
  `first_seen_at`, maintain `last_seen_at` / `last_changed_at`, set `removed_at` on
  keys that disappeared and clear it when they come back.
- Refuse a run that would mark more than 20 % of the active rows as removed,
  unless `--accept-removals` is passed.
- Retry 429, 5xx and transport errors with exponential backoff and full jitter,
  honour `Retry-After` when present, fail fast on other 4xx.
- Every run, including failures, is logged in `ingest_runs`.

## Consequences

- Re-running ingest is always safe: on an unchanged source it changes nothing but
  the run log.
- A refresh costs 1 request when nothing changed, 3 when something did - cheap
  enough that "incremental" never needs to be smarter than a full MERGE.
- `first_seen_at` gives a reliable "new since" signal for reports, independent of
  award dates.
- If the dataset ever grows by orders of magnitude, a full fetch per change will
  need revisiting; at ~4 MB it is not a concern.
