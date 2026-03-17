#!/usr/bin/env bash
# Run nctl AI conversion with full logging: nctl version + entire nctl ai output.
# Use the log to verify the conversion skill was loaded (e.g. "Reading file from .../converting-policies/SKILL.md").
set -e

INPUT="${1:-input/require-resource-limits.yaml}"
PROMPT="Convert the policy in ${INPUT} to a Kyverno ValidatingPolicy (Kyverno 1.16+) using CEL-based validation where appropriate. Write the converted policy to output/converted.yaml."

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
RESULTS_DIR="${REPO_ROOT}/results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="${RESULTS_DIR}/nctl_conversion_${TIMESTAMP}.log"

mkdir -p "$RESULTS_DIR"
mkdir -p "${REPO_ROOT}/output"

{
  echo "=== nctl version ==="
  nctl version 2>&1 || true
  echo ""
  echo "=== nctl ai (full output) ==="
  echo "Prompt: $PROMPT"
  echo ""
} | tee "$LOG_FILE"

echo "Running nctl ai (all output is also written to ${LOG_FILE})..."
# --skip-permission-checks: skip interactive prompts (e.g. "Does this capture the policy intent?") so conversion runs non-interactively
nctl ai --allowed-dirs "$REPO_ROOT" --prompt "$PROMPT" --skip-permission-checks 2>&1 | tee -a "$LOG_FILE"

echo ""
CONVERTING="Reading file from ~/.nirmata/nctl/skills/policy-skills/converting-policies/SKILL.md"
GENERATING="Reading file from ~/.nirmata/nctl/skills/policy-skills/generating-policies/SKILL.md"
AGENT_OK="✅ Agent completed successfully!"
if grep -qF "$CONVERTING" "$LOG_FILE" 2>/dev/null && grep -qF "$GENERATING" "$LOG_FILE" 2>/dev/null && grep -qF "$AGENT_OK" "$LOG_FILE" 2>/dev/null; then
  echo "Flow result: PASS (both skills loaded and agent completed successfully)"
else
  echo "Flow result: FAILED (one or both skills not loaded, or agent did not complete successfully)"
  exit 1
fi
echo "Log written to: ${LOG_FILE}"
