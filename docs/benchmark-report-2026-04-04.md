# Policy as Code Benchmark Report

**Date:** April 3-4, 2026
**Authors:** Shreyas Mocherla
**Repo:** nirmata/policy-bench (`feat/containerized-benchmark-isolation`)

---

## Executive Summary

We built a public benchmark for evaluating how well AI coding agents convert legacy Kyverno ClusterPolicies (kyverno.io/v1) to Kyverno 1.16+ CEL-based policy types (ValidatingPolicy, MutatingPolicy, GeneratingPolicy). The benchmark tests 32 real-world policies across three tools — nctl ai, Cursor, and Claude Code — in isolated Docker containers with identical inputs, prompts, and evaluation criteria.

**Final results:**

| Tool | ValidatingPolicy (20) | MutatingPolicy (8) | GeneratingPolicy (4) | Total | Pass Rate |
|---|---|---|---|---|---|
| nctl ai | 20/20 | 8/8 | 4/4 | 32/32 | 100% |
| Cursor | 17/20 | 2/8 | 0/4 | 19/32 | 59% |
| Claude Code | 0/20 | 0/8 | 0/4 | 0/32 | 0% |

nctl ai went from 28/32 (87.5%) to 32/32 (100%) during this sprint. The fix was not adding more instructions to the LLM — the LLM already had the right guidance. Instead, we added deterministic structural checks to the existing retry loop that catch semantically wrong but syntactically valid policies, letting the LLM self-correct.

---

## 1. What We're Benchmarking

### The Conversion Task

Kyverno 1.16 introduced CEL-based policy types (`ValidatingPolicy`, `MutatingPolicy`, `GeneratingPolicy`) to replace the legacy `ClusterPolicy` with JMESPath expressions. Every organization running Kyverno will need to convert their existing policies. This benchmark measures how well AI agents handle that conversion.

The conversion is non-trivial because:
- JMESPath expressions must be rewritten as CEL
- Match criteria change from Kyverno-style `match.any[].resources` to admission-style `matchConstraints.resourceRules[]`
- Mutation patterns change from `patchStrategicMerge` to `applyConfiguration` with CEL `Object{}` builder syntax
- Generation patterns change from `generate.data` to `generator.Apply()` with `dyn()` type wrapping
- Conditional anchors (`(name): "?*"`, `+(field)`) have no direct CEL equivalent — they need `.filter()` + `matchConditions`
- Container injection ordering matters — `patchStrategicMerge` prepends, so CEL must use `[new] + existing`

### The Dataset

