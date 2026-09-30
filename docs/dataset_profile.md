# Dataset profile — GeBIZ awards (data.gov.sg)

Profiled on 2026-09-29 from a full API dump (18 464 records) loaded in DuckDB.
Every number below was recomputed by a query; re-profile after a source update
(`dataset-explorer`).

## Source

- Dataset `d_acde1106003906a75c3fa052592f2fcb`, managed by Ministry of Finance.
- Metadata: coverage 2021-04-01 → 2026-03-31, `lastUpdatedAt` 2026-04-26T21:48:27+08:00,
  ~4.4 MB CSV. Published by fiscal year.
- Datastore API returns rows sorted by `tender_no`; `limit=10000` is accepted
  (full load = 2 pages). Bursts get HTTP 429 without `Retry-After`; recovery
  within seconds.

## Schema (as returned by the API)

| Field | API type | JSON value | Nulls/empty | Distinct | Notes |
|---|---|---|---|---|---|
| `_id` | int4 | int | 0 | 18 464 | Row index 1…n in `tender_no` order — not a stable key |
| `tender_no` | text | string | 0 | 12 052 | e.g. `ACR000ETT21000001`; prefix = agency code but **not 1:1** (11 of 90 prefixes span several agencies: PMO, IND, MOM, FIN…) |
| `tender_description` | text | string | 0 | — | 13–500 chars (median 109); 5 rows at exactly 500 (truncated); 6 866 all-caps; ~494 start with a reference code (`RO21/MS/0012 …`) |
| `agency` | text | string | 0 | 113 | One agency per tender |
| `award_date` | text | string | 0 | 1 443 | `D/M/YYYY`, day-first, no zero padding (11 684 values have day > 12). 2021-04-01 → 2026-03-31 |
| `tender_detail_status` | text | string | 0 | 4 | see below |
| `supplier_name` | text | string | 0 | 6 152 | Mixed case; `Unknown` = no supplier |
| `awarded_amt` | numeric | **string** | 0 | — | All parse as decimal; 3 254 carry decimals |

## Grain

One row per `(tender_no, supplier_name)` — unique on the full dataset (18 464 rows,
12 052 tenders). A tender has exactly one description and one agency.

| Fiscal year | Rows | Tenders |
|---|---|---|
| FY2021 | 3 951 | 2 475 |
| FY2022 | 3 484 | 2 332 |
| FY2023 | 3 707 | 2 358 |
| FY2024 | 3 531 | 2 480 |
| FY2025 | 3 791 | 2 407 |

## Status and amounts

| `tender_detail_status` | Rows | Median amount |
|---|---|---|
| Awarded to Suppliers | 9 294 | 474 878 |
| Awarded by Items | 7 845 | 16 341 |
| Award by interface record | 686 | 767 250 |
| Awarded to No Suppliers | 639 | 0 — always supplier `Unknown`, amount 0 |

- Amount distribution: min 0, median 178 450, p99 ≈ 175 M, max ≈ 1.81 B.
- 643 rows at 0 (639 are no-supplier rows).
- **1 294 rows at exactly 1** across 282 tenders: nominal amounts on panels /
  period contracts (e.g. BCA000ETT26000003: 15 individuals at $1).
- Multi-supplier tenders: 1 592 "Awarded by Items" (250 repeat one identical
  amount on every supplier row) and 77 "Award by interface record" (1 repeats).
  Example CCY000ETT21000005: 4 audit firms at 1 127 100 each — the tender total,
  repeated. Summing per supplier over-counts.

## Supplier names

- 6 152 raw names; only 6 148 after upper-case + removing `.`/`,` + collapsing
  whitespace — punctuation/case variants are rare. Suffix variants are the real
  issue.
- Trailing legal suffix (rows): `PTE. LTD.` 10 120, `PTE LTD` 3 159, none 2 828,
  `PRIVATE LIMITED` 513, `LLP` 492, `LIMITED` 471, `LTD.` 319, `LTD` 240,
  `PTE. LIMITED` 118, `CORPORATION` 61, `INC.` 60, `PTE LTD.` 37, plus `SDN BHD`.
- Same firm, different entities: `ANALYSYS MASON LIMITED` / `ANALYSYS MASON PTE. LIMITED`;
  `HUAY ARCHITECTS` / `HUAY ARCHITECTS PTE. LTD.`.
- ~122 names carry a country marker: `(S)`, `(SINGAPORE)`, `SINGAPORE`, `(SG)`.
- Individuals appear as suppliers (panels of assessors, e.g. BCA).

## Keyword landscape (case-insensitive regex on `tender_description`)

| Regex | Rows | Tenders | Signal |
|---|---|---|---|
| `\bdata\b` | 257 | 198 | Noisy: SCADA, mobile data, AV "data & recording", data purchases |
| `data\s*cent(re|er)` | 55 | 54 | Facilities (CRAC/UPS, fit-out, maintenance) — exclude |
| `analytic` | 107 | 75 | Mostly relevant; "analytical balances/instruments" = lab kit |
| `business intelligence|\bBI\b` | 10 | 6 | Relevant |
| `database` | 40 | 36 | Mostly subscriptions (news, screening, compliance) — weak |
| `dashboard` | 5 | 5 | Relevant |
| `machine learning|artificial intelligence|\bAI\b` | 45 | 41 | Mixed |
| `data\s+(platform|warehouse|lake)|lakehouse` | 7 | 7 | Relevant |
| `data scien` | 17 | 6 | Relevant (incl. a central data-science services contract) |
| `\betl\b|data integration|data pipeline` | 0 | 0 | — |
| `data (governance|management|quality)` | 4 | 4 | Relevant |
| `statistic` | 12 | 8 | Mixed |
| `survey` | 277 | 238 | Mostly quantity/land/site surveying — not data work |
| `visuali[sz]` | 20 | 7 | Mixed |
| `big data` | 0 | 0 | — |
| `scada|supervisory control and data acquisition` | 10 | 10 | Industrial control — exclude |
| `mobile data` | 12 | 10 | Telecom — exclude |

Examples — true positives: "Unified Data Analytics Platform (UDAP)", "Data Science
software and services for government ministries…", "Data Platform System with
option for maintenance", "Linked Data Management System and Discovery Interface",
"Data auditing services", "Singapore Tourism Analytics Network enhancement".
False positives: "Supervisory Control and Data Acquisition System", "mobile data
services", "Audio Visual … Data & Recording System", "Water Level Data at
Waterways", "Payment Transaction Data", "analytical balances", "News and
Information Database".
