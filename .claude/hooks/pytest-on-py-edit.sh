#!/usr/bin/env bash
# PostToolUse hook (Edit|Write|MultiEdit): after a Python file of this project is
# edited, run the test suite. Exit 2 feeds the failure back to Claude; any other
# outcome stays silent.
set -u
project="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
file=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')

[[ "$file" == *.py && "$file" == "$project"/* ]] || exit 0
cd "$project" || exit 0

out=$(PYTHONDONTWRITEBYTECODE=1 uv run --quiet pytest -q -x -p no:cacheprovider 2>&1)
rc=$?
# 5 = no tests collected (e.g. fresh checkout)
[[ $rc -eq 0 || $rc -eq 5 ]] && exit 0

{ echo "pytest failed after editing ${file#"$project"/}:"; echo "$out" | tail -n 40; } >&2
exit 2
