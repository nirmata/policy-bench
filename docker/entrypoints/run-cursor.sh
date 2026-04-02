#!/bin/bash
# Entrypoint for Cursor agent benchmark container.
# Args: $1 = conversion prompt
# Input: /workspace/policy.yaml (copied in by orchestrator)
# Output: agent writes to /workspace/output/converted.yaml
#
# Wraps cursor-agent in timeout to handle known hanging bug.
set -e

PROMPT="$1"
if [ -z "$PROMPT" ]; then
  echo "Usage: run-cursor.sh <prompt>" >&2
  exit 1
fi

# Inner timeout for cursor's known hanging bug. The Python ContainerRunner
# applies an outer timeout on the docker process — keep this value lower so
# the graceful exit-124 path fires before the outer hard-kill.
TIMEOUT="${CURSOR_TIMEOUT:-240}"

# Add cursor-agent to PATH (installer puts it in ~/.local/bin/ or ~/.cursor/bin/)
export PATH="$HOME/.local/bin:$HOME/.cursor/bin:$PATH"

cd /workspace

timeout "$TIMEOUT" cursor-agent --api-key "$CURSOR_API_KEY" -p --force \
  --output-format json \
  --model "${CURSOR_MODEL:-claude-4.6-sonnet-medium}" \
  "$PROMPT" || {
  code=$?
  # Exit 124 = timeout killed it. Output may have been written before the hang.
  if [ "$code" -eq 124 ] && [ -f /workspace/output/converted.yaml ]; then
    echo '{"warning":"cursor-agent timed out but output was written"}' >&2
    exit 0
  fi
  exit "$code"
}
