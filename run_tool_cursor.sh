#!/usr/bin/env bash
# Runner script for Cursor Agent CLI benchmark.
# Usage: ./run_tool_cursor.sh <source-policy-path> <output-path> "<prompt>"
#   <source-policy-path> is "none" for generation tasks.
#   Exit 0 on success, 1 on failure.
#   The converted/generated policy must be written to <output-path>.
#   For directory-output tasks (e.g. generate_test, generate_chainsaw_test),
#   BENCH_OUTPUT_KIND=dir is set and OUTPUT is a directory that already
#   contains policy.yaml; the tool should write the expected artifact there.
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

if [ "${BENCH_OUTPUT_KIND:-file}" = "dir" ]; then
  mkdir -p "$OUTPUT"
else
  mkdir -p "$(dirname "$OUTPUT")"
fi

"$AGENT" -p "$PROMPT" \
  --model claude-4.6-sonnet-medium \
  --force 2>&1

if [ "${BENCH_OUTPUT_KIND:-file}" = "dir" ]; then
  EXPECTED_ARTIFACT="${BENCH_OUTPUT_ARTIFACT:-kyverno-test.yaml}"
  if [ ! -f "${OUTPUT}/${EXPECTED_ARTIFACT}" ]; then
    echo "ERROR: Cursor agent did not produce ${EXPECTED_ARTIFACT} in $OUTPUT" >&2
    exit 1
  fi
else
  if [ ! -f "$OUTPUT" ]; then
    echo "ERROR: Cursor agent did not produce output at $OUTPUT" >&2
    exit 1
  fi
fi
