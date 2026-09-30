# 0003 - Keyword classification with masking exclusions, measured on a labelled set

Status: Accepted (2026-09-29)

## Context

The first question WonBy answers is "who wins **data / analytics / BI** tenders".
The dataset has no category field, only a free-text `tender_description`. A naive
`\bdata\b` search matches ~200 tenders, many of them wrong: data-centre
facilities, SCADA ("Supervisory Control and Data Acquisition"), mobile data plans,
"analytical balances" (lab equipment). Excluding a whole tender whenever it
mentions "data centre" would also drop a tender for "data centre and data
analytics platform".

## Decision

- Classify per tender with case-insensitive regexes in
  `config/classification.yaml`: `include` and `exclude` lists, each pattern with a
  `why`.
- Exclusions **mask** the text they match; includes then run on what remains. A
  tender is "data" if at least one include still matches. Matched patterns are
  stored with the result for explainability.
- Quality is measured, not guessed: a hand-labelled set
  (`tests/fixtures/labeled_descriptions.csv`, 445 tenders on 2026-09-29) and
  `wonby eval` (precision, recall, F1, and the tenders that flip between two
  configs). `tests/test_classification_quality.py` enforces floors.
- Optimise recall first (a missed tender is a missed target firm), with precision
  kept above 0.85.

## Consequences

- 2026-09-29 baseline: precision 0.86, recall 0.93, 94 data tenders out of 12 052.
- Masking cannot veto a whole tender, so some false positives remain (training
  courses, room renovations named after a "Data Science" lab). A `reject` list
  would need an engine change.
- Extending to many themes (IT, consulting, construction…) with regexes will not
  scale; the labelled set and `eval` are the baseline an LLM classifier will have
  to beat (next ADR when that lands).
