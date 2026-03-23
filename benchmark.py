#!/usr/bin/env python3
"""
Policy Conversion Benchmark — main orchestrator.

  dataset → run tools → collect outputs → evaluate → store JSON → generate report

Supports two task types:
  - **convert** — source policy → converted output (schema + intent + semantic + diff)
  - **generate** — natural-language prompt → new policy (schema + semantic only)

Usage:
  python3 benchmark.py                                   # all tools, all policies
  python3 benchmark.py --tool nctl                       # nctl only
  python3 benchmark.py --tool claude --track opa         # Claude on OPA track
  python3 benchmark.py --policy-id cp_require_resource_limits --tool nctl
  python3 benchmark.py --tool nctl --max-attempts 3      # iterative improvement
  python3 benchmark.py --difficulty stress               # stress tests only
  python3 benchmark.py --task-type generate              # generation tasks only
  python3 benchmark.py --output-kind MutatingPolicy      # filter by target kind
  python3 benchmark.py --report                          # generate report from existing results
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from evaluators.evaluate import evaluate, validate_input
from runners.base import RunResult, ToolRunner
from runners.prompts import build_prompt

REPO_ROOT = Path(__file__).resolve().parent


def _load_config() -> dict:
    cfg_path = REPO_ROOT / "config.yaml"
    if not cfg_path.exists():
        print(f"Error: {cfg_path} not found", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


def _load_dataset() -> list[dict]:
    idx_path = REPO_ROOT / "dataset" / "index.yaml"
    if not idx_path.exists():
        print(f"Error: {idx_path} not found", file=sys.stderr)
        sys.exit(1)
    data = yaml.safe_load(idx_path.read_text(encoding="utf-8"))
    return data.get("policies") or []


def _get_runner(tool_name: str, *, tool_script: str | None = None) -> ToolRunner:
    """Resolve the runner for a tool.

    Resolution order:
      1. Explicit --tool-script path
      2. Convention: run_tool_<name>.sh in repo root
      3. Built-in Python runner (nctl, claude, cursor)
    """
    from runners.script_runner import ScriptRunner

    # Explicit script path
    if tool_script:
        script_path = Path(tool_script)
        if not script_path.is_file():
            raise FileNotFoundError(f"Tool script not found: {tool_script}")
        return ScriptRunner(tool_name, script_path)

    # Convention-based script discovery
    script_path = REPO_ROOT / f"run_tool_{tool_name}.sh"
    if script_path.is_file():
        return ScriptRunner(tool_name, script_path)

    # Built-in Python runners
    if tool_name == "nctl":
        from runners.nctl_runner import NctlRunner
        return NctlRunner()
    if tool_name == "claude":
        from runners.claude_runner import ClaudeRunner
        return ClaudeRunner()
    if tool_name == "cursor":
        from runners.cursor_runner import CursorRunner
        return CursorRunner()
    raise ValueError(
        f"Unknown tool: {tool_name!r}. No run_tool_{tool_name}.sh found "
        f"and no built-in runner exists. Create run_tool_{tool_name}.sh or "
        f"pass --tool-script."
    )


def _run_single(
    tool_name: str,
    tool_config: dict,
    policy: dict,
    *,
    max_attempts: int = 1,
    eval_config: dict | None = None,
    tool_script: str | None = None,
    run_number: int = 1,
    total_runs: int = 1,
) -> dict:
    """Run one (tool, policy) pair and return the results dict."""
    eval_config = eval_config or {}
    runner = _get_runner(tool_name, tool_script=tool_script)

    if not runner.is_available():
        return {
            "tool": tool_name,
            "policy_id": policy["id"],
            "track": policy["track"],
            "task_type": policy.get("task_type", "convert"),
            "success": False,
            "error": f"Tool {tool_name!r} is not available",
        }

    track = policy["track"]
    policy_id = policy["id"]
    task_type = policy.get("task_type", "convert")
    is_generate = task_type == "generate"
    expect_failure = policy.get("expect_failure", False)

    input_path: Path | None = None
    if not is_generate and policy.get("path"):
        input_path = REPO_ROOT / "dataset" / policy["path"]

    if not is_generate and input_path and not input_path.is_file():
        hint = ""
        rel = policy.get("path") or ""
        if rel.startswith("imported/"):
            hint = " Run: python3 scripts/sync_kyverno_policies.py (see dataset/imported/README.md)"
        return {
            "tool": tool_name,
            "policy_id": policy_id,
            "track": track,
            "task_type": task_type,
            "success": False,
            "error": f"Dataset file not found: {input_path}.{hint}",
        }

    # Validate input (skip for generation tasks and stress tests)
    if not is_generate and not expect_failure and input_path:
        in_pass, in_errors = validate_input(
            track, input_path, use_kubectl=eval_config.get("kubectl_dry_run", True),
        )
        if not in_pass:
            return {
                "tool": tool_name,
                "policy_id": policy_id,
                "track": track,
                "task_type": task_type,
                "success": False,
                "error": f"Input validation failed: {'; '.join(in_errors)}",
            }

    output_dir = REPO_ROOT / "output" / tool_name
    if total_runs > 1:
        output_path = output_dir / f"{policy_id}_run{run_number}.yaml"
    else:
        output_path = output_dir / f"{policy_id}.yaml"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    timeout = eval_config.get("timeout_seconds", 120)
    expected_kind = policy.get("expected_output_kind")
    last_result: dict = {}

    for attempt in range(1, max_attempts + 1):
        # Per-task prompt override takes precedence over template
        if policy.get("prompt"):
            prompt = policy["prompt"].format(
                input_path=input_path or "",
                output_path=output_path,
            )
        else:
            prompt = build_prompt(
                track,
                str(input_path) if input_path else None,
                str(output_path),
                output_kind=expected_kind,
                task_type=task_type,
                description=policy.get("description"),
            )

        # Augment prompt on retry with previous errors
        if attempt > 1 and (last_result.get("schema_errors") or last_result.get("intent_errors")):
            prev_errs = last_result.get("schema_errors", []) + last_result.get("intent_errors", [])
            prompt += (
                "\n\nThe previous attempt had these errors:\n"
                + "\n".join(f"- {e}" for e in prev_errs)
                + "\nPlease fix them."
            )

        run_result: RunResult = runner.run(
            input_path or output_path,
            output_path,
            prompt,
            timeout_seconds=timeout,
            config=tool_config,
        )

        # Evaluate
        kyverno_test_dir = None
        if policy.get("kyverno_test_dir"):
            # Paths are relative to dataset/ (same convention as policy path)
            kyverno_test_dir = REPO_ROOT / "dataset" / policy["kyverno_test_dir"]

        eval_result: dict = {}
        if run_result.success and output_path.exists():
            eval_result = evaluate(
                track,
                input_path,
                output_path,
                expected_output_kind=expected_kind,
                use_kubectl=eval_config.get("kubectl_dry_run", True),
                skip_kyverno_test=eval_config.get("skip_kyverno_test", False),
                kyverno_test_dir=kyverno_test_dir,
                task_type=task_type,
            )

        success = run_result.success and eval_result.get("schema_pass", False)

        timestamp_str = datetime.now(timezone.utc).isoformat()
        last_result = {
            "run_id": f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{tool_name}_{policy_id}_r{run_number}",
            "tool": tool_name,
            "policy_id": policy_id,
            "track": track,
            "task_type": task_type,
            "difficulty": policy.get("difficulty"),
            "expected_output_kind": expected_kind,
            "timestamp": timestamp_str,
            "success": success,
            "conversion_time_seconds": run_result.conversion_time_seconds,
            "input_tokens": run_result.input_tokens,
            "output_tokens": run_result.output_tokens,
            "total_tokens": run_result.total_tokens,
            "cost_usd": run_result.cost_usd,
            "tokens_estimated": run_result.tokens_estimated,
            "model": run_result.model,
            "tool_version": run_result.tool_version,
            "raw_output_path": str(output_path),
            "attempt": attempt,
            "max_attempts": max_attempts,
            **eval_result,
            "error": run_result.error if not run_result.success else None,
        }

        if success or attempt == max_attempts:
            break

    return last_result


def _print_summary(results: list[dict]) -> None:
    """Print a rich summary table to stdout."""
    print()
    hdr = (
        f"  {'Tool':<10} {'Policy':<35} {'Type':<9} {'Kind':<20} "
        f"{'Diff':>4} {'Schema':>7} {'Intent':>7} {'Semantic':>9} {'Time(s)':>8}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    schema_total = intent_total = semantic_total = 0
    schema_pass_n = intent_pass_n = semantic_pass_n = 0
    intent_applicable = 0

    for r in results:
        task_type = r.get("task_type", "convert")
        kind_short = (r.get("expected_output_kind") or "-")[:18]
        t = r.get("conversion_time_seconds")
        time_str = f"{t:.1f}" if t else "-"
        diff = r.get("diff_score")
        diff_str = f"{diff:.2f}" if diff is not None else "-"

        s_pass = r.get("schema_pass")
        i_pass = r.get("intent_pass")
        sem = r.get("semantic_pass")
        sem_skip = r.get("semantic_skipped", True)

        s_str = "PASS" if s_pass else ("FAIL" if s_pass is not None else "-")
        if i_pass is None:
            i_str = "\u2014"  # em-dash for generation tasks
        else:
            i_str = "PASS" if i_pass else "FAIL"
        if sem_skip:
            sem_str = "SKIP"
        elif sem is None:
            sem_str = "-"
        else:
            sem_str = "PASS" if sem else "FAIL"

        print(
            f"  {r['tool']:<10} {r['policy_id']:<35} {task_type:<9} {kind_short:<20} "
            f"{diff_str:>4} {s_str:>7} {i_str:>7} {sem_str:>9} {time_str:>8}"
        )

        schema_total += 1
        if s_pass:
            schema_pass_n += 1
        if i_pass is not None:
            intent_applicable += 1
            intent_total += 1
            if i_pass:
                intent_pass_n += 1
        if not sem_skip:
            semantic_total += 1
            if sem:
                semantic_pass_n += 1

    print()
    parts = [f"Schema: {schema_pass_n}/{schema_total}"]
    if intent_applicable:
        parts.append(f"Intent: {intent_pass_n}/{intent_total} (convert only)")
    if semantic_total:
        parts.append(f"Semantic: {semantic_pass_n}/{semantic_total}")
    times = [r.get("conversion_time_seconds") for r in results if r.get("conversion_time_seconds")]
    if times:
        parts.append(f"Avg: {sum(times)/len(times):.1f}s")
    print(f"  Summary: {' | '.join(parts)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Policy Conversion Benchmark")
    parser.add_argument("--tool", nargs="*", help="Tools to run (default: all enabled)")
    parser.add_argument("--track", help="Filter by conversion track")
    parser.add_argument("--policy-id", nargs="+", help="Run one or more policies by ID")
    parser.add_argument("--difficulty", help="Filter by difficulty (easy, medium, hard, stress)")
    parser.add_argument("--task-type", choices=["convert", "generate"], help="Filter by task type")
    parser.add_argument("--output-kind", help="Filter by expected output kind (e.g. MutatingPolicy)")
    parser.add_argument("--max-attempts", type=int, default=1, help="Max attempts per policy (iterative improvement)")
    parser.add_argument("--runs", type=int, default=1, help="Number of runs per (tool, policy) pair for averaging")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers per tool (default: 1 = sequential)")
    parser.add_argument("--tool-script", help="Path to tool runner script (overrides auto-detection from --tool)")
    parser.add_argument("--skip-kyverno-test", action="store_true")
    parser.add_argument("--no-kubectl", action="store_true")
    parser.add_argument("--report", action="store_true", help="Generate report from existing results (no runs)")
    args = parser.parse_args()

    config = _load_config()
    policies = _load_dataset()
    eval_config = config.get("evaluation", {})
    if args.skip_kyverno_test:
        eval_config["skip_kyverno_test"] = True
    if args.no_kubectl:
        eval_config["kubectl_dry_run"] = False

    if args.report:
        try:
            from reports.generate import generate_all
            generate_all()
        except ImportError:
            print("Error: reports module not found", file=sys.stderr)
            return 1
        return 0

    # Filter policies
    if args.policy_id:
        policies = [p for p in policies if p["id"] in args.policy_id]
    if args.track:
        policies = [p for p in policies if p["track"] == args.track]
    if args.difficulty:
        policies = [p for p in policies if p.get("difficulty") == args.difficulty]
    if args.task_type:
        policies = [p for p in policies if p.get("task_type", "convert") == args.task_type]
    if args.output_kind:
        policies = [p for p in policies if p.get("expected_output_kind") == args.output_kind]

    if not policies:
        print("No policies match the given filters.", file=sys.stderr)
        return 1

    # Determine tools
    tool_configs = config.get("tools", {})
    if args.tool:
        tools_to_run = args.tool
    else:
        tools_to_run = [
            name for name, cfg in tool_configs.items() if cfg.get("enabled", True)
        ]

    results_dir = REPO_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[dict] = []

    num_runs = args.runs
    num_workers = args.workers

    for tool_name in tools_to_run:
        tcfg = tool_configs.get(tool_name, {})
        runs_label = f" (x{num_runs})" if num_runs > 1 else ""
        workers_label = f", {num_workers} workers" if num_workers > 1 else ""
        print(f"\n--- Running {tool_name}{runs_label}{workers_label} ---")

        # Build list of all (policy, run_number) jobs for this tool
        jobs: list[tuple[dict, int]] = []
        for policy in policies:
            for run_num in range(1, num_runs + 1):
                jobs.append((policy, run_num))

        def _execute_job(job: tuple[dict, int]) -> dict:
            policy, run_num = job
            result = _run_single(
                tool_name,
                tcfg,
                policy,
                max_attempts=args.max_attempts,
                eval_config=eval_config,
                tool_script=args.tool_script,
                run_number=run_num,
                total_runs=num_runs,
            )
            result["run_number"] = run_num
            result["total_runs"] = num_runs
            return result

        if num_workers <= 1:
            # Sequential execution (original behavior)
            for policy, run_num in jobs:
                task_type = policy.get("task_type", "convert")
                label = f"{policy['id']} ({task_type}/{policy['track']})"
                run_tag = f" run {run_num}/{num_runs}" if num_runs > 1 else ""
                print(f"  [{tool_name}] {label}{run_tag} ...", end=" ", flush=True)

                result = _execute_job((policy, run_num))
                all_results.append(result)

                status = "PASS" if result.get("success") else "FAIL"
                print(status)

                run_id = result.get("run_id", f"run_{tool_name}_{policy['id']}")
                out_json = results_dir / f"{run_id}.json"
                out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        else:
            # Parallel execution
            completed = 0
            total = len(jobs)
            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                futures = {pool.submit(_execute_job, job): job for job in jobs}
                for future in as_completed(futures):
                    policy, run_num = futures[future]
                    completed += 1
                    task_type = policy.get("task_type", "convert")
                    label = f"{policy['id']} ({task_type}/{policy['track']})"
                    run_tag = f" r{run_num}" if num_runs > 1 else ""

                    result = future.result()
                    all_results.append(result)

                    status = "PASS" if result.get("success") else "FAIL"
                    print(f"  [{completed}/{total}] [{tool_name}] {label}{run_tag} ... {status}")

                    run_id = result.get("run_id", f"run_{tool_name}_{policy['id']}")
                    out_json = results_dir / f"{run_id}.json"
                    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

    _print_summary(all_results)

    # Write aggregated results
    agg_path = results_dir / f"benchmark_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    agg_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"  Aggregated results: {agg_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
