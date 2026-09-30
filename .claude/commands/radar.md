---
description: Produce the up-to-date wonby report (CSV exports + Markdown) with what's new since the previous report
allowed-tools: Bash(uv run wonby build:*), Bash(uv run wonby report:*), Bash(duckdb -readonly:*), Read
---

Produce the current GeBIZ wonby report.

1. If `data/wonby.duckdb` doesn't exist, stop and tell the user to run `/refresh`.
2. Run `uv run wonby build` (cheap; makes the views reflect the current config),
   then `uv run wonby report`.
3. Read the Markdown report whose path the command printed.
4. Reply in French, briefly:
   - report path and data coverage (anchor date, source last update); if the
     last successful ingest is more than 30 days old, suggest `/refresh`;
   - **Nouveautés**: what's new since the previous report (or say it's the first
     report / nothing new);
   - **Top 5 cibles prioritaires**, one line each with a pitch angle: which
     agencies they deliver for, what kind of data work, how recent.
   Don't paste the whole report.
