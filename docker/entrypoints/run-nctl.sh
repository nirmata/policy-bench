#!/bin/bash
# Entrypoint for nctl benchmark container.
# Args: $1 = conversion prompt
# Input: /workspace/policy.yaml (copied in by orchestrator)
# Output: agent writes to /workspace/output/converted.yaml
set -e

PROMPT="$1"
if [ -z "$PROMPT" ]; then
  echo "Usage: run-nctl.sh <prompt>" >&2
  exit 1
fi

# --force required for non-interactive mode inside containers
exec nctl ai \
  --prompt "$PROMPT" \
  --skip-permission-checks \
  --force \
  --allowed-dirs /workspace \
  --provider "${NCTL_PROVIDER:-bedrock}" \
  --model "${NCTL_MODEL:-us.anthropic.claude-sonnet-4-6}"
