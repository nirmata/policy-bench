# Policy Conversion Benchmark

A **public benchmark** for converting old Kyverno policies to Kyverno 1.16+ (e.g. ValidatingPolicy). Use the same input and the same validation to compare **NPA (nctl)**, **Cursor Agent**, **Claude Code**, or any other AI/tool.

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
| **kyverno CLI** | Optional: only if you add semantic validation (e.g. `kyverno test`) |

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
└── test-resources/       # Test resources (e.g. Pods) for running policies
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

```bash
nctl ai --allowed-dirs "$(pwd)" --prompt "Convert the policy in input/require-resource-limits.yaml to a Kyverno ValidatingPolicy (Kyverno 1.16+) using CEL-based validation where appropriate. Write the converted policy to output/converted.yaml."
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

Results are written to `results/run_<timestamp>_<tool>.json`.

### 7. Compare with another AI (Cursor, Claude, etc.)

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
- **Kyverno CLI:** Not used for this validation. You can use it separately (e.g. `kyverno apply policy.yaml --resource test-resources/test-pod.yaml`) for extra semantic checks; it is optional and not required for the benchmark.

---

## What the validator checks

- **Input policy (before conversion):** When you run `validate.py` with both `--input` and `--output`, the input file is validated first (legacy ClusterPolicy structure: YAML, kind, apiVersion, spec.rules, match, validate). If the input is invalid, the script exits with an error so you fix the policy before comparing conversion output. You can also validate input only with `python3 validate.py --input input/your-policy.yaml` (no `--output`).
- **Schema (output):** The converted file is valid YAML, has `kind: ValidatingPolicy` (or another Kyverno 1.16+ policy kind), and `apiVersion` starting with `policies.kyverno.io/`. Optionally runs `kubectl apply --dry-run=client` if kubectl is available.
- **Intent:** For ClusterPolicy → ValidatingPolicy: same target kinds (e.g. Pods) and same validation action (Enforce → Deny, Audit → Audit).

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
  "intent_errors": []
}
```

Use these files to compare accuracy across tools (and add timing/cost later if you collect them).

---

## License

See [LICENSE](LICENSE) in this repo.
