#!/usr/bin/env bash
# Runner script for nctl AI benchmark.
# Usage: ./run_tool_nctl.sh <source-policy-path> <output-path> "<prompt>"
#   <source-policy-path> is "none" for generation tasks.
#   Exit 0 on success, 1 on failure.
#   The converted/generated policy must be written to <output-path>.
set -euo pipefail

SOURCE="$1"
OUTPUT="$2"
PROMPT="$3"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# Use NCTL_BIN env var if set, otherwise fall back to PATH
NCTL="${NCTL_BIN:-nctl}"

mkdir -p "$(dirname "$OUTPUT")"

"$NCTL" ai \
  --provider bedrock \
  --model us.anthropic.claude-sonnet-4-6 \
  --allowed-dirs "$REPO_ROOT" \
  --prompt "$PROMPT" \
  --skip-permission-checks 2>&1

if [ ! -f "$OUTPUT" ]; then
  echo "ERROR: nctl did not produce output at $OUTPUT" >&2
  exit 1
fi
