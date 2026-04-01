#!/bin/bash
# Entrypoint for Claude Code benchmark container.
# Args: $1 = conversion prompt
# Input: /workspace/policy.yaml (copied in by orchestrator)
# Output: agent writes to /workspace/output/converted.yaml
set -e

PROMPT="$1"
if [ -z "$PROMPT" ]; then
  echo "Usage: run-claude.sh <prompt>" >&2
  exit 1
fi

cd /workspace

exec claude -p "$PROMPT" \
  --dangerously-skip-permissions \
  --output-format json \
  --model "${CLAUDE_MODEL:-sonnet}"
