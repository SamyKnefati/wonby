# 0004 - Time windows anchored on the latest award, not on today

Status: Accepted (2026-09-29)

## Context

"Recent awards" and "firms winning regularly" need time windows. The open dataset
is published per fiscal year: on 2026-09-29 its coverage ended on 2026-03-31 and
its last update was 2026-04-26. A window counted back from `current_date` would
be empty or misleading for most of the year.

## Decision

- Every window is counted back from `max(award_date)` in `stg_awards` (the
  "anchor date"), never from `current_date`.
- Window lengths and thresholds are parameters in `config/radar.yaml`, loaded into
  the single-row `radar_params` table; SQL views read them from there, with no
  magic numbers.
- Reports print the anchor date and the source's last update, so readers know how
  fresh "recent" is.

## Consequences

- Reports are deterministic for a given data state and meaningful all year round.
- The priority-target window had to be widened from 12 to 24 months: the data
  market is thin (11 to 28 data tenders per fiscal year, a firm rarely wins more
  than one a year), and 12 months produced no target at all.
- Freshness is bounded by the yearly release; closing that gap needs another
  source (GeBIZ feeds), not a different window.
