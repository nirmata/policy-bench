#!/usr/bin/env bash
# Runner script for Cursor Agent CLI benchmark.
# Usage: ./run_tool_cursor.sh <source-policy-path> <output-path> "<prompt>"
#   <source-policy-path> is "none" for generation tasks.
#   Exit 0 on success, 1 on failure.
#   The converted/generated policy must be written to <output-path>.
#
# Requires: Cursor Team/Pro plan. Install CLI: curl https://cursor.com/install | bash
# Auth: agent login (one-time browser auth)
set -euo pipefail

SOURCE="$1"
OUTPUT="$2"
PROMPT="$3"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Use CURSOR_BIN env var if set, otherwise fall back to PATH
AGENT="${CURSOR_BIN:-agent}"

mkdir -p "$(dirname "$OUTPUT")"

"$AGENT" -p "$PROMPT" \
  --model claude-4.6-sonnet-medium \
  --force 2>&1

if [ ! -f "$OUTPUT" ]; then
  echo "ERROR: Cursor agent did not produce output at $OUTPUT" >&2
  exit 1
fi
