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

# No --output-format json: let the agent stream its activity (tool calls,
# reasoning, file writes) to stdout/stderr in real time so operators can
# monitor progress via `docker logs -f <container>`.  The ContainerRunner
# extracts the output file via `docker cp`, not from stdout.
exec claude -p "$PROMPT" \
  --dangerously-skip-permissions \
  --model "${CLAUDE_MODEL:-sonnet}"
