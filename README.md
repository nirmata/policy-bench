# Policy Conversion Benchmark

A **public, reproducible benchmark** for converting and generating Kyverno policies. Compare **nctl (NPA)**, **Cursor Agent**, **Claude Code**, or any other AI/tool using the same inputs, prompts, and evaluation criteria.

Two task types:
- **Conversion** — transform an existing policy (ClusterPolicy, Gatekeeper, OPA, Sentinel, CleanupPolicy) into a Kyverno 1.16+ policy.
- **Generation** — produce a brand-new Kyverno 1.16+ policy from a natural-language description (no source policy).

> **Related repos**
> - [Nirmata/go-nctl](https://github.com/nirmata/go-nctl) — nctl CLI and AI conversion skills
> - [Nirmata/go-llms-apps](https://github.com/nirmata/go-llms-apps) — LLM application patterns for policy work

---

## Table of Contents

- [Quick Start](#quick-start)
- [Pipeline Overview](#pipeline-overview)
- [Supported Tracks and Output Types](#supported-tracks-and-output-types)
- [Dataset](#dataset)
- [Folder Layout](#folder-layout)
- [Running the Benchmark](#running-the-benchmark)
- [Evaluation](#evaluation)
- [Results Schema](#results-schema)
- [Report Generation](#report-generation)
- [Blind Evaluation](#blind-evaluation)
- [Stress Testing](#stress-testing)
- [Iterative Improvement](#iterative-improvement)
- [Legacy CLI (backward compatible)](#legacy-cli-backward-compatible)
- [Transparency Statement](#transparency-statement)
- [Contributing](#contributing)
- [License](#license)

---

## Quick Start

```bash
# 1. Clone and install dependencies
git clone <this-repo-url>
cd convert-policies
pip install -r requirements.txt   # PyYAML + Jinja2

# 2. Pull ClusterPolicy corpus from kyverno/policies (pinned revision; see manifest)
python3 scripts/sync_kyverno_policies.py
# (Requires network. Uses dataset/kyverno-upstream-manifest.yaml and writes dataset/imported/.)

# 3. Run the benchmark (nctl only, all policies)
python3 benchmark.py --tool nctl

# 4. Run only generation tasks
python3 benchmark.py --tool nctl --task-type generate

# 5. Run only easy ValidatingPolicy conversions
python3 benchmark.py --tool nctl --task-type convert --output-kind ValidatingPolicy --difficulty easy

# 6. Generate reports
python3 benchmark.py --report
# or directly:
python3 reports/generate.py
```

If you run `benchmark.py` before sync, ClusterPolicy paths under `dataset/imported/` are missing and the run fails with a pointer to `scripts/sync_kyverno_policies.py`.

Reports are written to `reports/output/` (Markdown + HTML dashboard).

The HTML dashboard expects **Jinja2** (see `requirements.txt`). Use your project venv so `python3` resolves to an interpreter with deps installed (e.g. `.venv/bin/python reports/generate.py`). If Jinja2 is missing, the generator falls back to a minimal single-table HTML and prints a warning.

---

## Pipeline Overview

```
dataset/  -->  benchmark.py  -->  runners/  -->  output/
                                                   |
                                             evaluators/
                                                   |
                                             results/ (JSON)
                                                   |
                                          reports/generate.py
                                                   |
                                        Markdown + HTML dashboard
```

1. **Dataset** — curated input policies organized by conversion track, plus generation task descriptions.
2. **Runners** — benchmark harnesses that wrap each tool (nctl, Claude, Cursor). Each harness sends the prompt, captures output, measures wall-clock time, estimates tokens/cost, and returns a standard `RunResult`.
3. **Evaluators** — schema validation, intent preservation, semantic tests (Kyverno CLI), diff scoring.
4. **Results** — rich JSON per run (time, tokens, cost, diff_score, pass/fail).
5. **Reports** — aggregated Markdown reports, HTML dashboard with charts and leaderboard for **combined**, **conversion-only**, and **generation-only** slices.

---

## Supported Tracks and Output Types

### Conversion Tracks

| Track | Source Format | Target Kind(s) | Count |
|-------|-------------|----------------|-------|
| `cluster-policy` | Kyverno ClusterPolicy (v1) | ValidatingPolicy, MutatingPolicy, GeneratingPolicy, ImageValidatingPolicy | 18 |
| `gatekeeper` | ConstraintTemplate + Constraint | ValidatingPolicy | 3 |
| `opa` | OPA / Rego | ValidatingPolicy | 3 |
| `sentinel` | HashiCorp Sentinel | ValidatingPolicy | 2 |
| `cleanup` | Kyverno CleanupPolicy | DeletingPolicy | 2 |

### Generation Tasks

| Output Kind | Count | Description |
|-------------|-------|-------------|
| ValidatingPolicy | 7 | Generate validation policies from NL prompts |
| MutatingPolicy | 2 | Generate mutation policies from NL prompts |
| GeneratingPolicy | 1 | Generate resource-generating policies from NL prompts |

### Totals

| Dimension | Breakdown |
|-----------|-----------|
| **By task type** | 28 conversion + 10 generation + 3 stress = **41 total** |
| **By difficulty** | 13 easy + 14 medium + 8 hard + 3 stress |
| **By output kind** | 22 ValidatingPolicy + 5 MutatingPolicy + 3 GeneratingPolicy + 1 ImageValidatingPolicy + 2 DeletingPolicy |

---

## Dataset

`dataset/index.yaml` lists every benchmark task. Sources are split:

| Source | Location | How it gets here |
|--------|----------|------------------|
| **Kyverno ClusterPolicy** | `dataset/imported/kyverno-policies/` + optional `dataset/imported/kyverno-tests/` | **`python3 scripts/sync_kyverno_policies.py`** copies from [kyverno/policies](https://github.com/kyverno/policies) at the pinned `ref` in `dataset/kyverno-upstream-manifest.yaml`. |
| **Local ClusterPolicy** | `dataset/local/cluster-policy/` | Maintained here (e.g. one complex multi-rule sample). |
| **Gatekeeper, OPA, Sentinel, Cleanup, stress** | `dataset/gatekeeper/`, `dataset/opa/`, etc. | Maintained in this repo. |
| **Generation tasks** | (no input file) | Prompt text in `index.yaml` `description`. |

Example index entries:

```yaml
policies:
  # Synced conversion task (run sync script first)
  - id: cp_require_resource_limits
    track: cluster-policy
    task_type: convert
    path: imported/kyverno-policies/cp_require_resource_limits.yaml
    difficulty: easy
    description: "Require CPU and memory requests/limits on Pods (upstream: require-pod-requests-limits)"
    expected_output_kind: ValidatingPolicy
    kyverno_test_dir: imported/kyverno-tests/cp_require_resource_limits

  # Generation task (no path — prompt only)
  - id: gen_require_labels
    track: cluster-policy
    task_type: generate
    difficulty: easy
    description: "requires all Pods to have the label 'app.kubernetes.io/name' ..."
    expected_output_kind: ValidatingPolicy
```

**Refreshing upstream:** edit `ref` in `dataset/kyverno-upstream-manifest.yaml`, run the sync script again, and re-run the benchmark. `dataset/imported/upstream-meta.json` records the last sync.

**Offline / CI:** either run the sync step in the pipeline (needs network) or vendor/commit the contents of `dataset/imported/` and adjust `.gitignore` if you want them tracked.

Each entry has:
- **id** — unique identifier used in results and output filenames.
- **track** — conversion track (determines prompt template, input validator, intent checker).
- **task_type** — `convert` (has `path` to source policy) or `generate` (prompt-only, no source).
- **difficulty** — `easy`, `medium`, `hard`, or `stress`.
- **expected_output_kind** — target Kyverno 1.16+ kind (ValidatingPolicy, MutatingPolicy, etc.).
- **description** — for conversion tasks, describes the policy; for generation tasks, this is the NL requirement used in the prompt.
- **kyverno_test_dir** — optional; Kyverno CLI policy tests (often copied from upstream `.kyverno-test`).
- **expect_failure** — if `true`, the policy is intentionally invalid (stress testing).

---

## Folder Layout

```
convert-policies/
  benchmark.py                 # Main orchestrator (supports convert + generate)
  config.yaml                  # Tool + track + evaluation settings
  validate.py                  # CLI wrapper (--input optional for generation)
  validate-legacy.py           # Legacy ClusterPolicy validator (standalone)
  run-nctl-conversion.sh       # Legacy nctl helper script
  requirements.txt             # Python dependencies
  scripts/
    sync_kyverno_policies.py   # Fetch kyverno/policies → dataset/imported/
  dataset/
    index.yaml                 # Policy manifest (41 entries: convert + generate + stress)
    kyverno-upstream-manifest.yaml  # Curated paths + pinned ref for sync script
    imported/                  # Generated by sync (policies + tests); see imported/README.md
    local/cluster-policy/      # Small in-repo ClusterPolicy samples (not from upstream)
    gatekeeper/                # ConstraintTemplate + Constraint samples
    opa/                       # Rego policy files
    sentinel/                  # Sentinel policy files
    cleanup/                   # CleanupPolicy samples
    stress/                    # Malformed / edge-case inputs
  runners/                     # Benchmark harnesses (one per tool)
    base.py                    # RunResult, ToolRunner ABC, token/cost estimation
    nctl_runner.py             # nctl ai CLI harness
    claude_runner.py           # claude CLI (primary) + Anthropic API (fallback)
    cursor_runner.py           # cursor CLI --force (primary) + manual (fallback)
    prompts.py                 # Prompt templates (conversion per track+kind, generation)
  evaluators/
    evaluate.py                # Main entry point (supports output-only for generation)
    schema_validator.py        # Output schema checks (all 5 Kyverno 1.16+ kinds)
    intent_validator.py        # Per-track intent: vpol, mpol, gpol, ivpol, dpol
    semantic_validator.py      # Kyverno CLI test runner
    diff_scorer.py             # Structural similarity scoring (0.0-1.0)
    input_validators/          # Per-track input validation
      cluster_policy.py
      gatekeeper.py
      opa.py
      sentinel.py
      cleanup.py
  results/                     # Per-run JSON results (gitignored)
    examples/                  # Golden sample results (committed)
  reports/
    generate.py                # Markdown + HTML report generator
    templates/                 # Jinja2 templates
    output/                    # Generated reports
  blind-eval/
    anonymize.py               # Strip tool identity for blind judging
    judge_form.html            # Local scoring form
    reveal.py                  # Merge human scores with tool mapping
  kyverno-tests/               # Kyverno CLI semantic test suites
  input/                       # Legacy input dir (kept for compat)
  output/                      # Conversion outputs, organized by tool
```

---

## Running the Benchmark

### Prerequisites

| Tool | Purpose | Required? |
|------|---------|-----------|
| Python 3.9+ | Run benchmark | Yes |
| PyYAML | YAML parsing | Yes (`pip install pyyaml`) |
| Jinja2 | Report templates | Recommended (`pip install jinja2`) |
| `nctl` CLI | NPA conversion (subject) | If benchmarking nctl |
| `claude` CLI | Claude Code conversion (subject) | If benchmarking Claude |
| `cursor` CLI | Cursor Agent conversion (subject) | If benchmarking Cursor |
| `ANTHROPIC_API_KEY` | Fallback for Claude (API mode) | If no `claude` CLI |
| Kyverno CLI | Semantic validation | Optional (runs locally, no cluster needed) |
| kubectl | Schema dry-run | Optional |

### Runner architecture

Each tool being benchmarked has a **runner harness** in `runners/`. The harness is not the tool — it wraps the tool as the subject of the benchmark and produces standardized metrics:

```
  harness sends prompt  -->  tool runs  -->  harness captures output
       |                                          |
       +--- measures wall-clock time              +--- extracts YAML
       +--- estimates/reads token counts          +--- checks file written
       +--- computes cost                         +--- returns RunResult
```

| Runner | CLI command | Token source | Cost source |
|--------|------------|-------------|-------------|
| **nctl** | `nctl ai --prompt "..." --skip-permission-checks` | Estimated (~3.8 chars/token) | Estimated (Claude Sonnet rates) |
| **claude** | `claude -p "..." --output-format json --allowedTools Read,Write,Shell` | Real (from JSON output) | Real (model pricing table) |
| **cursor** | `cursor -p "..." --force --output-format json` | Real if exposed, else estimated | Estimated (Claude Sonnet rates) |

When the CLI is not installed, Claude falls back to the Anthropic Messages API; Cursor falls back to a manual mode (prints prompt, polls for the output file). Token estimates are flagged with `"tokens_estimated": true` in the results JSON so reports can distinguish real from estimated counts.

### Full benchmark (all tools, all policies)

```bash
python3 benchmark.py
```

### Filter by tool, track, difficulty, task type, or output kind

```bash
# All policies with nctl
python3 benchmark.py --tool nctl

# Only conversion tasks
python3 benchmark.py --tool nctl --task-type convert

# Only generation tasks with Claude
python3 benchmark.py --tool claude --task-type generate

# Only easy ValidatingPolicy tasks
python3 benchmark.py --tool nctl --output-kind ValidatingPolicy --difficulty easy

# Only MutatingPolicy conversions
python3 benchmark.py --tool claude --output-kind MutatingPolicy --task-type convert

# Single policy by ID
python3 benchmark.py --policy-id gk_required_labels --tool nctl

# Stress tests only
python3 benchmark.py --difficulty stress --tool nctl
```

### Skip semantic tests or kubectl

```bash
python3 benchmark.py --skip-kyverno-test --no-kubectl
```

---

## Evaluation

Each policy is evaluated based on its task type:

### Conversion tasks (4 dimensions)

1. **Schema** — valid YAML, correct `kind` (e.g. `ValidatingPolicy`, `MutatingPolicy`), correct `apiVersion` (`policies.kyverno.io/`). Optional `kubectl --dry-run=client`.
2. **Intent** — preserves the source policy's target resource kinds and enforcement action. Per-track + per-output-kind logic (handles validate, mutate, generate, imageVerify rules).
3. **Semantic** — Kyverno CLI `test` against sample resources (pass on compliant, fail on non-compliant). Runs locally, no cluster needed.
4. **Diff Score** — 0.0–1.0 structural similarity (target kinds overlap, rule count ratio, message overlap).

### Generation tasks (2 dimensions)

1. **Schema** — same as conversion.
2. **Semantic** — same as conversion (if kyverno-test exists).

Intent validation and diff scoring are **skipped** for generation tasks since there is no source policy to compare against.

---

## Results Schema

Each run produces a JSON file in `results/`:

```json
{
  "run_id": "run_20260319_143022_nctl_cp_require_resource_limits",
  "tool": "nctl",
  "policy_id": "cp_require_resource_limits",
  "track": "cluster-policy",
  "task_type": "convert",
  "difficulty": "easy",
  "expected_output_kind": "ValidatingPolicy",
  "timestamp": "2026-03-19T14:30:22Z",
  "success": true,
  "conversion_time_seconds": 2.3,
  "input_tokens": 850,
  "output_tokens": 1200,
  "total_tokens": 2050,
  "cost_usd": 0.002,
  "tokens_estimated": true,
  "model": "nctl-builtin",
  "schema_pass": true,
  "intent_pass": true,
  "semantic_pass": true,
  "semantic_skipped": false,
  "diff_score": 0.92,
  "schema_errors": [],
  "intent_errors": [],
  "semantic_errors": []
}
```

For **generation** tasks, `intent_pass` is `null` and `diff_score` is `null`.

### Console output

```
  Tool       Policy                              Type      Kind                 Diff Schema  Intent  Semantic  Time(s)
  ----------------------------------------------------------------------------------------------------
  nctl       cp_require_labels                   convert   ValidatingPolicy     0.95    PASS    PASS      SKIP      8.2
  nctl       cp_add_default_labels               convert   MutatingPolicy       0.88    PASS    PASS      SKIP     12.1
  nctl       gen_require_labels                  generate  ValidatingPolicy        -    PASS       —      SKIP      6.1
  nctl       gen_create_networkpolicy            generate  GeneratingPolicy        -    PASS       —      SKIP     11.3

  Summary: Schema: 38/41 | Intent: 24/28 (convert only) | Semantic: 0/0 | Avg: 10.2s
```

---

## Report Generation

```bash
# Generate both Markdown and HTML (loads all results/*.json that qualify)
python3 reports/generate.py

# Only use specific result files (avoid mixing ad-hoc runs)
python3 reports/generate.py --from-results benchmark_demo_conversion_generation.json

# Or via the main orchestrator (same as loading all of results/*.json)
python3 benchmark.py --report

# Markdown only
python3 reports/generate.py --format markdown

# HTML dashboard only
python3 reports/generate.py --format html
```

Outputs go to **`reports/output/report.md`** and **`reports/output/dashboard.html`**.

**Markdown report** includes: leaderboard, **conversion vs generation (per-task-type)**, per-output-kind, per-difficulty, per-tool, per-track, failures (tagged with `convert` / `generate`).

**HTML dashboard** includes: interactive charts (Chart.js), **“By task type”** summary table, filterable results (**tool / task type / track**), columns for **Task** (`convert`|`generate`) and **Output kind**, leaderboard with composite scores.

An illustrative combined run (8 conversion + 6 generation rows across three tools) lives at **`results/benchmark_demo_conversion_generation.json`**; generate from it with `--from-results` as above to preview the report without running the full benchmark.

### Leaderboard scoring

Tools are ranked by a composite score (configurable weights in `config.yaml`):

```
composite = 0.5 * pass_rate + 0.2 * (1 - normalized_time) + 0.2 * diff_score + 0.1 * (1 - normalized_cost)
```

---

## Blind Evaluation

To remove bias when comparing tools:

```bash
# 1. Anonymize outputs (strips tool identity, assigns random IDs)
python3 blind-eval/anonymize.py

# 2. Open the judge form in a browser
open blind-eval/judge_form.html
# Load files from blind-eval/anonymized/, score each on 1-5 scale, export scores.json

# 3. Reveal identities and merge scores
python3 blind-eval/reveal.py --scores blind-eval/scores.json
```

---

## Stress Testing

The `dataset/stress/` directory contains intentionally broken or edge-case policies:

- `malformed-yaml.yaml` — broken YAML syntax
- `missing-spec.yaml` — valid YAML, no `spec`
- `empty-rules.yaml` — spec with empty rules list

Run stress tests to see how tools handle bad input:

```bash
python3 benchmark.py --difficulty stress --tool nctl
```

Stress policies have `expect_failure: true` in the index; the benchmark evaluates whether the tool fails gracefully or hallucinates output.

---

## Iterative Improvement

Allow tools multiple attempts to fix their own errors:

```bash
python3 benchmark.py --tool claude --max-attempts 3
```

On failure, the benchmark re-runs with an augmented prompt that includes the previous errors. Each attempt is recorded separately (`"attempt": 1`, `"attempt": 2`, ...) so reports can show the improvement curve.

---

## Legacy CLI (backward compatible)

The `validate.py` CLI supports both conversion and generation validation:

```bash
# Validate input only
python3 validate.py --input input/require-resource-limits.yaml

# Validate conversion (input + output)
python3 validate.py --input input/require-resource-limits.yaml \
  --output output/converted.yaml --tool nctl

# Validate generated policy (output only — no source to compare)
python3 validate.py --output output/generated-vpol.yaml --tool claude

# With expected kind check
python3 validate.py --output output/mutating.yaml --tool nctl --expected-kind MutatingPolicy
```

The legacy `run-nctl-conversion.sh` script also still works.

---

## Transparency Statement

This benchmark is designed to be **credible and fair**:

- **Open dataset** — ClusterPolicy fixtures are pinned copies of [kyverno/policies](https://github.com/kyverno/policies) (see manifest + sync script); other tracks and generation tasks are defined in this repo.
- **Reproducible** — same prompts, same evaluation, same scoring for every tool.
- **Failures shown** — results include failures, errors, and partial outputs. We don't hide bad runs.
- **Raw data published** — every run produces a JSON file with full details. Nothing is aggregated away.
- **Blind evaluation** — human judging with anonymized outputs removes tool-name bias.
- **No special treatment** — nctl uses the same prompts and evaluation as Cursor and Claude. If Nirmata MCP servers or skills are active in your IDE, disable them when benchmarking other tools (see the blind-eval workflow).

If you find issues with the evaluation methodology, open an issue.

---

## Contributing

### Add a new ClusterPolicy from kyverno/policies

1. Add a block to `dataset/kyverno-upstream-manifest.yaml` with `id`, `upstream_path` (under [kyverno/policies](https://github.com/kyverno/policies)), and `sync_test: true|false`.
2. Add a matching entry to `dataset/index.yaml` with `path: imported/kyverno-policies/<id>.yaml` and, if tests were synced, `kyverno_test_dir: imported/kyverno-tests/<id>`.
3. Run `python3 scripts/sync_kyverno_policies.py` and verify the files appear under `dataset/imported/`.

### Add a new policy maintained in this repo

1. Add the policy file under `dataset/local/` or the appropriate `dataset/<track>/` directory (Gatekeeper, OPA, Sentinel, cleanup, stress).
2. Add an entry to `dataset/index.yaml` with a unique `id`, `path` relative to `dataset/`, `track`, `task_type`, `difficulty`, `expected_output_kind`, and `description`.
3. (Optional) Add a Kyverno CLI test directory and set `kyverno_test_dir` (relative to `dataset/`).

### Add a generation task

1. Add an entry to `dataset/index.yaml` with `task_type: generate` and no `path`.
2. Write the `description` as the natural-language requirement (it becomes the prompt body).
3. (Optional) Add a Kyverno CLI test suite to validate the generated policy.

### Add a new tool

1. Create `runners/<tool>_runner.py` implementing the `ToolRunner` interface from `runners/base.py`.
2. Add the tool to `config.yaml` under `tools:`.
3. Run `python3 benchmark.py --tool <tool>` to test.

### Add a new conversion track

1. Create an input validator in `evaluators/input_validators/<track>.py`.
2. Add intent validation logic to `evaluators/intent_validator.py`.
3. Add a prompt template to `runners/prompts.py`.
4. Add the track to `config.yaml` under `tracks:`.
5. Add sample policies to `dataset/<track>/` and update `dataset/index.yaml`.

---

## License

See [LICENSE](LICENSE) in this repo.
