# CLAUDE.md — wonby

## Goal

WonBy shows **who wins Singapore public-sector tenders**, for which agencies, how
often and how recently - starting with **data / analytics / BI** tenders. Target
users: recruiters and staffing agencies, freelancers and vendors who want to know
which firms just won work and will need to staff it. The key output is the
"Cibles prioritaires" list in the Markdown report.

Source: MOF "Government Procurement via GeBIZ" on data.gov.sg — awarded tenders
since FY2021. Dataset id `d_acde1106003906a75c3fa052592f2fcb`.
Field-level facts (formats, distributions, anomalies): `docs/dataset_profile.md`.

## Golden rule

**Never assume a column name.** Before writing SQL or code that references a
column, verify it in DuckDB — `duckdb -readonly data/wonby.duckdb -c "describe <table>"` —
or, for API fields, in `docs/dataset_profile.md`. This applies to every table in
this file too: the data model below is the intended contract, the database is the
truth.

## Stack

Python ≥3.11 (pinned to 3.13 via `.python-version`), uv, DuckDB, httpx, PyYAML,
pytest. No pandas: DuckDB does the tabular work. No extra HTTP-mocking library:
tests use `httpx.MockTransport`.

## Commands

```bash
uv sync                                  # install
uv run wonby ingest [--full] [--accept-removals]   # incremental load (--full: fetch even if lastUpdatedAt is unchanged;
                                         #   --accept-removals: allow a run that removes >20% of active rows)
uv run wonby build                       # rebuild stg_awards, tender_classification, radar_params, mart_* views
uv run wonby report                      # CSV exports + Markdown report in output/
uv run wonby eval [--config P] [--against P]   # classifier precision/recall on the labelled set
uv run wonby sample [--n N] [--seed S]   # fresh tenders to label (excludes already-labelled ones)
uv run pytest -q                         # tests (no network)
uv run ruff check && uv run ruff format --check   # lint + formatting (CI runs both)
```

Paths default to the project root (`data/wonby.duckdb`, `config/`, `output/`);
`WONBY_DB` overrides the database path. Optional `DATA_GOV_SG_API_KEY` is sent as
the `x-api-key` header for higher API limits.

## Layout

```
config/classification.yaml   include/exclude regexes for "data" tenders (owned by classifier-tuner)
config/radar.yaml            analysis parameters: windows, thresholds, top_n (owned by market-analyst)
config/supplier_aliases.yaml manual supplier_group merges, e.g. two entities of one firm
src/wonby/
  api.py        data.gov.sg client: metadata, pagination, retries/backoff — I/O only
  ingest.py     incremental MERGE into raw_awards + ingest_runs
  clean.py      pure functions: supplier normalisation, amounts, dates, fiscal year
  classify.py   pure classification engine (config → is_data + matched patterns) + eval/sample helpers
  build.py      rebuilds stg_awards, tender_classification, radar_params, then runs sql/*.sql
  report.py     CSV exports + Markdown report + report_runs
  cli.py        argparse entry point `wonby`
  sql/          one analysis view per file, file name = view name (mart_*.sql)
tests/          pytest; fixtures/ holds API samples and the labelled set
docs/           dataset_profile.md; adr/ = architecture decision records (add one per significant decision)
data/, output/  generated, gitignored
```

## Data model (DuckDB, `data/wonby.duckdb`)

| Table / view | Built by | Content |
|---|---|---|
| `raw_awards` | ingest | API fields **as returned** (all VARCHAR) + `source_row_id` (API `_id`), `row_hash`, `first_seen_at`, `last_seen_at`, `last_changed_at`, `removed_at`. Primary key `(tender_no, supplier_name)` |
| `ingest_runs` | ingest | one row per run: status (`success`/`skipped`/`failed`), source `lastUpdatedAt`, API total, fetched/inserted/updated/removed counts, error |
| `stg_awards` | build | active raw rows (`removed_at is null`), typed and normalised — see business rules |
| `tender_classification` | build | one row per `tender_no`: `is_data`, `matched_includes`, `matched_excludes`, `config_hash` |
| `radar_params` | build | single row, one column per key of `config/radar.yaml` |
| `mart_*` | build (sql/) | analysis views, read by report and exports |
| `report_runs` | report | one row per report: generated_at, data watermark, file path |

## Ingestion semantics — read before touching api.py / ingest.py

- **No offset-based incremental.** The API returns rows sorted by `tender_no`,
  not by date, so new awards land in the middle of the pagination; and `_id` is a
  row index that is reassigned when the dataset is republished. It is not a key.
- **Incremental = change detection + full MERGE.** Read `lastUpdatedAt` from the
  metadata endpoint. If it equals the last successful run's value (and no
  `--full`), record a `skipped` run and make no datastore call. Otherwise fetch
  every page and MERGE on `(tender_no, supplier_name)`: insert new keys, update rows
  whose `row_hash` changed (keep `first_seen_at`), set `removed_at` on keys absent
  from the fetch and clear it on keys that come back. The whole dataset is ~18.5k
  rows = 2 pages of 10 000.
- **All-or-nothing.** Fetch everything first, validate (the API `total` is stable
  across pages, fetched count == total, no duplicate keys), then apply in one
  transaction. Any failure → `failed` run row, raw tables untouched.
- **Idempotent.** Running ingest twice on an unchanged source changes nothing
  except the `ingest_runs` log.