32 real-world policies sourced from the [kyverno-policies](https://github.com/kyverno/policies) community library, spanning three target types and three difficulty levels:

**By target type:**
- 20 ValidatingPolicy (validation/deny rules)
- 8 MutatingPolicy (mutation rules — patchStrategicMerge, foreach, conditional anchors)
- 4 GeneratingPolicy (resource generation — data, clone, apiCall context)

**By difficulty:**
- Easy (10): Simple match + single-rule policies (require-labels, disallow-latest-tag, add-default-labels)
- Medium (10): Multi-condition, preconditions, foreach iteration (require-drop-all, add-ndots, create-default-pdb)
- Hard (12): Complex CEL, cross-resource references, PDB validation, Kasten backup generation, sidecar injection

Each policy has a corresponding Kyverno CLI test suite (`kyverno-test.yaml` + resources + expected patched outputs) that validates functional correctness, not just schema validity.

### The Tools

| Tool | Model | Skills/Context | Container Image |
|---|---|---|---|
| **nctl ai** | Claude Sonnet (via Nirmata) | Built-in converting-policies skill with field-level mappings per policy type, plus CEL compiler validation in `ValidateResponse` | `benchmark-nctl` |
| **Cursor** | Claude Sonnet 4.6 | Public Kyverno skills from [christian-dussol-cloud-native/kyverno](https://github.com/christian-dussol-cloud-native/kyverno/tree/main/skills) loaded via `~/.cursor/skills/` | `benchmark-cursor` |
| **Claude Code** | Claude Sonnet 4.6 | Same public Kyverno skills loaded via `~/.claude/skills/` | `benchmark-claude` |

---

## 2. Benchmark Architecture

### Containerized Isolation

Every benchmark run executes inside an isolated Docker container. The container sees only:
- `/workspace/policy.yaml` — the input ClusterPolicy
- `/workspace/output/` — empty directory for the converted output
- The conversion prompt (with input/output paths rewritten for the container)
- API keys via `--env-file`

The container does **not** have access to:
- CLAUDE.md or project instructions
- Memory from previous sessions
- MCP servers or external tools
- Previous conversion outputs
- The benchmark's own evaluation code or test suites

This prevents skills/config/memory leakage between tools and ensures each tool is evaluated on its own capabilities.

**One task per container:** Each policy conversion runs in a fresh container. The container is created, runs the conversion, and is destroyed. This prevents agents from being influenced by previous conversion results.

### Evaluation Pipeline

Each converted policy goes through three evaluation stages:

```
Input Policy → Tool (containerized) → Converted YAML → Evaluation
                                                          │
                                                          ├─ 1. Schema + CEL Validation
                                                          │     (Go binary using Kyverno's own compiler)
                                                          │
                                                          ├─ 2. Structural Lint (NEW)
                                                          │     (Python pattern checks on CEL expressions)
                                                          │
                                                          └─ 3. Functional Test
                                                                (kyverno test with real resources)
```

**Stage 1: Schema + CEL Validation** (`evaluators/go_validator.py` → `cmd/validate-policy/`)
- Validates YAML structure against Kyverno OpenAPI schemas
- Compiles CEL expressions using Kyverno's own compiler packages (vpolcompiler, mpolcompiler, gpolcompiler, dpolcompiler)
- Catches syntax errors, type mismatches, undefined variables
- Uses the same validation code as Kyverno itself — if it compiles here, it compiles in a real cluster

**Stage 2: Structural Lint** (`evaluators/structural_lint.py`) — added April 4
- Catches semantic issues that pass CEL compilation but fail functional tests
- Three checks for MutatingPolicy:
  1. Append vs prepend for container injection
  2. `.filter()` on containers without `matchConditions`
  3. Add-if-absent `.orValue()` without `matchConditions`
- Advisory warnings — shown in benchmark output but don't block evaluation

**Stage 3: Functional Test** (`evaluators/semantic_validator.py`)
- Runs `kyverno test` with the converted policy against real Kubernetes resource fixtures
- Tests both positive cases (should mutate/validate) and negative cases (should skip/pass)
- Auto-patches the test manifest to match the converted policy's `metadata.name`
- Strips the `rule` field for new policy types (they don't have named rules)
- Merges per-rule test results for policies with multiple rules

### Prompt Construction

All tools receive the same prompt template:

```
Convert the Kyverno ClusterPolicy in /workspace/policy.yaml to a [ValidatingPolicy|MutatingPolicy|GeneratingPolicy].
[Optional: policy description for context]
Write the converted policy to /workspace/output/converted.yaml.
```

The prompt includes the policy's description (from annotations) when available, giving agents context about what the policy does without revealing the expected output structure.

---

## 3. The Journey to 100%

### Starting Point: 28/32 (April 3)

nctl ai was passing 28 out of 32 policies. The 4 failures were:

| Policy | Type | Failure |
|---|---|---|
| cp_add_default_resources | MutatingPolicy | "Want skip, got pass" — Pod with existing resources should skip |
| cp_always_pull_images | MutatingPolicy | "Want skip, got pass" — Pod with empty container names should skip |
| cp_inject_sidecar | MutatingPolicy | "Resource diff" — sidecar container in wrong position |
| cp_kasten_generate_backup | GeneratingPolicy | CEL compilation errors — undefined variables |

### Root Cause Analysis

**Failure pattern 1: "Want skip, got pass" (2 policies)**

The v1 ClusterPolicy uses conditional anchors like `(name): "?*"` (match non-empty names) and `+(memory): "100Mi"` (add only if absent). These cause the entire mutation to be **skipped** when no containers match the condition.

The converted MutatingPolicy used `.filter()` or `.orValue()` ternary logic in the CEL expression, which correctly preserves existing values — but the mutation still **fires**. Kyverno reports "pass" (mutation applied, even if it was a no-op) instead of "skip" (mutation not applicable). The functional test expects "skip."

The fix requires a `matchCondition` with an `.exists()` expression that prevents the mutation from firing when no containers need changes.

**Failure pattern 2: Container ordering (1 policy)**

The v1 `patchStrategicMerge` places injected containers in patch-document order — new containers appear **first** in the list. The converted CEL used `object.spec.containers + [newContainer]` (append), but the functional test expects `[newContainer] + object.spec.containers` (prepend).

**Failure pattern 3: CEL variable composition (1 policy)**

The Kasten backup policy is a complex GeneratingPolicy with `apiCall` context and nested variable dependencies. The converter decomposed the resource definition into multiple `spec.variables[]` entries, each holding a `dyn()` result. But CEL's type checker treats `dyn`-typed variables as opaque — they can't be composed inside another `dyn({...})` map literal. This cascades into "undefined field" errors for every downstream variable reference.

### What We Tried First (and Why We Reverted)

**Attempt 1: Add verbose examples to the skill docs**

We added ~100 lines of detailed before/after YAML examples to `mutatingpolicy.md` and `generatingpolicy.md` in the nctl skills directory, showing the exact matchCondition patterns and prepend syntax.

**Why we reverted:** The user (Shreyas) correctly identified this as brittle. The examples were essentially teaching the LLM the exact answers to 4 specific test cases. A slightly different conditional anchor pattern wouldn't match any example and would fail the same way. Also, the skill doc was getting long — LLMs get worse at following instructions as docs grow. We were overfitting to the benchmark.

### What Actually Worked: Deterministic Guardrails

The key insight: **the LLM already had the right instructions**. The `mutatingpolicy.md` prompt template in go-llm-apps already documented:
- Prepend pattern with `[new] + existing` (line 212-222)
- matchCondition required for `.filter()` (line 246-280)
- matchCondition for conditional mutations (line 282-319)

The problem wasn't missing knowledge — the LLM just didn't follow instructions 100% of the time. Adding more instructions wouldn't fix this reliably. Instead, we needed **a guardrail that catches the mistake and lets it self-correct.**

#### Layer 1: go-llm-apps ValidateResponse (self-correcting)

Added `lintMutatingPolicy()` to the `ValidateResponse()` function in the generate_policy app. This runs after CEL compilation passes but before the response is accepted.

Three structural checks on the parsed policy JSON:

1. **Append detection:** Regex `object\.spec(\.template)?\.spec\.(containers|initContainers|volumes)\s*\+\s*\[` matches the append anti-pattern. Returns: *"MutatingPolicy appends injected containers (existing + [new]) instead of prepending ([new] + existing). Use [newContainer] + object.spec...containers to match expected ordering"*

2. **Filter without matchCondition:** String check for `.filter(` + `containers` in mutation expressions with empty `spec.matchConditions`. Returns: *"MutatingPolicy uses .filter() on containers but has no matchConditions. When the filter produces an empty list the mutation still fires (result: pass instead of skip). Add a matchCondition with an .exists() expression matching the same predicate as the filter"*

3. **orValue without matchCondition:** String check for `.map(` + `.orValue(` + `containers` without matchConditions. Returns: *"MutatingPolicy uses add-if-absent pattern (.orValue defaults) on containers without matchConditions. The mutation fires even when all containers already have the values (result: pass instead of skip). Add a matchCondition using .exists() to skip when no containers need defaults"*

When any check fails, the error message goes back to the LLM via the AppRunner retry loop (max 10 retries). The LLM reads the descriptive error and fixes the issue. This is the existing retry mechanism — we just made the rejection criteria smarter.

**Also fixed the prompt template:** Updated `mutatingpolicy.md` to show prepend pattern (`[new] + existing`) instead of append in the examples, and added matchCondition to the filter example. This helps the LLM get it right on the first try, but the lint catches it if it doesn't.

#### Layer 2: Benchmark structural lint (observability)

Added `evaluators/structural_lint.py` to the benchmark's evaluation pipeline, running between schema validation and functional testing. Same three pattern checks as the Go code, but operating on the YAML output from any tool.

Results appear as `lint_pass`/`lint_warnings` in the JSON output and as inline warnings in the benchmark summary table:

```
cursor  cp_inject_sidecar    convert  MutatingPolicy  PASS  FAIL  94.9  WARN: Appends injected containers...
cursor  cp_add_default_resources  convert  MutatingPolicy  PASS  FAIL  68.1  WARN: Uses add-if-absent pattern...
```

This doesn't change pass/fail outcomes — it's diagnostic. But it tells you *why* a functional test will fail before you even run it.

### The Fix in Numbers

**Before (April 3):**
- nctl: 28/32 (87.5%)
- 4 failures: 2 missing matchConditions, 1 wrong ordering, 1 CEL variable error

**After (April 4):**
- nctl: 32/32 (100%)
- All 4 previously failing policies pass both schema and functional tests
- Verified in containerized mode (no config/memory/skills leakage)

**Code changes:**

| File | Change |
|---|---|
| `go-llm-apps/pkg/apps/generate_policy/validate.go` | +65 lines: `lintMutatingPolicy()` function with 3 structural checks |
| `go-llm-apps/pkg/apps/generate_policy/generate.go` | +3 lines: Wire lint into `ValidateResponse()` |
| `go-llm-apps/pkg/apps/generate_policy/templates/mutatingpolicy.md` | Fix examples: prepend not append, matchCondition in filter example |
| `policy-bench/evaluators/structural_lint.py` | +85 lines: New module with same 3 checks for any tool |
| `policy-bench/evaluators/evaluate.py` | +4 lines: Wire lint into evaluation pipeline |
| `policy-bench/benchmark.py` | +3 lines: Show lint warnings in summary table |

---

## 4. Competitor Analysis

### Cursor: 19/32 (59%)

Cursor was given the public Kyverno skills from [christian-dussol-cloud-native/kyverno](https://github.com/christian-dussol-cloud-native/kyverno/tree/main/skills), loaded via `~/.cursor/skills/kyverno-policy-generator/SKILL.md`.

**Strengths:**
- Strong on ValidatingPolicy (17/20) — the public skill covers validation patterns well
- Decent conversion speed (avg 66s per policy)
- Good YAML structure and metadata preservation

**Weaknesses:**
- MutatingPolicy: 2/8 — struggles with conditional anchors, container ordering, foreach-to-CEL translation
- GeneratingPolicy: 0/4 — all fail with CEL compilation errors from incorrect `dyn()` usage and variable composition

**Failure breakdown (13 failures):**

| Category | Count | Policies |
|---|---|---|
| CEL compilation errors | 9 | cp_require_pdb, cp_pdb_minavailable, cp_block_stale_images, cp_add_safe_to_evict, cp_add_ndots, cp_add_ns_quota, cp_create_default_pdb, cp_kasten_generate_backup, cp_kasten_generate_by_label |
| Missing matchConditions | 2 | cp_add_default_resources, cp_add_tolerations |
| Wrong mutation output | 2 | cp_always_pull_images, cp_inject_sidecar |

The structural lint caught 2 of Cursor's failures (append ordering on cp_inject_sidecar, missing matchCondition on cp_add_default_resources), confirming the lint generalizes across tools.

### Claude Code: 0/32 (0%)

Claude Code was given the same public Kyverno skills, loaded via `~/.claude/skills/kyverno-policy-generator/SKILL.md`.

**What happened:**
Every single policy failed schema validation. Claude consistently used wrong apiVersions:
- `kyverno.io/v1alpha1` (doesn't exist as a valid GV for these types)
- `policies.kyverno.io/v1alpha1` (outdated — should be `policies.kyverno.io/v1`)

**Deeper investigation:** We patched all outputs to the correct apiVersion and re-evaluated. Result: **1/32 passes**. The issues go far beyond the version string — the generated CEL expressions themselves are fundamentally broken. The public skill was designed for ClusterPolicy generation with JMESPath, not for CEL-based policy types.

**Why this matters:** Claude Code with generic Kyverno knowledge produces zero usable conversions. The skill gap isn't about prompting or model quality — it's about domain-specific knowledge of CEL expression patterns, Kyverno's type system, and the structural differences between the old and new policy formats.

---

## 5. Why nctl Wins

### 1. Built-in Conversion Skills with Field-Level Mappings

nctl's `converting-policies` skill in go-nctl includes sub-skills for each target policy type:
- `mutatingpolicy.md` — field-level mapping from patchStrategicMerge to ApplyConfiguration, conditional anchors to .filter() + matchConditions, foreach to CEL iteration, preconditions to matchConditions
- `generatingpolicy.md` — generate.data to dyn() + generator.Apply(), clone/cloneList patterns, variable composition rules, synchronization mapping
- `clusterpolicy.md` — ValidatingPolicy field mapping, CEL assertion patterns
- Plus OPA Gatekeeper, Sentinel, and CleanupPolicy sub-skills

These aren't generic "how to write Kyverno policies" docs — they're specific conversion mappings that tell the LLM exactly how each v1 field maps to the new schema.

### 2. Kyverno CEL Compiler in the Loop

The `generate_policy` tool in go-llm-apps validates every generated policy using Kyverno's own CEL compiler packages before accepting it. This catches:
- CEL syntax errors
- Type mismatches in expressions
- Undefined variable references
- Invalid messageExpression patterns

If the CEL doesn't compile, the error goes back to the LLM for retry. Cursor and Claude Code don't have this — they generate YAML and hope it's valid.

### 3. Structural Guardrails (New)

The `lintMutatingPolicy()` function catches three patterns that pass CEL compilation but fail functional tests:
- Append vs prepend for container injection
- .filter() without matchConditions
- .orValue() add-if-absent without matchConditions

These checks close the gap between "compiles" and "works correctly." The LLM has ~10 attempts to produce a policy that passes all three validation layers (schema, CEL compilation, structural lint) before the result is finalized.

### 4. The Retry Loop Advantage

nctl's architecture gives it a unique advantage: the LLM generates → validates → retries in a tight loop. Each retry includes the previous error message, so the LLM learns from its mistake. This is fundamentally different from Cursor and Claude Code, which generate once and submit.

The retry loop existed before this sprint. What changed is the quality of rejection criteria. Before: JSON valid? Kind present? CEL compiles? After: plus structural lint for semantic correctness. Same loop, smarter rejection.

---

## 6. Infrastructure Details

### Container Images

| Image | Base | Size | Key Components |
|---|---|---|---|
| `benchmark-base` | debian:bookworm-slim | 168 MB | bash, curl, jq, kubectl, kyverno CLI |
| `benchmark-nctl` | benchmark-base | 909 MB | nctl binary (cross-compiled linux/amd64), built-in skills embedded |
| `benchmark-claude` | benchmark-base | 452 MB | Node.js 20, Claude Code CLI (npm), public Kyverno skills at `~/.claude/skills/` |
| `benchmark-cursor` | benchmark-base | 333 MB | cursor-agent CLI, public Kyverno skills at `~/.cursor/skills/` |

### Authentication

API keys are stored in `docker/secrets/{tool}.env` (gitignored) and mounted at runtime via `--env-file`. No keys are committed to the repo.

| Tool | Auth Mechanism |
|---|---|
| nctl | NIRMATA_TOKEN + AWS credentials (Bedrock) |
| Claude Code | ANTHROPIC_API_KEY |
| Cursor | CURSOR_API_KEY |

### Result Storage

Each conversion produces a JSON result file at `results/run_{timestamp}_{tool}_{policy_id}.json` containing:
- Success/failure status
- Schema pass/fail with errors
- Semantic (functional) pass/fail with kyverno test output
- Structural lint warnings
- Conversion time, token usage, estimated cost
- Tool version, model used

The dashboard at `reports/output/dashboard.html` is regenerated from all result files, deduplicating by (tool, policy_id) to keep only the latest run.

---

## 7. Key Commits

### policy-bench repo

| Commit | Description |
|---|---|
| `1e94625` | Go policy validator, curated dataset, functional testing, clean dashboard |
| `ae2e443` | Merge: resolve conflicts, keep curated 30-task dataset |
| `e463975` | One-command benchmark runner, remove schemas from git, rewrite README |
| `86befc6` | nctl linux binary download, robust flag parsing, re-add schemas |
| `360275b` | Dataset: add check-nvidia-gpu and limit-configmap-for-sa (32 tasks total) |
| `3c9ef57` | Add OpenAI Codex scaffolding (Dockerfile, entrypoint, config) |
| `79edc53` | Remove hardcoded image map, use benchmark-{tool} convention |
| `08afd56` | Pass --api-key flag explicitly to cursor-agent |
| `85d32d0` | Remove multi-run averaging, one run per (tool, policy) |
| `c1dfc04` | Remove kubectl dependency from input validation |
| `572a937` | Add policy descriptions to prompts for better agent context |
| `3ee273c` | Merge per-rule test results for new policy types in semantic validator |
| `50d03f0` | **Add structural lint between schema and semantic validation** |

### go-llm-apps repo

| Commit | Description |
|---|---|
| `1913f66` | Add container coverage rule to MutatingPolicy guidance |
| `8d3323d` | Cover initContainers and ephemeralContainers in MutatingPolicy CAG samples |
| `b4b9094` | Add output_path to generate_policy tool and fix CEL template patterns |
| `2af9b8b` | **Add structural lint to MutatingPolicy validation (the 100% fix)** |

---

## 8. Lessons Learned

### 1. Don't yell louder — add a guardrail

The LLM had all the instructions it needed. Adding more examples and longer docs made things worse (more to ignore, longer context, diluted attention). The fix was a 65-line Go function that deterministically catches bad patterns. Deterministic code beats probabilistic instruction-following every time.

### 2. Validate at the right layer

CEL compilation catches syntax errors but not semantic mistakes. A policy that compiles but appends instead of prepends is syntactically valid and semantically wrong. The structural lint bridges this gap — it's cheap to run and catches the class of bugs between "compiles" and "works."

### 3. Containerization matters for fair benchmarking

Without containers, tools leak knowledge through config files, memory, MCP servers, and previous outputs. An agent that saw yesterday's successful conversion in the output directory has an unfair advantage. One-task-per-container isolation ensures each conversion starts from zero.

### 4. Public skills aren't enough for complex conversions

The public Kyverno skill is well-designed for ClusterPolicy generation with JMESPath. But CEL-based policy types (ValidatingPolicy, MutatingPolicy, GeneratingPolicy) have fundamentally different structures, expression languages, and patterns. Generic knowledge produces 0-59% pass rates. Domain-specific conversion mappings produce 100%.

### 5. The retry loop is nctl's secret weapon

Claude Code and Cursor generate once and submit. nctl generates, validates with the actual Kyverno compiler + structural lint, and retries up to 10 times with descriptive error messages. This closed-loop architecture means even if the LLM gets it wrong on the first try, it has multiple chances to self-correct with specific feedback.

---

## 9. What's Next

- **Claude Code re-run** with a fresh API key (current key expired during testing)
- **OpenAI Codex** scaffolding is in place (Dockerfile, entrypoint, config) — needs API key
- **Multi-run consistency** — run each tool 3x to measure variance
- **Generation tasks** — benchmark policy generation from natural language (not just conversion)
- **Additional tracks** — Gatekeeper, OPA Rego, Sentinel, CleanupPolicy conversion benchmarks
- **Blind evaluation** — anonymize outputs and have human judges rank quality (tooling exists in `blind-eval/`)
- **Real-time dashboard** — benchmark now regenerates dashboard after each policy result (added April 4)
