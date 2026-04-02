#!/usr/bin/env bash
# One-command benchmark runner.
#
# Usage:
#   ./run-benchmark.sh --tool nctl claude --policy-id cp_require_labels
#   ./run-benchmark.sh --tool nctl --containerized
#   ./run-benchmark.sh --report
#
# What it does:
#   1. Checks dependencies (docker, kyverno, python3, go)
#   2. Downloads + caches OpenAPI schemas for the Go validator
#   3. Builds the Go validator binary if needed
#   4. Downloads nctl binary for Docker builds if needed
#   5. Builds Docker images if needed (only when --containerized is used)
#   6. Syncs upstream kyverno policies if dataset is empty
#   7. Runs benchmark.py with all passed flags
#
# Cached artifacts (gitignored, download once):
#   .cache/schemas/       — Kyverno OpenAPI v3 schemas
#   .cache/nctl/          — nctl Linux binary for Docker builds
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
CACHE_DIR="$REPO_ROOT/.cache"
GO_VALIDATOR="$REPO_ROOT/validate-policy"
SCHEMA_DIR="$REPO_ROOT/cmd/validate-policy/schemas/openapi/v3"

# nctl version detection (Homebrew formula is the public source for latest version)

# nctl download
NCTL_DOWNLOAD_BASE="https://dl.nirmata.io/nctl"
HOMEBREW_FORMULA_URL="https://raw.githubusercontent.com/nirmata/homebrew-tap/main/nctl.rb"
NCTL_CACHE="$CACHE_DIR/nctl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo "==> $*"; }
warn()  { echo "WARNING: $*" >&2; }
die()   { echo "ERROR: $*" >&2; exit 1; }

check_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but not found. $2"
}

has_flag() {
  local flag="$1"; shift
  for arg in "$@"; do
    [ "$arg" = "$flag" ] && return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# 1. Dependency checks
# ---------------------------------------------------------------------------

check_deps() {
  check_cmd python3 "Install Python 3.9+"
  check_cmd go "Install Go 1.25+ (needed to build the policy validator)"

  # Docker only needed for --containerized
  if has_flag "--containerized" "$@"; then
    check_cmd docker "Install Docker Desktop or OrbStack"
  fi

  # kyverno CLI optional but recommended
  if ! command -v kyverno >/dev/null 2>&1; then
    warn "kyverno CLI not found — functional tests will be skipped. Install: brew install kyverno"
  fi
}

# ---------------------------------------------------------------------------
# 2. Download + cache OpenAPI schemas
# ---------------------------------------------------------------------------

check_schemas() {
  # Schemas are committed in cmd/validate-policy/schemas/. Just verify they exist.
  if [ -f "$SCHEMA_DIR/apis/policies.kyverno.io/v1beta1.json" ]; then
    return 0
  fi
  die "OpenAPI schemas missing at $SCHEMA_DIR. They should be committed in the repo — run 'git checkout cmd/validate-policy/schemas/' to restore."
}

# ---------------------------------------------------------------------------
# 3. Build Go validator
# ---------------------------------------------------------------------------

build_go_validator() {
  if [ -f "$GO_VALIDATOR" ]; then
    return 0
  fi

  info "Building Go policy validator..."
  check_schemas
  (cd "$REPO_ROOT/cmd/validate-policy" && GOWORK=off go build -o "$GO_VALIDATOR" .) || die "Go validator build failed. Check Go installation and dependencies."
  info "Built: $GO_VALIDATOR"
}

# ---------------------------------------------------------------------------
# 4. Download nctl for Docker builds
# ---------------------------------------------------------------------------

fetch_nctl() {
  local nctl_bin="$REPO_ROOT/docker/nctl"
  if [ -f "$nctl_bin" ]; then
    return 0
  fi

  # Check cache first
  if [ -f "$NCTL_CACHE/nctl" ]; then
    info "Using cached nctl binary"
    cp "$NCTL_CACHE/nctl" "$nctl_bin"
    return 0
  fi

  info "Downloading nctl Linux binary for Docker builds..."
  mkdir -p "$NCTL_CACHE"

  # Detect target arch for Docker (arm64 on Apple Silicon, amd64 otherwise)
  local arch
  case "$(uname -m)" in
    aarch64|arm64) arch="arm64" ;;
    *)             arch="amd64" ;;
  esac

  # Get latest version from Homebrew formula (same source the install script uses)
  local version
  version=$(curl -fsSL "$HOMEBREW_FORMULA_URL" 2>/dev/null | grep -o 'version "[^"]*"' | head -1 | tr -d '"' | awk '{print $2}')
  if [ -z "$version" ]; then
    die "Failed to detect nctl version. Check $HOMEBREW_FORMULA_URL"
  fi

  # Download Linux binary directly (the install script would get the host OS)
  local url="https://dl.nirmata.io/nctl/v${version}/nctl_${version}_linux_${arch}.zip"
  local tmpdir
  tmpdir=$(mktemp -d)

  info "Fetching nctl v${version} linux/${arch} from $url"
  curl -fsSL "$url" -o "$tmpdir/nctl.zip" || die "Failed to download nctl from $url"
  unzip -q "$tmpdir/nctl.zip" -d "$tmpdir" || die "Failed to extract nctl"

  if [ -f "$tmpdir/nctl" ]; then
    chmod +x "$tmpdir/nctl"
    cp "$tmpdir/nctl" "$NCTL_CACHE/nctl"
    cp "$tmpdir/nctl" "$nctl_bin"
    info "nctl v${version} linux/${arch} cached"
  else
    rm -rf "$tmpdir"
    die "nctl binary not found in archive. Place manually at docker/nctl"
  fi
  rm -rf "$tmpdir"
}

