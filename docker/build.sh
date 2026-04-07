#!/bin/bash
# Build all benchmark container images.
#
# Usage:
#   ./build.sh                           # build all (nctl binary must be at ./nctl)
#   ./build.sh --nctl-bin /path/to/nctl  # specify nctl binary path
#   ./build.sh --only nctl               # build base + one tool only
set -e

cd "$(dirname "$0")"

NCTL_BIN="nctl"
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nctl-bin) NCTL_BIN="$2"; shift 2 ;;
    --only)     ONLY="$2"; shift 2 ;;
    *)          echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo "==> Building benchmark-base..."
docker build -f Dockerfile.base -t benchmark-base .

build_tool() {
  local tool="$1"
  shift
  echo "==> Building benchmark-${tool}..."
  docker build -f "Dockerfile.${tool}" -t "benchmark-${tool}" "$@" .
}

if [ -z "$ONLY" ] || [ "$ONLY" = "nctl" ]; then
  build_tool nctl --build-arg "NCTL_BIN=${NCTL_BIN}"
fi

if [ -z "$ONLY" ] || [ "$ONLY" = "claude" ]; then
  build_tool claude
fi

if [ -z "$ONLY" ] || [ "$ONLY" = "cursor" ]; then
  build_tool cursor
fi

if [ -z "$ONLY" ] || [ "$ONLY" = "codex" ]; then
  build_tool codex
fi

echo ""
echo "Done. Images:"
docker images --format '  {{.Repository}}:{{.Tag}}  {{.Size}}' | grep benchmark
