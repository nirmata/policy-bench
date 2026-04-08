# Policy Conversion Benchmark

A **public, reproducible benchmark** for converting Kyverno ClusterPolicies to the new Kyverno 1.16+ policy types. Compares **nctl**, **Claude Code**, **Cursor**, or any other AI tool using identical inputs, bare-minimum prompts, containerized isolation, and rigorous validation.

## Leaderboard

Ranked by **pass rate only** — the percentage of tasks that pass all validation layers. No composite scores, no arbitrary weights. This matches how [SWE-bench](https://www.swebench.com/), [HumanEval](https://github.com/openai/human-eval), [Aider](https://aider.chat/docs/leaderboards/), and every other major AI coding benchmark ranks tools.

Speed and cost are reported as supplementary metrics, not factored into ranking. The HTML dashboard includes an accuracy-vs-cost scatter plot for Pareto frontier visualization.

## Quick Start

```bash
# One command does everything: builds tools, syncs dataset, runs benchmark, generates report
./run-benchmark.sh --tool nctl claude --containerized

# Run a single policy
./run-benchmark.sh --tool nctl claude --policy-id cp_require_labels --containerized

# Just regenerate the report from existing results
./run-benchmark.sh --report
```

The script handles dependency checks, Go validator compilation, Docker image builds, dataset sync, benchmark execution, and report generation — in that order.

### Prerequisites

| Tool | Purpose | Required? |
|------|---------|-----------|
| Python 3.9+ | Orchestration | Yes |
| Go 1.25+ | Build policy validator | Yes |
| Docker | Containerized isolation | Yes (for `--containerized`) |
| kyverno CLI | Functional testing | Recommended (`brew install kyverno`) |
| PyYAML + Jinja2 | Reports | `pip install -r requirements.txt` |

### API Keys

For containerized runs, place API keys in `docker/secrets/` (gitignored):

```bash
# docker/secrets/nctl.env
NIRMATA_TOKEN=your-token
NIRMATA_URL=https://your-instance.nirmata.co

# docker/secrets/claude.env
ANTHROPIC_API_KEY=sk-ant-...

# docker/secrets/cursor.env
CURSOR_API_KEY=crsr_...
```

## How It Works

```
./run-benchmark.sh --tool nctl claude --containerized
  |
  |  [1/6] Check dependencies (python3, go, docker, kyverno)
  |  [2/6] Build Go policy validator (schema + CEL compilation)
  |  [3/6] Sync upstream kyverno policies (pinned revision)
  |  [4/6] Build Docker images (one per tool, ephemeral containers)
  |  [5/6] Run benchmark (tools in parallel, one container per task)
  |  [6/6] Generate report (HTML dashboard + Markdown)
  |
  v
  reports/output/dashboard.html
```

### Containerized Isolation

Each agent runs in an ephemeral Docker container — no memory, no CLAUDE.md, no MCP servers. Public Kyverno skills are installed for domain knowledge parity with nctl's built-in skills. The container sees only:

- `/workspace/policy.yaml` — the single input policy
- `/workspace/output/` — empty directory to write the converted policy

After the container exits, the host extracts the output and validates it. The container is destroyed.

### Bare-Minimum Prompts

No version hints, no CEL instructions, no coaching:

> "Convert the Kyverno ClusterPolicy in /workspace/policy.yaml to a ValidatingPolicy. Write the converted policy to /workspace/output/converted.yaml."

This tests what the agent actually knows, not what we tell it.

## Dataset

32 curated tasks, **every one with upstream kyverno functional tests** from [kyverno/policies](https://github.com/kyverno/policies). No unverifiable tasks.

| Output Kind | Easy | Medium | Hard | Total |
|------------|------|--------|------|-------|
| ValidatingPolicy | 6 | 6 | 8 | **20** |
| MutatingPolicy | 3 | 3 | 2 | **8** |
| GeneratingPolicy | 1 | 2 | 1 | **4** |
| **Total** | **10** | **11** | **11** | **32** |

Tasks are defined in `dataset/index.yaml`. Policies are synced from upstream via `dataset/kyverno-upstream-manifest.yaml`.

## Validation

Three layers. If any fails, the task is a failure.

### 1. Schema + CEL (Go binary)

A standalone Go binary (`cmd/validate-policy/`) that imports Kyverno's actual CEL compilers and OpenAPI schemas — the same validation approach used by [go-llm-apps](https://github.com/nirmata/go-llm-apps) benchmarks.

- Validates YAML structure against OpenAPI v3 schemas
- Compiles every CEL expression through Kyverno's engine
- Catches invalid fields (e.g., `validationFailureAction` on a ValidatingPolicy)
- Catches invalid apiVersions (e.g., `kyverno.io/v1alpha1` instead of `policies.kyverno.io/v1beta1`)

### 2. Functional (kyverno test)

Runs `kyverno test` with upstream test resources — real "good" and "bad" Kubernetes resources that the policy should accept or reject.

- Proves the policy actually works, not just compiles
- Uses upstream test suites from [kyverno/policies](https://github.com/kyverno/policies)
- Automatically patches policy names and strips rule fields for new policy types

### 3. Expected Kind

Verifies the output kind matches what the dataset specifies (e.g., task says ValidatingPolicy, agent should not produce MutatingPolicy).

## Folder Layout

```
convert-policies/
  run-benchmark.sh               # One-command runner (builds, syncs, benchmarks, reports)
  benchmark.py                    # Main orchestrator
  config.yaml                     # Tool + track + evaluation settings
  validate.py                     # Standalone CLI validator
  cmd/validate-policy/            # Go binary: schema + CEL validation
  docker/                         # Containerized isolation
    Dockerfile.{base,nctl,claude,cursor}
    entrypoints/run-{nctl,claude,cursor}.sh
    secrets/                      # API keys (gitignored)
  dataset/
    index.yaml                    # 32 curated tasks with kyverno tests
    kyverno-upstream-manifest.yaml
    imported/                     # Synced from kyverno/policies
  runners/                        # Tool harnesses
    base.py                       # ToolRunner ABC + RunResult
    container_runner.py           # Docker isolation runner
    prompts.py                    # Bare-minimum prompt templates
    {nctl,claude,cursor}_runner.py
  evaluators/                     # Validation pipeline
    evaluate.py                   # Orchestrator: schema+CEL → functional
    go_validator.py               # Calls Go binary
    schema_validator.py           # Python fallback
    semantic_validator.py         # kyverno test runner
  reports/
    generate.py                   # Markdown + HTML dashboard
    templates/dashboard.html.j2
  results/                        # Per-run JSON (gitignored)
```

## Contributing

### Add a new tool

1. Create `runners/<tool>_runner.py` implementing `ToolRunner` from `runners/base.py`
2. Add a Dockerfile at `docker/Dockerfile.<tool>` and entrypoint at `docker/entrypoints/run-<tool>.sh`
3. Add the tool to `config.yaml`
4. Run `./run-benchmark.sh --tool <tool> --containerized`

### Add a new policy

1. Add to `dataset/kyverno-upstream-manifest.yaml` (must have `.kyverno-test/` upstream)
2. Add to `dataset/index.yaml` with `kyverno_test_dir`
3. Run `python3 scripts/sync_kyverno_policies.py`

## Transparency

- **Open dataset** — all policies from [kyverno/policies](https://github.com/kyverno/policies) at a pinned revision
- **Reproducible** — same prompts, same evaluation, same containerized environment
- **Failures shown** — raw JSON results include errors, no aggregation hiding
- **No special treatment** — nctl uses the same prompts and isolation as Claude and Cursor
- **Functional proof** — every task is validated with real test resources, not just schema checks

## License

See [LICENSE](LICENSE).
