# 0005 - Rank suppliers by number of tenders, flag shared and nominal amounts

Status: Accepted (2026-09-29)

## Context

One row is one `(tender, supplier)` pair. Profiling showed that amounts are not
additive per supplier:

- on multi-supplier tenders the amount is often the tender total repeated on every
  supplier row (250 of 1 592 "Awarded by Items" tenders);
- panels carry nominal amounts: 1 294 rows at exactly $1 across 282 tenders;
- "Awarded to No Suppliers" rows carry supplier `Unknown` and amount 0.

Summing `awarded_amt` per supplier would inflate the firms that sit on panels and
framework agreements.

## Decision

- `stg_awards` flags `amt_shared_across_suppliers` (more than one supplier row and
  a single distinct amount), `is_nominal_amt` (amount = 1) and `is_no_supplier`.
- Supplier rankings lead with the **number of distinct tenders**; they show both
  the total amount and the total excluding shared and nominal rows. No-supplier
  rows never appear in supplier rankings.
- Agency totals count a shared tender amount once.
- Suppliers are grouped by `supplier_group` (name without legal suffix or country
  marker, then manual aliases in `config/supplier_aliases.yaml`), so two entities
  of one firm rank together.

## Consequences

- Rankings reflect how often a firm wins, which is the prospecting signal, rather
  than how large a framework it sits on.
- Amount columns stay available but need the "excluding shared" reading.
- Supplier grouping still needs occasional manual aliases (e.g. Accenture, KPMG,
  NTT DATA entities); the review of `mart_top_data_suppliers` is where they surface.
