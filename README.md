# WonBy

**Who wins Singapore public-sector tenders — and who will need to staff them.**

WonBy turns the official record of Singapore government contract awards into a
ranked view of the firms that keep winning, the agencies they work for, and what
they just won. It starts with **data / analytics / BI** tenders: which vendors win
them, how often, how recently. The intended users are recruiters, staffing
agencies, freelancers and vendors looking for the companies that are about to
need people and partners.

> Independent project, not affiliated with GeBIZ, the Ministry of Finance or the
> Government of Singapore.

## What it does

```
data.gov.sg API ──► ingest ──► raw_awards ──► build ──► stg_awards ──► mart_* views ──► report
 (GeBIZ awards)   incremental   (DuckDB)     clean +     typed,         top suppliers,    Markdown +
                  MERGE, retry              classify    normalised     agencies, targets  CSV exports
```

- **Incremental, idempotent ingestion** of ~18.5k award records: change detection
  on the dataset's `lastUpdatedAt`, full fetch, then a transactional `MERGE` on the
  natural key. Re-running on an unchanged source changes nothing.
- **Cleaning**: supplier names normalised across legal-suffix variants
  (`PTE. LTD.`, `PRIVATE LIMITED`…) and grouped by firm, day-first dates, decimal
  amounts, flags for amounts repeated across suppliers and nominal $1 panel seats.
- **Classification** of "data" tenders with configurable include/exclude regexes,
  measured against a hand-labelled set of 445 tenders
  (**precision 0.86, recall 0.93**).
- **Analysis views**: top data suppliers, top buying agencies, recent data awards,
  supplier × agency, and **priority targets** (firms winning data tenders
  repeatedly in the last 24 months).
- **Report**: Markdown summary with what's new since the previous report, plus one
  CSV per view.

## Engineering notes

A few decisions worth reading before touching the code (full reasoning in the
[architecture decision records](docs/adr/), details in [`CLAUDE.md`](CLAUDE.md) and
[`docs/dataset_profile.md`](docs/dataset_profile.md)):

- **No offset-based incremental.** The API sorts by tender number, not by date, so
  new awards land in the middle of the pagination; the row id is reassigned on
  republication. Incremental = metadata check + full fetch + MERGE.
- **All-or-nothing loads.** Everything is validated (stable total across pages,
  fetched = total, unique keys, schema drift) before one write transaction; a
  failed run leaves the data untouched and is logged. A guard blocks runs that
  would remove more than 20 % of the active rows.
- **Rate limits.** 429s arrive without `Retry-After`; retries use exponential
  backoff with jitter, honour `Retry-After` when present and fail fast on other 4xx.
- **Exclusions mask, they don't veto.** "data centre maintenance" is excluded, but
  "data centre and data analytics platform" stays in.
- **Windows are anchored on the data**, not on today: the source is published per
  fiscal year and lags the calendar by months.
- **Tests never touch the network**: `httpx.MockTransport`, and a `conftest.py`
  guard that fails any real HTTP request.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 if needed).

```bash
uv sync
uv run wonby ingest     # incremental load from data.gov.sg into data/wonby.duckdb
uv run wonby build      # clean, classify, rebuild the analysis views
uv run wonby report     # output/reports/radar_<date>.md + output/exports/*.csv
uv run pytest -q        # 123 tests, no network
```

| Command | What it does |
|---|---|
| `wonby ingest [--full] [--accept-removals]` | Skips if the source is unchanged; `--full` forces a fetch |
| `wonby build` | Rebuilds `stg_awards`, `tender_classification`, `radar_params` and the `mart_*` views |
| `wonby report` | CSV exports + Markdown report, with changes since the previous report |
| `wonby eval [--config P] [--against P]` | Classifier precision/recall on the labelled set; `--against` compares two configs |
| `wonby sample [--n N] [--seed S]` | Unlabelled tenders to label next |

Optional: `DATA_GOV_SG_API_KEY` raises the API rate limits; `WONBY_DB` overrides
the database path.

## Configuration

| File | Purpose |
|---|---|
| [`config/classification.yaml`](config/classification.yaml) | Include/exclude patterns for "data" tenders |
| [`config/radar.yaml`](config/radar.yaml) | Recent window, priority-target thresholds, ranking size |
| [`config/supplier_aliases.yaml`](config/supplier_aliases.yaml) | Manual merges of entities of the same firm |

## Built with Claude Code

The repo ships its own agent setup in [`.claude/`](.claude): five subagents
(dataset explorer, pipeline engineer, classifier tuner, market analyst, code
reviewer), slash commands (`/refresh`, `/radar`, `/tune`) and a hook that runs the
test suite after every Python edit. Open Claude Code in this directory to use them.

## Roadmap

- CI on GitHub Actions
- FastAPI service exposing the views
- Multi-theme classification (IT, consulting, construction…) with an LLM,
  evaluated against the keyword baseline
- Containerised deployment on Google Cloud Run
- Fresher data than the yearly open-data release

## License

Code: [MIT](LICENSE).

Data: contains information from "Government Procurement via GeBIZ" accessed on
29 September 2026 from [data.gov.sg](https://data.gov.sg), which is made available
under the terms of the
[Singapore Open Data Licence version 1.0](https://data.gov.sg/open-data-licence).
Test fixtures in `tests/fixtures/` contain extracts of this dataset; the MIT
licence covers the code only, not the data.
