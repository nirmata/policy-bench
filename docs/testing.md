# Testing Guide

How the benchmark validates converted policies, and how to add new test cases.

## Validation Layers

Every converted policy goes through three validation layers, in order:

1. **Schema + CEL** — A Go binary (`cmd/validate-policy/`) validates YAML structure against Kyverno's OpenAPI schemas and compiles all CEL expressions through Kyverno's engine. Catches wrong apiVersions, invalid fields, and broken CEL.

2. **Structural lint** (advisory) — Catches common patterns the CEL compiler misses, like append-vs-prepend ordering in MutatingPolicy or missing `matchConditions` on filtered containers. Warnings appear in `lint_warnings` in the result JSON but do not fail the overall validation.

3. **Functional test** — Runs `kyverno test` with real Kubernetes resources (good and bad) to verify the policy actually enforces what it should. No cluster required — the Kyverno CLI handles this locally.

A policy **passes** if Schema + CEL passes AND the functional test passes (or is skipped). Structural lint warnings are advisory. If the Kyverno CLI isn't installed, the functional test is skipped (not failed).

## Running Tests

### Single policy, single tool
```bash
./run-benchmark.sh --tool nctl --policy-id cp_require_labels --containerized
```

### All policies, multiple tools
```bash
./run-benchmark.sh --tool nctl claude cursor --containerized
```

### Manual validation (no benchmark harness)
```bash
# Validate input policy only
python3 validate.py --input input/require-resource-limits.yaml

# Validate a conversion (schema + CEL + functional)
python3 validate.py --input input/policy.yaml --output output/converted.yaml --tool nctl

# Skip functional test
python3 validate.py --input input/policy.yaml --output output/converted.yaml --tool nctl --skip-kyverno-test
```

### Regenerate the dashboard from existing results
```bash
./run-benchmark.sh --report    # must be the only argument
```

## Adding a New Test Case

### From upstream kyverno/policies (recommended)

1. **Add to the upstream manifest** (`dataset/kyverno-upstream-manifest.yaml`):
   ```yaml
   - id: cp_my_new_policy
     upstream_path: best-practices/my-policy/my-policy.yaml
     sync_test: true
   ```

2. **Sync** to download the policy and its test fixtures:
   ```bash
   python3 scripts/sync_kyverno_policies.py
   ```
   This writes to `dataset/imported/kyverno-policies/` and `dataset/imported/kyverno-tests/`.

3. **Add to the dataset index** (`dataset/index.yaml`):
   ```yaml
   - id: cp_my_new_policy
     track: cluster-policy     # cluster-policy | gatekeeper | opa | sentinel | cleanup
     task_type: convert        # convert | generate
     difficulty: easy          # easy | medium | hard
     expected_output_kind: ValidatingPolicy
     path: imported/kyverno-policies/cp_my_new_policy.yaml
     kyverno_test_dir: imported/kyverno-tests/cp_my_new_policy
     description: Short description of what the policy enforces
   ```

4. **Run it** to verify the test case works:
   ```bash
   ./run-benchmark.sh --tool nctl --policy-id cp_my_new_policy --containerized
   ```

### Custom (non-upstream) policy

1. **Place the source policy** in `input/`:
   ```
   input/my-custom-policy.yaml
   ```

2. **Create test fixtures** (optional but recommended) in a new directory:
   ```
   dataset/local/my-custom-policy/
   ├── kyverno-test.yaml
   └── resource.yaml
   ```

3. **Add to `dataset/index.yaml`** as above, adjusting `path` and `kyverno_test_dir`.

4. **Validate the input** before benchmarking:
   ```bash
   python3 validate.py --input input/my-custom-policy.yaml
   ```

## Test Fixture Format

Each test directory under `dataset/imported/kyverno-tests/<policy-id>/` contains:

### kyverno-test.yaml
```yaml
apiVersion: cli.kyverno.io/v1alpha1
kind: Test
metadata:
  name: require-labels
policies:
  - ../require-labels.yaml
resources:
  - resource.yaml
results:
  - kind: Pod
    policy: require-labels
    resources:
      - badpod01
    result: fail
    rule: check-for-labels
  - kind: Pod
    policy: require-labels
    resources:
      - goodpod01
    result: pass
    rule: check-for-labels
```

### resource.yaml
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: badpod01
spec:
  containers:
    - name: nginx
      image: nginx:1.12
---
apiVersion: v1
kind: Pod
metadata:
  name: goodpod01
  labels:
    app.kubernetes.io/name: nginx
spec:
  containers:
    - name: nginx
      image: nginx:1.12
```

For MutatingPolicy tests, include a `patchedResource.yaml` with the expected mutated output.

**Auto-patching:** During evaluation, the semantic validator automatically patches `results.policy` to match the converted policy's `metadata.name` and strips `rule` fields for new policy types (ValidatingPolicy, MutatingPolicy, etc.) that don't use named rules.

## Index Fields Reference

| Field | Required | Values |
|-------|----------|--------|
| `id` | yes | Unique identifier (e.g., `cp_require_labels`) |
| `track` | yes | `cluster-policy`, `gatekeeper`, `opa`, `sentinel`, `cleanup` (not all tracks have test cases yet) |
| `task_type` | yes | `convert` or `generate` |
| `difficulty` | yes | `easy`, `medium`, `hard` |
| `expected_output_kind` | yes | `ValidatingPolicy`, `MutatingPolicy`, `GeneratingPolicy`, `DeletingPolicy`, `ImageValidatingPolicy` |
| `path` | yes | Relative to `dataset/` — path to source policy |
| `kyverno_test_dir` | no | Relative to `dataset/` — path to test fixtures directory |
| `description` | yes | What the policy enforces |

## Result Interpretation

Each benchmark result JSON contains:

| Field | Meaning |
|-------|---------|
| `schema_pass: true` | YAML is valid and all CEL expressions compile (inspect `schema_errors` for details) |
| `lint_pass: true` | Structural lint passed (no warnings) |
| `lint_warnings: [...]` | Advisory warnings about common patterns (e.g., append vs prepend) |
| `semantic_pass: true` | `kyverno test` passed — policy behaves correctly |
| `semantic_skipped: true` | No test directory or Kyverno CLI not on PATH |

**Overall pass** = `schema_pass` AND (`semantic_pass` OR `semantic_skipped`). Lint warnings are advisory and do not affect the pass/fail result.