- **Rate limit.** data.gov.sg answers bursts with HTTP 429 and no `Retry-After`
  header (recovery observed within seconds). Retry 429, 5xx and transport
  errors/timeouts with exponential backoff and jitter, honour `Retry-After` if it
  ever appears, and fail fast on other 4xx. Sleep is injectable so tests don't
  wait.
- Endpoints: datastore `https://data.gov.sg/api/action/datastore_search?resource_id=<id>&limit=&offset=`,
  metadata `https://api-production.data.gov.sg/v2/public/api/datasets/<id>/metadata`
  (`data.lastUpdatedAt`).

## Business rules

- **Dates**: `award_date` is day-first `D/M/YYYY` without zero padding
  (`6/9/2021` = 6 September 2021). Singapore fiscal year runs 1 Apr → 31 Mar and
  is named by its starting year (FY2021 = Apr 2021 → Mar 2022).
- **Amounts**: `awarded_amt` arrives as a string → `DECIMAL(18,2)`. For
  multi-supplier tenders the amount is often the tender total repeated on every
  supplier row, and panels carry nominal amounts ($1). Hence
  `amt_shared_across_suppliers` (tender has >1 supplier row and a single distinct
  amount) and `is_nominal_amt` (amount = 1: 1 294 rows on 282 panel tenders — a
  real panel seat, but no money signal). Supplier rankings lead with the **number of distinct tenders**, and show
  both the total and the total excluding shared amounts. Agency totals count a
  tender's amount once when it is shared.
- **No-supplier rows**: status `Awarded to No Suppliers` comes with supplier
  `Unknown` and amount 0 → `is_no_supplier`, excluded from every supplier ranking.
- **Supplier names**, two levels:
  - `supplier_name_norm` — upper case, `.`/`,` removed, whitespace collapsed,
    legal suffix canonicalised (`PTE. LTD.`, `PTE LTD.`, `PRIVATE LIMITED`,
    `PTE. LIMITED` → `PTE LTD`; `SDN. BHD.` → `SDN BHD`; `LIMITED`/`LTD.` → `LTD`).
  - `supplier_group` — norm name without legal suffix or country marker
    (`(S)`, `(SINGAPORE)`), then mapped through `config/supplier_aliases.yaml`.
    This is the prospecting key: all rankings group by it.
- **"Data" tenders** are classified per tender on `tender_description` with
  `config/classification.yaml`. Exclude patterns **mask** the text they match
  (they don't reject the tender), then include patterns run on what's left — so
  "data centre maintenance" is out but "data centre and data analytics platform"
  stays in. In scope: data platforms/warehouses/lakes, ETL/integration, data
  management/governance/quality, analytics, BI/dashboards/reporting systems,
  data science, AI/ML model work, data consultancy/audit. Out of scope: physical
  infrastructure (data centres, SCADA, cabling), telecoms/mobile data, buying data
  or database subscriptions, lab "analytical" equipment.
  State on 2026-09-29: 445 labelled tenders, precision 0.86 / recall 0.93, 94 data
  tenders. Known false-positive classes the engine can't fix yet (masking can't
  veto a whole tender): training courses and lab/room renovations whose title
  contains "data science"/"machine learning".
- **"Recent" is anchored on the data, not on today.** The dataset is published by
  fiscal year (coverage currently ends 31 Mar 2026), so windows are counted back
  from `max(award_date)` in `stg_awards`. Window lengths live in `config/radar.yaml`.
- **"Nouveautés"** in a report = data awards whose `first_seen_at` is later than the
  data watermark of the latest *previous* report with an older watermark — so
  regenerating a report on unchanged data gives the same result.

## Conventions

- I/O stays in `api.py`, `ingest.py`, `build.py`, `report.py`, `cli.py`;
  `clean.py` and `classify.py` are pure and fully unit-tested.
- Type hints everywhere, `logging` for diagnostics, `print` only for CLI output.
- SQL: one view per `sql/mart_*.sql` file; views read only `stg_awards`,
  `tender_classification`, `radar_params` and other marts.
- Tests never hit the network (`httpx.MockTransport`; `tests/conftest.py` makes any
  real HTTP request fail) and use a `tmp_path`
  DuckDB. Every bug fix gets a regression test.
- A change to `config/classification.yaml` comes with `wonby eval` numbers before
  and after, and new labelled rows in `tests/fixtures/labeled_descriptions.csv`
  for each false positive/negative it fixes.
- A PostToolUse hook runs `uv run pytest -q -x` after each edit of a `.py` file.
- Work goes through small pull requests, never direct pushes to `main`: CI
  (`.github/workflows/ci.yml` - ruff + pytest on Python 3.11 and 3.13) must be
  green before merging.

## Subagents (`.claude/agents/`) and commands (`.claude/commands/`)

| Agent | Model | Delegate when |
|---|---|---|
| `dataset-explorer` | sonnet | profiling / "what does the data look like" — read-only |
| `pipeline-engineer` | sonnet | api, ingest, clean, classify engine, build, cli code + tests |
| `classifier-tuner` | sonnet | classification quality: label samples, adjust keywords, measure |
| `market-analyst` | sonnet | mart views, exports, report, "Cibles prioritaires" |
| `code-reviewer` | sonnet | after each significant step — read-only review |

Workflow: explorer → pipeline-engineer → code-reviewer → classifier-tuner and
market-analyst (in parallel, disjoint files) → code-reviewer.

Commands: `/refresh` (ingest + build), `/radar` (report + what's new),
`/tune [n]` (tuning pass on a fresh sample).
