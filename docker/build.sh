#!/bin/bash
# Build all benchmark container images.
#
# For the public benchmark, the nctl binary is fetched from the pinned
# GitHub release (see NCTL_VERSION below). Bumping this pin is how the
# benchmark adopts new upstream nctl work — do it as a small PR so the
# reproducibility history stays traceable.
#
# Usage:
#   ./build.sh                              # download pinned nctl release, build all
#   ./build.sh --only nctl                  # download pinned nctl release, build nctl only
#   ./build.sh --nctl-version v4.10.15      # override pinned release (for a bump PR)
#   ./build.sh --nctl-bin /path/to/nctl     # use a locally-built binary (internal dev only;
#                                           # bypasses the release download so you can test
#                                           # unmerged branches. NOT for public repro runs.)
set -e

cd "$(dirname "$0")"

# Pinned nctl release for reproducible benchmarks. Bump via PR.
# See https://github.com/nirmata/go-nctl/releases for available versions.
NCTL_VERSION="${NCTL_VERSION:-v4.10.14}"
NCTL_BIN=""
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nctl-bin)     NCTL_BIN="$2"; shift 2 ;;
    --nctl-version) NCTL_VERSION="$2"; shift 2 ;;
    --only)         ONLY="$2"; shift 2 ;;
    *)              echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# Resolve which nctl binary to bake into the image.
# Precedence:
#   1. --nctl-bin <path>  (internal dev: local build from any branch/SHA)
#   2. Pre-existing ./nctl next to this script (manual override)
#   3. Download pinned release NCTL_VERSION from GitHub Releases (public path)
if [[ -n "$NCTL_BIN" ]]; then
  if [[ ! -f "$NCTL_BIN" ]]; then
    echo "ERROR: --nctl-bin path does not exist: $NCTL_BIN" >&2
    exit 1
  fi
  # Dockerfile.nctl uses NCTL_BIN as a path relative to this build context,
  # so normalize by copying into ./nctl.
  if [[ "$(cd "$(dirname "$NCTL_BIN")" && pwd)/$(basename "$NCTL_BIN")" != "$(pwd)/nctl" ]]; then
    cp "$NCTL_BIN" ./nctl
  fi
  echo "==> Using locally-supplied nctl binary (--nctl-bin) — internal dev mode"
  echo "    Public reproducibility requires the pinned release; use --nctl-version or no flag for that."
  NCTL_BIN="nctl"
elif [[ -f ./nctl ]]; then
  echo "==> Using existing ./nctl (delete it to force re-download of ${NCTL_VERSION})"
  NCTL_BIN="nctl"
else
  case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=amd64 ;;
    *) echo "ERROR: unsupported host arch $(uname -m)" >&2; exit 1 ;;
  esac
  V="${NCTL_VERSION#v}"
  ASSET="nctl_${V}_linux_${ARCH}.zip"
  echo "==> Downloading nctl ${NCTL_VERSION} (${ASSET}) via gh CLI"
  if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: 'gh' CLI not found on PATH. The nctl release assets live in a" >&2
    echo "private GitHub repo (nirmata/go-nctl), so 'gh auth login' is required" >&2
    echo "to fetch them. Install: https://cli.github.com/  OR pass --nctl-bin" >&2
    echo "to use a locally-built binary." >&2
    exit 1
  fi
  TMPDIR_LOCAL="$(mktemp -d -t nctl-release-XXXXXX)"
  trap 'rm -rf "${TMPDIR_LOCAL}"' EXIT
  gh release download "${NCTL_VERSION}" \
    --repo nirmata/go-nctl \
    --pattern "${ASSET}" \
    --dir "${TMPDIR_LOCAL}" \
    --clobber
  unzip -o -j "${TMPDIR_LOCAL}/${ASSET}" nctl -d .
  chmod +x ./nctl
  echo "    saved to $(pwd)/nctl"
  NCTL_BIN="nctl"
fi

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
