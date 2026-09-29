#!/usr/bin/env bash
# PostToolUse hook (CodeIt PRD 16.3): lint the file Claude just edited.
# Never blocks the edit (it already happened). Problems go to Claude via exit 2 + stderr.
set -uo pipefail

file="$(jq -r '.tool_input.file_path // empty')"
case "$file" in
  *.ts|*.tsx|*.js|*.jsx|*.mjs|*.cjs) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
if output="$(npx --no-install eslint --fix "$file" 2>&1)"; then
  exit 0
fi
{
  echo "Lint problems in $file (auto-fixable ones were fixed):"
  printf '%s\n' "$output" | tail -n 30
} >&2
exit 2
