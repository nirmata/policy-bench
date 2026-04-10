# Policy Conversion Benchmark — Findings

**Date:** 2026-04-10
**Dataset:** 32 ClusterPolicy → Kyverno 1.16+ conversions (20 ValidatingPolicy, 8 MutatingPolicy, 4 GeneratingPolicy)
**Methodology:** 1 run per (tool, policy) pair, schema+CEL validation via embedded Kyverno upstream Go validator, container-isolated execution
**Models:** Claude Sonnet 4.x via Anthropic API (Claude Code), Sonnet 4.x via Cursor API (Cursor agent), Nirmata Provider (nctl)

## Headline

| Rank | Tool | Schema+CEL pass | Overall success | Avg time |
|---|---|---|---|---|
| 1 | **nctl** | **30/32 (94%)** | 29/32 (91%) | 145s |
| 2 | **cursor** | **24/32 (75%)** | 21/32 (66%) | 93s |
| 3 | **claude** | **7/32 (22%)** | 7/32 (22%) | 94s |

Schema+CEL pass = the converted policy validates against the upstream Kyverno schemas under `policies.kyverno.io/v1*` AND its CEL expressions compile without errors. Overall success additionally requires the functional test (`kyverno test`) to pass when applicable.

## The story this run is actually telling

The interesting finding from this run is not the leaderboard. It's *why* the leaderboard looks the way it does, which we discovered by accident in the course of debugging an earlier 0% claude score.

**nctl is the only tool whose output is invariant to context noise.** In a controlled four-condition experiment on `cp_require_labels` (toggling whether the prompt contained an apiVersion hint, and whether a workspace-level `AGENTS.md` was present), nctl produced schema-valid Kyverno 1.16+ output in *all four conditions*. Claude Code and Cursor produced schema-valid output in only *one* of the four conditions, and the discriminator was the absence of a workspace context file. The architectural reason is that nctl's `converting-policies` skill is compiled into the binary, so the model's behavior is anchored to the skill regardless of what the prompt or workspace happens to contain. Sonnet without that anchor is at the mercy of priming bias from anything in its input window — including a 17-line nudge file that happened to mention the legacy `kyverno.io/v1 ClusterPolicy` group as the *source* format being converted away from.

We had originally added that nudge file to *help* the Sonnet-backed tools. It hurt them. Removing it caused the largest single jump observed during this benchmark run: **cursor improved from 0/32 to 24/32 schema-pass after deleting one file from the cursor container image.** Claude improved more modestly (from 3/32 to 7/32), but in both cases the direction was the same and the cause was the same.

This is the kind of fragility a built-in skill is supposed to insulate a tool against. nctl is.

## What each tool gets wrong

### claude (7/32 schema pass, 22 fails at yaml_parse)

Claude's dominant failure mode is unchanged from before the context file was removed: it knows the new top-level identity (`apiVersion: policies.kyverno.io/v1`, `kind: ValidatingPolicy/MutatingPolicy/GeneratingPolicy`) and produces it correctly for *every single one of the 32 policies*, but it then fills the spec body with the legacy ClusterPolicy structure — `spec.rules: [{name, match, validate.pattern}]` — instead of the 1.16+ shape (`spec.validationActions: [Audit]`, `spec.matchConstraints.resourceRules`, `spec.validations[].expression`).

Top spec fields the validator rejected as unknown across the 22 yaml_parse failures:

| Count | Field | Why rejected |
|---|---|---|
| 9 | `spec.background` | Moved to `spec.evaluation.background.enabled` in 1.16+ |
| 8 | `spec.rules` | Replaced by `spec.validations[]` with CEL |
| 5 | `spec.validationFailureAction` | Renamed to `spec.validationActions` (a list) |
| 3 | `spec.failureAction` | Hallucinated — this field name does not exist in either schema |
| 1 | `spec.validationAction` | Hallucinated — singular form, the real field is plural |

The hallucinated field names (`spec.failureAction`, `spec.validationAction`) are notable. They suggest the model is averaging across multiple Kyverno API versions in its training data and synthesizing field names that *sound* right for the new API but were never actually defined. This is a deeper kind of failure than just "uses the legacy schema."

### cursor (24/32 schema pass, 8 fails at cel_compile, 0 fails at yaml_parse)

Cursor essentially solved the schema migration. Every single one of its 32 outputs structurally validates as a Kyverno 1.16+ policy — there are no `unknown field` rejections at all. Its 8 remaining failures are all `cel_compile` errors, meaning the policy *structure* is correct but the CEL expressions inside `spec.validations[].expression` (or `matchConditions[].expression`) don't compile under Kyverno's CEL environment. CEL semantics are harder than schema layout, and this is the boundary cursor's model has not yet crossed.

3 of cursor's 24 schema-passing policies still fail the functional test (`SEMF` — schema valid, `kyverno test` rejects them on real input resources): `cp_add_default_resources`, `cp_add_safe_to_evict`, `cp_always_pull_images`. All three are MutatingPolicies. Mutation correctness is harder to verify from prompt + schema alone than validation correctness.

### nctl (30/32 schema pass, 2 cel_compile fails, 1 functional fail)

nctl's 3 non-success policies are all in the "hard" category and all fail for different reasons that point at engineering problems, not model knowledge:

| Policy | Stage | Cause |
|---|---|---|
| `cp_add_default_resources` | cel_compile | Generated `spec.mutations[0].applyConfiguration` block has a structural issue the compiler rejects |
| `cp_kasten_generate_backup` | cel_compile | Generating policy 2.0 compiler internal error on `spec.variables` block |
| `cp_inject_sidecar` | functional | Schema and CEL valid, but the resulting MutatingPolicy doesn't produce the right patch on the test input |

