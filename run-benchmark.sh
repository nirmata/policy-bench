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
PYTHON_BIN="python3"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
  PYTHON_BIN="$REPO_ROOT/.venv/bin/python3"
fi

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

required_env_vars_for_tool() {
  local tool="$1"
  case "$tool" in
    nctl) echo "NIRMATA_TOKEN NIRMATA_URL" ;;
    claude) echo "ANTHROPIC_API_KEY" ;;
    cursor) echo "CURSOR_API_KEY" ;;
    *) echo "" ;;
  esac
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
    if docker image inspect benchmark-base >/dev/null 2>&1; then
      if ! docker run --rm --entrypoint sh benchmark-base -lc 'curl -I -m 8 https://google.com >/dev/null 2>&1' >/dev/null 2>&1; then
        die "Docker containers do not have outbound network access. Restart Docker Desktop and verify container egress with: docker run --rm --entrypoint sh benchmark-base -lc 'curl -I -m 8 https://google.com'"
      fi
    fi
  fi

  # kyverno CLI optional but recommended
  if ! command -v kyverno >/dev/null 2>&1; then
    warn "kyverno CLI not found — functional tests will be skipped. Install: brew install kyverno"
  fi

  # Containerized runs: verify credentials exist via env file or shell env vars.
  if has_flag "--containerized" "$@"; then
    local secrets_dir="$REPO_ROOT/docker/secrets"
    local tools_requested=() tool arg
    # Collect all values after --tool (stop at the next -- flag or end)
    local capture=false
    for arg in "$@"; do
      if [ "$arg" = "--tool" ]; then capture=true; continue; fi
      if [[ "$arg" == --* ]]; then capture=false; continue; fi
      $capture && tools_requested+=("$arg")
    done
    for tool in "${tools_requested[@]+"${tools_requested[@]}"}"; do
      local env_file="$secrets_dir/${tool}.env"
      local required_vars
      local missing_vars=()
      required_vars=$(required_env_vars_for_tool "$tool")
      if [ -n "$required_vars" ]; then
        local var_name
        for var_name in $required_vars; do
          if [ -f "$env_file" ] && grep -Eq "^[[:space:]]*${var_name}[[:space:]]*=.+" "$env_file"; then
            continue
          fi
          if [ -n "${!var_name:-}" ]; then
            continue
          fi
          missing_vars+=("$var_name")
        done
      fi

      if [ ${#missing_vars[@]} -gt 0 ]; then
        die "Missing credentials for --tool $tool (${missing_vars[*]}). Set them in your shell environment (see README § API Keys)."
      fi
    done
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

  # Get formula once and derive both version and linux URL from it.
  local formula version url
  formula=$(curl -fsSL "$HOMEBREW_FORMULA_URL" 2>/dev/null) || die "Failed to fetch formula from $HOMEBREW_FORMULA_URL"
  version=$(printf "%s" "$formula" | grep -o 'version "[^"]*"' | head -1 | tr -d '"' | awk '{print $2}')
  if [ -z "$version" ]; then
    die "Failed to detect nctl version. Check $HOMEBREW_FORMULA_URL"
  fi

  # Preferred: exact Linux URL from the formula (avoids path drift on dl.nirmata.io)
  url=$(printf "%s" "$formula" \
    | grep -Eo 'https://[^" ]*nctl_[0-9.]+_linux_(amd64|arm64)\.zip' \
    | grep "linux_${arch}\.zip" \
    | head -1 || true)

  # Fallbacks for older/newer publication layouts.
  if [ -z "$url" ]; then
    url="https://dl.nirmata.io/nctl/nctl_${version}/nctl_${version}_linux_${arch}.zip"
  fi
  local tmpdir
  tmpdir=$(mktemp -d)

  info "Fetching nctl v${version} linux/${arch} from $url"
  if ! curl -fsSL "$url" -o "$tmpdir/nctl.zip"; then
    # Last fallback kept for backward compatibility with legacy paths.
    local legacy_url="https://dl.nirmata.io/nctl/v${version}/nctl_${version}_linux_${arch}.zip"
    warn "Primary nctl URL failed, retrying legacy path: $legacy_url"
    curl -fsSL "$legacy_url" -o "$tmpdir/nctl.zip" || die "Failed to download nctl from $url and $legacy_url"
  fi
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
  "$PYTHON_BIN" "$REPO_ROOT/scripts/sync_kyverno_policies.py"
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
    "$PYTHON_BIN" "$REPO_ROOT/benchmark.py" --report
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
  "$PYTHON_BIN" "$REPO_ROOT/benchmark.py" "$@"

  info "[6/6] Generating report..."
  "$PYTHON_BIN" "$REPO_ROOT/benchmark.py" --report

  echo ""
  echo "  Done."
  echo "  Dashboard: reports/output/dashboard.html"
  echo "  Markdown:  reports/output/report.md"
  echo "  Results:   results/"
  echo ""
}

main "$@"
