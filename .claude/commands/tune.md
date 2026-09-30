---
description: Run the classifier-tuner on a fresh sample and report precision/recall before vs after
argument-hint: "[sample size, default 60]"
allowed-tools: Bash(git diff:*), Bash(uv run wonby build:*)
---

Run one classification tuning pass.

1. Sample size: `$ARGUMENTS` if it is a number, otherwise 60. Seed: today's date
   as `YYYYMMDD`.
2. Delegate to the **classifier-tuner** subagent: "Run a tuning pass on a fresh
   sample of <size> tenders with seed <seed>. Follow your tuning loop and return
   your full report."
3. When it returns, show the user:
   - its metrics table (before → after) and the full-dataset effect;
   - `git diff --stat -- config/ tests/`;
   - the pattern changes with their reasons, and the borderline cases it flagged.
4. Don't commit. Ask whether to keep the pattern changes. If kept, run
   `uv run wonby build` so the views use the new classification. If not, revert
   only `config/classification.yaml` and `tests/test_classification_quality.py`
   (`git checkout -- <files>`) — keep the new labelled rows, they are ground
   truth either way.