# ---------------------------------------------------------------------------
# 5. Build Docker images
# ---------------------------------------------------------------------------

build_docker_images() {
  if ! echo "$@" | grep -q -- "--containerized"; then
    return 0
  fi

  local needs_build=false
  for img in benchmark-base benchmark-nctl benchmark-claude benchmark-cursor benchmark-codex; do
    if ! docker image inspect "$img" >/dev/null 2>&1; then
      needs_build=true
      break
    fi
  done

  if [ "$needs_build" = false ]; then
    return 0
  fi

  info "Building Docker images..."
  fetch_nctl

  (
    cd "$REPO_ROOT/docker"
    docker build -f Dockerfile.base -t benchmark-base . 2>&1 | tail -3
    docker build -f Dockerfile.nctl -t benchmark-nctl --build-arg NCTL_BIN=nctl . 2>&1 | tail -3
    docker build -f Dockerfile.claude -t benchmark-claude . 2>&1 | tail -3
    docker build -f Dockerfile.cursor -t benchmark-cursor . 2>&1 | tail -3
    docker build -f Dockerfile.codex -t benchmark-codex . 2>&1 | tail -3
  )
  info "Docker images built."
}

# ---------------------------------------------------------------------------
# 6. Sync dataset
# ---------------------------------------------------------------------------

sync_dataset() {
  local policy_dir="$REPO_ROOT/dataset/imported/kyverno-policies"
  if [ -d "$policy_dir" ] && [ "$(ls "$policy_dir"/*.yaml 2>/dev/null | wc -l)" -gt 0 ]; then
    return 0
  fi

  info "Syncing upstream kyverno policies..."
  python3 "$REPO_ROOT/scripts/sync_kyverno_policies.py"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  echo ""
  echo "  Policy Conversion Benchmark"
  echo "  ==========================="
  echo ""

  # If just --report, skip builds
  if [ "$#" -eq 1 ] && [ "$1" = "--report" ]; then
    info "[1/1] Generating report..."
    python3 "$REPO_ROOT/benchmark.py" --report
    return
  fi

  info "[1/6] Checking dependencies..."
  check_deps "$@"

  info "[2/6] Building Go policy validator..."
  build_go_validator

  info "[3/6] Syncing upstream kyverno policies..."
  sync_dataset

  info "[4/6] Building Docker images..."
  build_docker_images "$@"

  info "[5/6] Running benchmark..."
  python3 "$REPO_ROOT/benchmark.py" "$@"

  info "[6/6] Generating report..."
  python3 "$REPO_ROOT/benchmark.py" --report

  echo ""
  echo "  Done."
  echo "  Dashboard: reports/output/dashboard.html"
  echo "  Markdown:  reports/output/report.md"
  echo "  Results:   results/"
  echo ""
}

main "$@"