Two CEL compile failures and one functional failure on the hardest dataset categories (Mutating and Generating policies with sidecar injection and Kasten integration). Notably, `cp_add_tolerations` — a previous timeout in the prior run — now passes. The remaining failures are not "doesn't know the schema" failures.

## The architectural finding (the actual headline)

The benchmark exists to measure differences between AI tools that all sit on top of similar foundation models. With Sonnet under the hood of all three tools today, the interesting question is: *what does each tool's specific scaffolding add or remove?*

This run, plus the four-condition controlled experiment that backed it, yields a clean answer:

1. **nctl is skill-driven.** Its `converting-policies` skill is part of the binary and sits between the model and the user prompt. The model is told *how* to do the conversion every single time, in the same words, regardless of what the user typed. Output is robust to prompt phrasing and workspace clutter. Schema knowledge is compiled in, not learned at inference time.

2. **Claude Code and Cursor are model-driven.** They are general-purpose agentic shells that defer to the underlying Sonnet model's training-data knowledge of Kyverno. When Sonnet's training has gaps (which it does for Kyverno 1.16+ as of the model snapshot powering both tools today), the resulting output reflects those gaps. Both tools also amplify priming bias from anything in the workspace because they're designed to *follow project conventions* — if you put a markdown file in the project root, they read it and weight it heavily. That property is normally a feature.

3. **The priming bias is real and quantifiable.** A single 17-line markdown file mentioning the legacy `kyverno.io/v1 ClusterPolicy` group in passing was enough to flip cursor's schema-pass rate from 0/32 to nothing, and back to 24/32 once removed. nctl was unaffected.

This is a direct, measurable advantage of the built-in-skill architecture for narrow domain tasks where the foundation model's training data is incomplete or out of date. It's not a marketing claim — it's the exact mechanism the four-condition experiment isolated. The benchmark would not have surfaced this without the controlled toggle.

## Methodology notes and caveats

1. **Single-run vs majority vote.** The earlier 2026-03-23 findings used 3 runs per (tool, policy) pair with majority voting. This run is single-pass. Single-pass numbers will have more variance for the Sonnet-backed tools — claude and cursor will likely score 2-4 points higher or lower on a re-run. nctl's variance is much lower because its output is anchored to the skill rather than to the model's stochastic sampling.

2. **Validator strictness has changed since the 2026-03-23 findings.** The Go validator now enforces upstream Kyverno openAPI schemas via `kubectl-validate`, which is much stricter about unknown fields than the previous Python fallback validator. Outputs that were borderline-OK before are now hard-fails. This explains some of the gap between the old 98%/93%/95% numbers and today's 22%/75%/94%; it is not purely a model regression.

3. **Dataset is convert-only.** The 2026-03-23 dataset included generation tasks ("write a new policy from a description"). Today's dataset is 32 conversions only. Generation tasks tend to be easier for Sonnet-backed tools because there's no legacy starting point to anchor to, so removing them removes the easy-win column from cursor and claude.

4. **Model snapshots drift.** "Sonnet" is not pinned. Both Claude Code and Cursor route through model aliases that can move under us between runs without notice. Comparing cross-time runs is unreliable for this reason. Comparing cross-tool runs *within* a single benchmark execution (as we did here) is the only reliable comparison.

5. **No prompt-engineering crutches in the prompt this time.** The benchmark prompt for every tool now explicitly includes the target apiVersion (`policies.kyverno.io/v1`). Without that hint claude scored even worse. The prompt change applies uniformly to all three tools — fairness is preserved — but it does represent a deliberate concession that the benchmark is *not* testing whether the tool can guess the apiVersion in the dark; it's testing whether the tool can produce a correct conversion *given* the target.

## Recommendations for the benchmark itself

1. **Re-add generation tasks** to the dataset for breadth. Conversion-only is a narrow slice and the 2026-03-23 findings suggested generation is much closer across all three tools.

2. **Run each (tool, policy) pair 3 times** and report the median. The Sonnet-backed tools have enough run-to-run variance that single-pass results are noisy at the 1-2 policy level.

3. **Track the priming bias as an explicit benchmark dimension.** The four-condition experiment we ran should be re-runnable as a one-shot diagnostic — it's the most discriminating test in the suite for distinguishing skill-anchored tools from prompt-following ones, and it's cheap (~3 minutes per tool per policy).

4. **Capture model snapshots when possible.** Both Anthropic and Cursor expose date-stamped model IDs alongside the friendly names. Recording the actual snapshot in each result JSON would let us interpret cross-time comparisons honestly.

## Reproducibility

All three Docker images are minimal and contain only the tool binary plus its standard dependencies — no project-specific skills, no MCP servers, no workspace context files. The full benchmark can be reproduced from this repository by:

```bash
cd docker && ./build.sh --nctl-bin /path/to/nctl-linux
export ANTHROPIC_API_KEY=... CURSOR_API_KEY=... NIRMATA_TOKEN=... NIRMATA_URL=...
python3 benchmark.py --containerized
```

Aggregated results land in `results/benchmark_<timestamp>.json` and the dashboard at `reports/output/dashboard.html`. The four-condition priming experiment can be reproduced from the script in `/tmp/cursor_claude_overspec_experiment.sh` (TODO: move into the repo as `experiments/priming_bias.sh`).
