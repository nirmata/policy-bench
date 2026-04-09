# Contributing Guide

This guide covers how to add test cases, run the validation suite locally, interpret results, and meet the minimum bar before opening a PR.

For a deep dive into how each validation layer works, see [`docs/testing.md`](docs/testing.md).

## Table of Contents

- [Environment Setup](#environment-setup)
- [Test Architecture](#test-architecture)
- [Integration Tests](#integration-tests)
- [Test Data Conventions](#test-data-conventions)
- [Adding a New Test Case](#adding-a-new-test-case)
- [Stress Tests](#stress-tests)
- [Adding a New Tool](#adding-a-new-tool)
- [Local Test Commands](#local-test-commands)
- [Scoping Benchmark Runs](#scoping-benchmark-runs)
- [CI-Equivalent Commands](#ci-equivalent-commands)
- [Updating the Leaderboard](#updating-the-leaderboard)
- [Failure Triage Workflow](#failure-triage-workflow)
- [Minimum Checks Before PR](#minimum-checks-before-pr)

---

## Environment Setup

### Requirements

| Tool | Minimum version | Purpose |
|---|---|---|
| Python | 3.9 | Orchestration, evaluation, reporting |
| Go | 1.25 | Schema + CEL validator binary |
| Docker | Any recent | Containerized tool runs (`--containerized`) |
| Kyverno CLI | Latest | Functional tests (`kyverno test`) |

### One-time setup

```bash
# 1. Python dependencies
pip install -r requirements.txt

# 2. Go validator binary (required for all validation)
cd cmd/validate-policy && GOWORK=off go build -o ../../validate-policy .
cd ../..

# 3. Kyverno CLI — needed for functional (semantic) tests
# macOS
brew install kyverno
# Linux
curl -LO "https://github.com/kyverno/kyverno/releases/latest/download/kyverno_linux_amd64.tar.gz"
tar -xvf kyverno_linux_amd64.tar.gz && chmod +x kyverno && mv kyverno /usr/local/bin/

# 4. Sync the dataset (downloads upstream policies + test fixtures)
python3 scripts/sync_kyverno_policies.py
```

### Configuring tool secrets (required for containerized runs)

Containerized runs read credentials from shell environment variables.

Set the variables for the tools you plan to run:

```bash
export NIRMATA_TOKEN=...
export NIRMATA_URL=https://nirmata.io
export ANTHROPIC_API_KEY=...
export CURSOR_API_KEY=...
```

Required variables by tool:

| Tool | Required variables |
|---|---|
| nctl | `NIRMATA_TOKEN`, `NIRMATA_URL` (and optionally AWS vars) |
| claude | `ANTHROPIC_API_KEY` |
| cursor | `CURSOR_API_KEY` |

If required credentials are missing for a selected tool, preflight checks fail before launching containers.

---

## Test Architecture

This project is an integration-testing benchmark — there are no unit tests. All validation happens by running real tool outputs through a three-layer pipeline:

```
Tool output (YAML)
       │
       ▼
┌─────────────────────────────────┐
│  Layer 1: Schema + CEL          │  Go binary (cmd/validate-policy/)
│  – OpenAPI schema validation    │  Must pass to continue
│  – CEL expression compilation   │
└─────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────┐
│  Layer 2: Structural Lint       │  Python (evaluators/structural_lint.py)
│  – Anti-pattern checks          │  Advisory only — never fails overall result
│  – MutatingPolicy ordering      │
└─────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────┐
│  Layer 3: Functional Test       │  `kyverno test` via Kyverno CLI
│  – Real good/bad resources      │  Skipped if kyverno not on PATH
│  – Policy behavior verification │
└─────────────────────────────────┘
```

**Overall pass** = `schema_pass` AND (`semantic_pass` OR `semantic_skipped`).

The test dataset is defined in `dataset/index.yaml`. Each entry points to a source policy and an optional test fixture directory. All test fixtures follow the Kyverno CLI test format (`kyverno-test.yaml` + `resource.yaml`).

```
dataset/
├── index.yaml                            # Task definitions (32 entries)
├── imported/
│   ├── kyverno-policies/                 # Source policies (synced from kyverno/policies)
│   └── kyverno-tests/<policy-id>/        # Test fixtures per policy
│       ├── kyverno-test.yaml
│       └── resource.yaml
├── cleanup/                              # Local CleanupPolicy examples
├── gatekeeper/                           # Local Gatekeeper examples
└── stress/                               # Edge cases (malformed, empty, etc.)
```

---

## Integration Tests

Every benchmark run is an integration test: a tool converts a source policy, and the output is validated through the three layers above. No mocking, no isolated unit assertions.

### Functional test fixtures

Each policy that has `kyverno_test_dir` set in `dataset/index.yaml` gets a functional test. Fixtures live in `dataset/imported/kyverno-tests/<policy-id>/` (for upstream policies) or `dataset/local/<policy-id>/` (for custom policies).

**`kyverno-test.yaml`** — declares what the policy should pass and fail:

```yaml
apiVersion: cli.kyverno.io/v1alpha1
kind: Test
metadata:
  name: require-labels
policies:
  - ../kyverno-policies/cp_require_labels.yaml
resources:
  - resource.yaml
results:
  - kind: Pod
    policy: require-labels
    resources: [badpod01]
    result: fail
    rule: check-for-labels
  - kind: Pod
    policy: require-labels
    resources: [goodpod01]
    result: pass
    rule: check-for-labels
```

**`resource.yaml`** — the Kubernetes resources the policy is tested against:

```yaml
# Should fail policy check
apiVersion: v1
kind: Pod
metadata:
  name: badpod01
spec:
  containers: [{name: nginx, image: nginx:1.12}]
---
# Should pass policy check
apiVersion: v1
kind: Pod
metadata:
  name: goodpod01
  labels:
    app.kubernetes.io/name: nginx
spec:
  containers: [{name: nginx, image: nginx:1.12}]
```

For MutatingPolicy tests, also add `patchedResource.yaml` with the expected mutated output.

> **Auto-patching:** The semantic validator automatically patches `results.policy` to match the converted policy's `metadata.name` and strips `rule` fields for new policy types (ValidatingPolicy, MutatingPolicy, etc.) that don't use named rules. You do not need to update these manually.

Run a fixture manually:

```bash
kyverno test dataset/imported/kyverno-tests/cp_require_labels/
```

---

## Test Data Conventions

| Element | Convention | Example |
|---|---|---|
| Policy ID | `<track_prefix>_<snake_case_name>` | `cp_require_labels`, `gk_container_limits` |
| Track prefix | `cp_` (ClusterPolicy), `gk_` (Gatekeeper), `opa_`, `sentinel_`, `cleanup_` | `cp_require_ro_rootfs` |
| Upstream policy file | `dataset/imported/kyverno-policies/<id>.yaml` | `cp_require_labels.yaml` |
| Upstream test dir | `dataset/imported/kyverno-tests/<id>/` | `kyverno-tests/cp_require_labels/` |
| Custom policy file | `input/<descriptive-name>.yaml` or `dataset/local/<id>.yaml` | `input/require-resource-limits.yaml` |
| Custom test dir | `dataset/local/<id>/` | `dataset/local/my_custom_policy/` |
| Test manifest | Always `kyverno-test.yaml` | — |
| Test resources | Always `resource.yaml` (or `resources.yaml` for upstream) | — |
| Result files | Per-policy: `results/run_<timestamp>_<tool>_<policy_id>.json`; aggregated: `results/benchmark_<timestamp>.json` | — |

Policies in `dataset/stress/` are intentionally malformed — do not add well-formed policies there.

---

## Adding a New Test Case

### Option A: From upstream kyverno/policies (recommended)

1. Add an entry to `dataset/kyverno-upstream-manifest.yaml`:
   ```yaml
   - id: cp_my_new_policy
     upstream_path: best-practices/my-policy/my-policy.yaml
     sync_test: true
   ```

2. Sync to download the policy and its test fixtures:
   ```bash
   python3 scripts/sync_kyverno_policies.py
   ```
   This writes to `dataset/imported/kyverno-policies/` and `dataset/imported/kyverno-tests/`.

3. Add to `dataset/index.yaml`:
   ```yaml
   - id: cp_my_new_policy
     track: cluster-policy
     task_type: convert
     difficulty: medium
     expected_output_kind: ValidatingPolicy
     path: imported/kyverno-policies/cp_my_new_policy.yaml
     kyverno_test_dir: imported/kyverno-tests/cp_my_new_policy
     description: What the policy enforces
   ```

4. Verify the test case runs cleanly:
   ```bash
   ./run-benchmark.sh --tool nctl --policy-id cp_my_new_policy --containerized
   ```

### Option B: Custom local policy

1. Place the source policy in `input/`:
   ```
   input/my-custom-policy.yaml
   ```

2. Create test fixtures:
   ```
   dataset/local/my_custom_policy/
   ├── kyverno-test.yaml
   └── resource.yaml
   ```

3. Add to `dataset/index.yaml` (adjust `path` and `kyverno_test_dir`):
   ```yaml
   - id: my_custom_policy
     track: cluster-policy
     task_type: convert
     difficulty: easy
     expected_output_kind: ValidatingPolicy
     path: local/my_custom_policy/source.yaml
     kyverno_test_dir: local/my_custom_policy
     description: What the policy enforces
   ```

4. Validate the source policy before benchmarking:
   ```bash
   python3 validate.py --input input/my-custom-policy.yaml
   ```

5. Run the benchmark:
   ```bash
   ./run-benchmark.sh --tool nctl --policy-id my_custom_policy --containerized
   ```

---

## Stress Tests

`dataset/stress/` contains intentionally broken or pathological policies used to verify that the validator handles bad input gracefully rather than crashing or silently passing.

Current stress cases:

| File | What it tests |
|---|---|
| `empty-rules.yaml` | Policy with a rules array but no rule entries |
| `malformed-yaml.yaml` | Invalid YAML syntax (parse error expected) |
| `missing-spec.yaml` | Valid YAML but missing the `spec` field entirely |

### When to add a stress test case

Add a stress case when you find an input that causes the validator, benchmark harness, or a runner to behave unexpectedly — crash, hang, emit a misleading result, or silently succeed. Stress cases are **not** added to `dataset/index.yaml` and are not counted in the leaderboard.

### Adding a stress case

1. Add the file to `dataset/stress/<descriptive-name>.yaml`.
2. Run the validator against it manually to confirm the error is handled cleanly (non-zero exit, readable error message):
   ```bash
   python3 validate.py --input dataset/stress/my-bad-policy.yaml
   ```
3. The expected outcome is `schema_pass: false` with a clear error in `schema_errors` — not an unhandled exception.

---

## Adding a New Tool

Tools are plugged in via four touch points — the benchmark harness auto-discovers runners via convention, so no manual wiring is needed. Use an existing runner (e.g., `runners/claude_runner.py`) as a reference.

### 1. Create the runner

Create `runners/<tool>_runner.py` extending `ToolRunner` from `runners/base.py`:

```python
from .base import RunResult, ToolRunner

class MyToolRunner(ToolRunner):
    name = "mytool"                   # matches the key in config.yaml

    def run(self, input_path: Path, output_path: Path, prompt: str, *, 
            timeout_seconds: int = 120, config: dict | None = None) -> RunResult:
        # 1. Invoke the tool (subprocess, API call, etc.)
        # 2. Capture output and write YAML to output_path
        # 3. Return a RunResult with timing, token counts, cost
        ...
```

The contract from `base.py`:
- `run()` must write the converted policy to `output_path` if conversion succeeded.
- Return a `RunResult` with `output_path`, `success=True/False`, `conversion_time_seconds`, and — if available — `input_tokens`, `output_tokens`, `cost_usd`, `model`.
- Token counts may be estimated using `estimate_tokens()` from `base.py` if the tool doesn't expose them.

### 2. Add a Docker image (for containerized runs)

Create `docker/Dockerfile.mytool` and `docker/entrypoints/run-mytool.sh`. Follow the pattern of `Dockerfile.claude` and `run-claude.sh`. Then add it to `docker/build.sh`.

### 3. Add credentials template

Add the required environment variables to `docker/.env.example`:

```bash
# --- mytool.env ---
# MYTOOL_API_KEY=your-key-here
```

And create `docker/secrets/mytool.env` locally (gitignored).

### 4. Register in `config.yaml`

```yaml
tools:
  mytool:
    type: cli
    command: "mytool convert"
    api_key_env: "MYTOOL_API_KEY"
    enabled: true
```

### Verify

```bash
./run-benchmark.sh --tool mytool --policy-id cp_require_labels --containerized
```

---

## Local Test Commands

### Prerequisites

```bash
# Python dependencies
pip install -r requirements.txt

# Build the Go validator binary (required for schema + CEL validation)
cd cmd/validate-policy && GOWORK=off go build -o ../../validate-policy . && cd ../..

# Install Kyverno CLI (required for functional tests)
# https://kyverno.io/docs/installation/quick-start/#install-kyverno-cli
```

### Run commands

| Command | What it does |
|---|---|
| `./run-benchmark.sh --tool nctl --policy-id <id> --containerized` | Run one policy through one tool (containerized) |
| `./run-benchmark.sh --tool nctl claude --containerized` | Run all policies through multiple tools |
| `./run-benchmark.sh --report` | Regenerate the HTML + Markdown dashboard from existing results |
| `python3 validate.py --input <policy.yaml>` | Validate an input policy (schema check only) |
| `python3 validate.py --input <in.yaml> --output <out.yaml> --tool nctl` | Validate a converted policy (all three layers) |
| `python3 validate.py --output <out.yaml> --tool claude` | Validate a generated policy (no source) |
| `python3 validate.py ... --skip-kyverno-test` | Skip functional test (useful if Kyverno CLI not installed) |
| `kyverno test dataset/imported/kyverno-tests/<id>/` | Run one policy's functional test directly |
| `python3 scripts/sync_kyverno_policies.py` | Sync upstream policies and test fixtures |
| `python3 reports/generate.py` | Generate HTML + Markdown report from `results/` |

### Expected artifacts

| Command | Output |
|---|---|
| `validate.py` | JSON result printed to stdout |
| `benchmark.py` / `run-benchmark.sh` | Per-policy `results/run_<timestamp>_<tool>_<policy_id>.json`, aggregated `results/benchmark_<timestamp>.json` |
| `reports/generate.py` | `reports/output/dashboard.html`, `reports/output/report.md` |
| `kyverno test` | Pass/fail summary printed to stdout, no files written |

---

## Scoping Benchmark Runs

Running all 32 policies across multiple tools takes time. Use these flags to scope runs while developing or testing a specific change.

```bash
# Single policy
python3 benchmark.py --tool nctl --policy-id cp_require_labels

# Filter by difficulty (easy | medium | hard)
python3 benchmark.py --tool nctl --difficulty easy

# Filter by track (currently all 32 policies are cluster-policy)
python3 benchmark.py --tool nctl --track cluster-policy

# Filter by expected output kind
python3 benchmark.py --tool nctl --output-kind MutatingPolicy

# Filter by task type
python3 benchmark.py --tool nctl --task-type generate

# Iterative — re-run failing policies up to N times and keep the best result
python3 benchmark.py --tool nctl --max-attempts 3

# Combine filters
python3 benchmark.py --tool claude --track cluster-policy --difficulty easy --max-attempts 2
```

> `benchmark_latest.json` is never auto-produced — you must manually promote an aggregated run file. See [Updating the Leaderboard](#updating-the-leaderboard).

---

## CI-Equivalent Commands

The only CI job (`deploy-dashboard.yml`) deploys the leaderboard dashboard to GitHub Pages when `results/benchmark_latest.json` changes. It does not run the benchmark itself.

To reproduce what CI does:

```bash
pip install -r requirements.txt
python3 reports/generate.py --format html
# Output: reports/output/dashboard.html
```

To update the leaderboard, run the benchmark locally, promote the aggregated result, and commit:

```bash
./run-benchmark.sh --tool nctl claude --containerized
# Then promote the aggregated file:
cp results/benchmark_<timestamp>.json results/benchmark_latest.json
# Inspect and commit results/benchmark_latest.json
```

---

## Updating the Leaderboard

`results/benchmark_latest.json` is the file CI reads to deploy the public dashboard. Treat it as a curated artifact, not a scratch file.

### When to update it

- After a **complete run** across all policies and all tools you want to represent: `./run-benchmark.sh --tool nctl claude --containerized`
- When adding a new policy to `dataset/index.yaml` and you have fresh results that include it.
- When fixing a bug in the validator or a runner that changed pass/fail outcomes.

### When NOT to update it

- From a filtered or partial run (`--policy-id`, `--difficulty`, `--track`, etc.) — partial results would silently drop policies from the leaderboard.
- From an aborted run — the aggregated file would only reflect completed policies.
- To make a single tool look better in isolation without re-running all tools.

### How it works

`benchmark.py` writes per-policy results as `results/run_<timestamp>_<tool>_<policy_id>.json` and an aggregated file as `results/benchmark_<timestamp>.json`. It does **not** auto-produce `benchmark_latest.json` — you must promote a run manually:

```bash
cp results/benchmark_20250601_120000.json results/benchmark_latest.json
```

Commit only `results/benchmark_latest.json` — individual run files (`run_*.json`) and timestamped aggregates are gitignored.

---

## Failure Triage Workflow

### Schema or CEL validation failure

**Symptom:** `schema_pass: false` in the result JSON, with errors in `schema_errors`.

```json
{
  "schema_pass": false,
  "schema_errors": ["spec.rules[0].validate.cel.expressions[0]: unknown field 'expresion'"]
}
```

**Steps:**
1. Read the error — it usually names the exact field path.
2. Run the validator directly to iterate quickly:
   ```bash
   python3 validate.py --input input/policy.yaml --output output/converted.yaml --tool nctl --skip-kyverno-test
   ```
3. Fix the converted policy YAML or, if this is a source policy issue, fix the source fixture.
4. If the error is `CEL compilation failed`, check CEL expression syntax against the [Kyverno CEL docs](https://kyverno.io/docs/writing-policies/cel/).

### Functional test failure

**Symptom:** `semantic_pass: false`, with errors in `semantic_errors`.

```json
{
  "semantic_pass": false,
  "semantic_errors": ["FAIL: badpod01 expected=fail actual=pass"]
}
```

**Steps:**
1. Run the test directly to see full output:
   ```bash
   kyverno test dataset/imported/kyverno-tests/<policy-id>/
   ```
2. Compare the converted policy's behavior against the test fixtures:
   - If the policy is correct but fixtures are stale (upstream changed), re-sync: `python3 scripts/sync_kyverno_policies.py`
   - If the tool produced a policy that doesn't match the intended behavior, that is a tool failure (expected) — no fixture change needed.
3. If `semantic_skipped: true` and you expected it to run, confirm `kyverno` is on `PATH`: `kyverno version`.

### `semantic_skipped` when Kyverno CLI is missing

```bash
# Install Kyverno CLI
curl -LO "https://github.com/kyverno/kyverno/releases/latest/download/kyverno_linux_amd64.tar.gz"
tar -xvf kyverno_linux_amd64.tar.gz
chmod +x kyverno && mv kyverno /usr/local/bin/
```

### Go validator build failure

**Symptom:** `python3 validate.py` errors with `validate-policy binary not found` or subprocess error.

```bash
cd cmd/validate-policy
GOWORK=off go build -o ../../validate-policy .
cd ../..
# Verify:
./validate-policy --help
```

### Benchmark result not updating

`results/benchmark_latest.json` is never auto-produced. After a successful full run, manually promote the aggregated file: `cp results/benchmark_<timestamp>.json results/benchmark_latest.json`.

### Dataset out of sync

If `dataset/imported/` is empty or stale:

```bash
pip install pyyaml
python3 scripts/sync_kyverno_policies.py
```

---

## Minimum Checks Before PR

Before opening a PR, verify:

- [ ] **Source policy is valid** — `python3 validate.py --input <your-policy.yaml>` exits cleanly.
- [ ] **New test case runs end-to-end** — `./run-benchmark.sh --tool nctl --policy-id <your-id> --containerized` completes without a crash (tool failures on the conversion are expected and acceptable; infra errors are not).
- [ ] **Functional test fixtures cover at least one pass and one fail resource** — every `kyverno-test.yaml` must include both a resource that should be admitted and one that should be rejected.
- [ ] **`dataset/index.yaml` entry is complete** — all required fields (`id`, `track`, `task_type`, `difficulty`, `expected_output_kind`, `path`, `description`) are present.
- [ ] **Dashboard regenerates cleanly** — `python3 reports/generate.py` produces `reports/output/dashboard.html` without errors.
- [ ] **No secrets or credentials committed** — `docker/secrets/` is gitignored; verify with `git status` before committing.

A contributor who adds one new test case should be able to confirm it works by running:
```bash
./run-benchmark.sh --tool nctl --policy-id <new-id> --containerized
```
and seeing a result JSON file for `<new-id>` in `results/` (e.g., `results/run_*_nctl_<new-id>.json`).
