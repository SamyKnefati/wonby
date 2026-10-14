# Architecture decision records

One file per significant decision, numbered in order, never rewritten: a decision
that changes gets a new ADR that supersedes the old one. Format: context, decision,
consequences (after Michael Nygard).

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-incremental-ingestion-by-change-detection.md) | Incremental ingestion = change detection + full fetch + MERGE on the natural key | Accepted |
| [0002](0002-duckdb-single-file-warehouse.md) | DuckDB single-file warehouse, no pandas | Accepted |
| [0003](0003-keyword-classification-with-masking.md) | Keyword classification with masking exclusions, measured on a labelled set | Accepted |
| [0004](0004-windows-anchored-on-data.md) | Time windows anchored on the latest award, not on today | Accepted |
| [0005](0005-rank-suppliers-by-tender-count.md) | Rank suppliers by number of tenders, flag shared and nominal amounts | Accepted |
