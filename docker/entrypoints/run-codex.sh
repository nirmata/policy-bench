#!/bin/bash
# Entrypoint for OpenAI Codex benchmark container.
# Args: $1 = conversion prompt
# Input: /workspace/policy.yaml (copied in by orchestrator)
# Output: agent writes to /workspace/output/converted.yaml
set -e

PROMPT="$1"
if [ -z "$PROMPT" ]; then
  echo "Usage: run-codex.sh <prompt>" >&2
  exit 1
fi

cd /workspace

exec codex exec \
  --full-auto \
  --sandbox danger-full-access \
  --skip-git-repo-check \
  --ephemeral \
  --model "${CODEX_MODEL:-gpt-5-codex}" \
  "$PROMPT"
