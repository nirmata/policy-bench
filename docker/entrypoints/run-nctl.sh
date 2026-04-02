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
CMD=(nctl ai
  --prompt "$PROMPT"
  --skip-permission-checks
  --force
  --allowed-dirs /workspace
  --provider "${NCTL_PROVIDER:-nirmata}"
)
# Only pass --model if explicitly set (default is provider-specific)
if [ -n "${NCTL_MODEL:-}" ]; then
  CMD+=(--model "$NCTL_MODEL")
fi
exec "${CMD[@]}"
