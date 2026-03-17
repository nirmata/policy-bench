# Policy Conversion Benchmark

A **public benchmark** for converting old Kyverno policies to Kyverno 1.16+ (e.g. ValidatingPolicy). Use the same input and the same validation to compare **NPA (nctl)**, **Cursor Agent**, **Claude Code**, or any other AI/tool.

## Table of contents

- [Prerequisites](#prerequisites)
- [Folder layout](#folder-layout)
- [Step-by-step flow](#step-by-step-flow)
  - [1. Clone the repo](#1-clone-the-repo)
  - [2. Input: choose a policy to convert](#2-input-choose-a-policy-to-convert)
  - [3. If you use your own policy, make sure it is valid first](#3-if-you-use-your-own-policy-make-sure-it-is-valid-first-optional-before-converting)
  - [4. Conversion prompt](#4-conversion-prompt-use-this-exact-task-for-fair-comparison)
  - [5. Run conversion with nctl (NPA)](#5-run-conversion-with-nctl-npa)
  - [6. Run validation](#6-run-validation)
  - [7. Kyverno CLI test (semantic validation)](#7-kyverno-cli-test-semantic-validation--runs-by-default)
  - [8. Compare with another AI](#8-compare-with-another-ai-cursor-claude-etc)
- [How we validate your policy (no cluster required)](#how-we-validate-your-policy-no-cluster-required)
- [What the validator checks](#what-the-validator-checks)
- [Results format](#results-format)
- [License](#license)

---

## Prerequisites

Install the following and ensure they are on your PATH (unless noted):

| Tool | Purpose |
|------|--------|
| **Git** | Clone this repo |
| **Python 3** (3.9+) | Run the validation script |
| **PyYAML** | `pip install pyyaml` (required by the validator) |
| **nctl** | Run conversions with NPA; run **nctl login** first (optional if you only compare other AIs) |
| **kubectl** | Used by validator for schema dry-run (optional if no cluster) |
| **kyverno CLI** | Used by default for semantic validation (step 7). Runs **locally**—no Kubernetes cluster or Kind needed. Use **--skip-kyverno-test** to skip. |

No other dependencies: the validator uses only the Python standard library and PyYAML (`pip install pyyaml` if needed).

---

## Folder layout

```
convert-policies/
├── README.md             # This file
├── validate.py           # Validation script (input + output schema + intent)
├── validate-legacy.py    # Legacy policy validation (used by validate.py; can also run standalone)
├── input/                # Policies to convert (add yours or use the sample)
├── output/               # Put the converted policy here (from nctl or any AI)
├── results/              # Validation results (JSON) go here
├── sample-policies/      # Example legacy policies (ClusterPolicy YAMLs)
├── test-resources/       # Test resources (e.g. Pods) for running policies
├── kyverno-tests/        # Kyverno CLI test (cli.kyverno.io Test + resources); runs by default unless --skip-kyverno-test
└── run-nctl-conversion.sh # Run nctl AI conversion with full logging (nctl version + output) to verify skill loading
```

---

## Step-by-step flow

### 1. Clone the repo

```bash
git clone <this-repo-url>
cd convert-policies
```

### 2. Input: choose a policy to convert

- **Benchmark:** Use the provided sample policy in `input/require-resource-limits.yaml`.
- **Your own:** Add your own policy YAML into `input/` (e.g. `input/my-policy.yaml`). It should be an old Kyverno `ClusterPolicy` (`apiVersion: kyverno.io/v1`) or a Gatekeeper ConstraintTemplate + Constraint. *(Input validation in step 3 and the validator currently support ClusterPolicy; Gatekeeper support may be added later.)*

### 3. If you use your own policy, make sure it is valid first (optional, before converting)

If you use your own policy (not the benchmark sample), validate it before converting. Invalid input makes conversion and comparison results unreliable. You can skip this step when using `input/require-resource-limits.yaml`; for any other input, run:

```bash
python3 validate.py --input input/your-policy.yaml
```

You should see **PASS** (`Input policy: PASS` from `validate.py`, or `Validation: PASS` from `validate-legacy.py`). Do not proceed to conversion if you see **FAIL**.


### 4. Conversion prompt (use this exact task for fair comparison)

Use one of the prompts below with **nctl** or **any other AI** so results are comparable.

**1. When you use our sample policy YAML file** (benchmark):

```
Convert the policy in input/require-resource-limits.yaml to a Kyverno ValidatingPolicy (Kyverno 1.16+) using CEL-based validation where appropriate. Write the converted policy to output/converted.yaml.
```

**2. When you use your own custom policy file** (e.g. `input/my-policy.yaml`):

```
Convert the policy in input/my-policy.yaml to a Kyverno ValidatingPolicy (Kyverno 1.16+) using CEL-based validation where appropriate. Write the converted policy to output/converted.yaml.
```

Replace `input/my-policy.yaml` with your actual input path. The **output** should always be `output/converted.yaml` (or use the same path every time so the validator can find it).

### 5. Run conversion with nctl (NPA)

Use this step when you want to **test** the nctl AI conversion feature. It converts your policy using nctl’s AI mechanism.

**To log the nctl version and capture all nctl AI output** (so you can verify the conversion skill was loaded), use the helper script:

```bash
./run-nctl-conversion.sh
# Or with your own input file:
./run-nctl-conversion.sh input/my-policy.yaml
```

The script writes **nctl version** and the **full nctl ai console output** to `results/nctl_conversion_<timestamp>.log`. Check that log for lines like `Reading file from .../converting-policies/SKILL.md` or `policy-skills` to confirm the conversion skill was loaded and used. The script uses **`--skip-permission-checks`** so nctl does not prompt for confirmation (e.g. "Does this capture the policy intent?") and the conversion can run non-interactively (e.g. in CI or when there is no TTY).

To run the conversion without the script (no log file). Add **`--skip-permission-checks`** if you need non-interactive runs:

```bash
nctl ai --allowed-dirs "$(pwd)" --prompt "Convert the policy in input/require-resource-limits.yaml to a Kyverno ValidatingPolicy (Kyverno 1.16+) using CEL-based validation where appropriate. Write the converted policy to output/converted.yaml." --skip-permission-checks
```

If you are converting your own policy (from step 2), replace `input/require-resource-limits.yaml` in the prompt with your input path (e.g. `input/my-policy.yaml`). Ensure the converted policy is saved to `output/converted.yaml` (nctl or you may need to copy it there).

**Using another AI (e.g. ChatGPT, Cursor, or another agent)?** Skip this step. Use one of the prompts from step 4 in your AI, save the converted policy to `output/converted.yaml`, then run step 6 to validate the output.

### 6. Run validation

Run this after conversion (whether you used nctl in step 5 or another AI in step 4). Use the **same** `--input` path as the policy you converted, and the path where you saved the converted policy as `--output`:

```bash
python3 validate.py --input input/require-resource-limits.yaml --output output/converted.yaml --tool nctl
```

If you converted your own policy, set `--input` to that file (e.g. `--input input/my-policy.yaml`). If you saved the conversion elsewhere (e.g. `output/cursor/converted.yaml`), set `--output` to that path.

- **`--input`** — Path to the **original** policy (for intent comparison). It is validated first; if invalid, the script exits before checking the output.
- **`--output`** — Path to the **converted** policy (the file to validate).
- **`--tool`** — Label for this run (e.g. `nctl`, `cursor`, `claude`). Used in the results JSON filename.

Results are written to `results/run_<timestamp>_<tool>.json`. **Semantic validation** (Kyverno CLI test) runs by default when `kyverno-tests/` exists and `kyverno` is on PATH; use **--skip-kyverno-test** to skip it (see step 7).

### 7. Kyverno CLI test (semantic validation) — runs by default

**No cluster needed.** When you run step 6, the validator also runs the **Kyverno CLI test** by default (if the `kyverno` CLI is on your PATH and the `kyverno-tests/` directory exists). The CLI runs **locally** against YAML files; you do **not** need a Kind cluster or Kyverno installed in a cluster. Install the [Kyverno CLI](https://kyverno.io/docs/kyverno-cli/) and ensure it is on your PATH.

To **skip** semantic validation (e.g. if you don't have the Kyverno CLI or use a custom test dir elsewhere), pass **--skip-kyverno-test**:

```bash
python3 validate.py --input input/require-resource-limits.yaml --output output/converted.yaml --tool nctl --skip-kyverno-test
```

You can also run the test manually:

```bash
kyverno test kyverno-tests/
```

The repo includes a minimal `kyverno-tests/` for the sample policy: it expects the converted policy in `output/converted.yaml`. The test's `results.policy` must match the policy's `metadata.name` (e.g. `require-cpu-memory-limits` for nctl output). If your converter uses a different policy name, edit `kyverno-tests/kyverno-test.yaml` to match. When the test runs, it checks that the converted policy **passes** on compliant resources and **fails** on non-compliant ones—so you can tell if the conversion is accurate, not just valid YAML. To use a different test directory, pass **--kyverno-test-dir &lt;dir&gt;** (default is `kyverno-tests`).

**Note:** As of Kyverno CLI 1.17, the `kyverno test` command does not yet support the ValidatingPolicy 1.16+ schema (e.g. `spec.admission`, `spec.assertions`). If you see **Semantic: SKIP** with that message, the validator is treating it as a known limitation—schema and intent still validate your conversion. Use **--skip-kyverno-test** to skip the step explicitly.

### 8. Compare with another AI (Cursor, Claude, etc.)

**Fair comparison.** When you benchmark an AI other than nctl (e.g. ChatGPT, Cursor, Claude, or another agent), the conversion must be done **only** by that AI. If Nirmata MCP servers or Nirmata skills are enabled in your environment (e.g. in Cursor or another IDE), they can take part in the conversion and the result will no longer reflect that agent alone. Before running the other AI, disable or remove any Nirmata MCP servers and Nirmata-related skills so the benchmark measures only the agent you are testing.

1. Use the **same** input file and the **same** prompt (see step 4).
2. Run the other AI (Cursor Agent, Claude Code, etc.) and get the converted policy.
3. Save that converted policy to `output/converted.yaml` (overwrite), or to a path like `output/cursor/converted.yaml` if you keep multiple runs.
4. Run the same validation command. Use `--input` for the original policy you converted and `--output` for where you saved the converted file; set `--tool` to a label for this AI (e.g. `cursor`):

   ```bash
   python3 validate.py --input input/require-resource-limits.yaml --output output/converted.yaml --tool cursor
   ```

   If you saved to a subfolder (e.g. `output/cursor/converted.yaml`), use that path for `--output`.

5. Open the JSON in `results/` to compare: `schema_pass`, `intent_pass`, and any error messages.

---

## How we validate your policy (no cluster required)

User policies are validated by **Python scripts only** (PyYAML + structure checks). You do **not** need a Kubernetes cluster or a Kyverno installation.

- **What runs:** `validate-legacy.py` or `validate.py --input <path>` checks that the file is valid YAML, has `kind: ClusterPolicy`, `apiVersion: kyverno.io/...`, and that each rule has `match` and a valid `validate` block (pattern/anyPattern/deny + message).
- **Optional:** If `kubectl` is on your PATH, the script may run `kubectl apply -f <policy> --dry-run=client`. That only succeeds when a cluster with Kyverno CRDs is available; if not (e.g. no cluster or CRDs not installed), the script ignores that failure and still reports PASS from the structure checks.
- **Kyverno CLI:** Semantic validation runs **by default** when you run `validate.py` with `--output`: the script runs `kyverno test kyverno-tests/` (or the dir given by `--kyverno-test-dir`). The CLI runs **locally** (no cluster needed). If the Kyverno CLI is not on PATH or the test dir is missing, semantic validation is skipped. Use **--skip-kyverno-test** to skip it explicitly.

---

## What the validator checks

- **Input policy (before conversion):** When you run `validate.py` with both `--input` and `--output`, the input file is validated first (legacy ClusterPolicy structure: YAML, kind, apiVersion, spec.rules, match, validate). If the input is invalid, the script exits with an error so you fix the policy before comparing conversion output. You can also validate input only with `python3 validate.py --input input/your-policy.yaml` (no `--output`).
- **Schema (output):** The converted file is valid YAML, has `kind: ValidatingPolicy` (or another Kyverno 1.16+ policy kind), and `apiVersion` starting with `policies.kyverno.io/`. Optionally runs `kubectl apply --dry-run=client` if kubectl is available.
- **Intent:** For ClusterPolicy → ValidatingPolicy: same target kinds (e.g. Pods) and same validation action (Enforce → Deny, Audit → Audit).
- **Semantic (default on):** Unless you pass **--skip-kyverno-test**, the script runs `kyverno test <dir>` (default dir: `kyverno-tests/`) when the Kyverno CLI is on PATH to check that the converted policy passes/fails on the test resources as expected. No cluster required.

---

## Results format

Each run produces a JSON file in `results/`, e.g. `results/run_20250115_143022_nctl.json`:

```json
{
  "input_path": "input/require-resource-limits.yaml",
  "output_path": "output/converted.yaml",
  "tool": "nctl",
  "timestamp": "2025-01-15T14:30:22",
  "schema_pass": true,
  "intent_pass": true,
  "schema_errors": [],
  "intent_errors": [],
  "semantic_pass": true,
  "semantic_errors": [],
  "semantic_skipped": false
}
```

When Kyverno test is skipped (**--skip-kyverno-test**, or `kyverno` not on PATH, or test dir missing), `semantic_skipped` is `true` and `semantic_pass`/`semantic_errors` may be omitted.

Use these files to compare accuracy across tools (and add timing/cost later if you collect them).

---

## License

See [LICENSE](LICENSE) in this repo.
