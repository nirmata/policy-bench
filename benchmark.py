#!/usr/bin/env python3
"""
Policy as Code Benchmark — main orchestrator.

  dataset → run tools → collect outputs → evaluate → store JSON → generate report

Supports three task types:
  - **convert** — source policy → converted output (schema+CEL + functional test)
  - **generate** — natural-language prompt → new policy (schema+CEL + functional test)
  - **generate_test** — source policy → kyverno-test.yaml + resources.yaml (schema + kyverno test + coverage)

Usage:
  python3 benchmark.py                                   # all tools, all policies
  python3 benchmark.py --tool nctl                       # nctl only
  python3 benchmark.py --tool claude --track opa         # Claude on OPA track
  python3 benchmark.py --policy-id cp_require_resource_limits --tool nctl
  python3 benchmark.py --tool nctl --max-attempts 3      # iterative improvement
  python3 benchmark.py --difficulty stress               # stress tests only
  python3 benchmark.py --task-type generate              # generation tasks only
  python3 benchmark.py --task-type generate_test         # test-generation tasks only
  python3 benchmark.py --output-kind MutatingPolicy      # filter by target kind
  python3 benchmark.py --report                          # generate report from existing results
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
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
from evaluators.error_summariser import summarise_errors
from evaluators.testgen_validator import evaluate_testgen
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


def _get_runner(
    tool_name: str,
    *,
    tool_script: str | None = None,
    containerized: bool = False,
    persistent: bool = False,
) -> ToolRunner:
    """Resolve the runner for a tool.

    Resolution order:
      0. --containerized → ContainerRunner (Docker isolation)
      1. Explicit --tool-script path
      2. Convention: run_tool_<name>.sh in repo root
      3. Built-in Python runner (nctl, claude, cursor)
    """
    if containerized:
        from runners.container_runner import ContainerRunner
        return ContainerRunner(tool_name, persistent=persistent)

    from runners.script_runner import ScriptRunner

    # Explicit script path
    if tool_script:
        script_path = Path(tool_script).resolve()
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
    containerized: bool = False,
    persistent: bool = False,
    runner: ToolRunner | None = None,
) -> dict:
    """Run one (tool, policy) pair and return the results dict."""
    eval_config = eval_config or {}
    if runner is None:
        runner = _get_runner(tool_name, tool_script=tool_script, containerized=containerized, persistent=persistent)

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
    is_testgen = task_type == "generate_test"
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

    # Validate input (skip for generation/test-gen tasks and stress tests)
    if not is_generate and not is_testgen and not expect_failure and input_path:
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
    if is_testgen:
        output_path = output_dir / policy_id  # directory, not a file
        output_path.mkdir(parents=True, exist_ok=True)
        # Copy source policy into the output dir so kyverno test can reference
        # it as policies: [policy.yaml] — no ../.. path games needed.
        if input_path:
            shutil.copy2(input_path, output_path / "policy.yaml")
    else:
        output_path = output_dir / f"{policy_id}.yaml"
        output_path.parent.mkdir(parents=True, exist_ok=True)

    timeout = eval_config.get("timeout_seconds", 120)
    expected_kind = policy.get("expected_output_kind")
    last_result: dict = {}
    base_prompt: str | None = None

    include_docs = True

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
                include_docs=include_docs,
            )

        # Save base prompt on first attempt; reset on each retry
        if base_prompt is None:
            base_prompt = prompt
        else:
            prompt = base_prompt

        # Augment prompt on retry with previous errors (only latest attempt)
        if attempt > 1:
            prev_errs = (
                last_result.get("expected_kind_errors", [])
                + last_result.get("schema_errors", [])
            )
            if prev_errs:
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
        if is_testgen:
            if run_result.success and output_path.is_dir():
                eval_result = evaluate_testgen(
                    generated_dir=output_path,
                    source_policy=input_path or output_path / "policy.yaml",
                    oracle_dir=kyverno_test_dir,
                    timeout_sec=timeout,
                )
            elif kyverno_test_dir:
                # No output directory produced — penalise same as a failed run
                eval_result = {
                    "testgen_composite_pass": False,
                    "testgen_schema_pass": False,
                    "testgen_errors": ["No output directory produced by tool"],
                    "schema_pass": False,
                    "semantic_pass": False,
                    "semantic_skipped": False,
                    "semantic_errors": ["No output produced by tool"],
                }
        else:
            if run_result.success and output_path.exists():
                eval_result = evaluate(
                    track,
                    input_path,
                    output_path,
                    expected_output_kind=expected_kind,
                    skip_kyverno_test=eval_config.get("skip_kyverno_test", False),
                    kyverno_test_dir=kyverno_test_dir,
                    task_type=task_type,
                )
            # If tool failed to produce output but a functional test exists,
            # mark functional as failed (not skipped) — no output is worse than
            # wrong output and should count against the tool's score.
            if not eval_result and kyverno_test_dir:
                eval_result["semantic_pass"] = False
                eval_result["semantic_skipped"] = False
                eval_result["semantic_errors"] = ["No output produced by tool"]

        if is_testgen:
            success = run_result.success and eval_result.get("testgen_composite_pass", False)
        else:
            schema_ok = eval_result.get("schema_pass", False)
            semantic = eval_result.get("semantic_pass")
            semantic_skipped = eval_result.get("semantic_skipped", True)
            functional_ok = semantic_skipped or (semantic is True)
            success = run_result.success and schema_ok and functional_ok

        # For test-gen tasks the canonical output file is kyverno-test.yaml inside the dir.
        output_yaml_path = (output_path / "kyverno-test.yaml") if is_testgen else output_path

        # Include full YAML output on failure for diagnostics
        yaml_preview = None
        if not success:
            try:
                if output_yaml_path.exists():
                    yaml_preview = output_yaml_path.read_text(encoding="utf-8", errors="replace")[:10_000]
            except OSError:
                pass

        # Capture input snippet (first 100 lines of source policy)
        input_snippet = None
        if input_path and input_path.is_file():
            try:
                with input_path.open(encoding="utf-8", errors="replace") as fh:
                    input_snippet = "".join(itertools.islice(fh, 100))
            except OSError:
                pass

        # Capture generated output YAML
        output_yaml = None
        try:
            if output_yaml_path.exists():
                output_yaml = output_yaml_path.read_text(encoding="utf-8", errors="replace")[:10_000]
        except OSError:
            pass

        timestamp_str = datetime.now(timezone.utc).isoformat()
        last_result = {
            "run_id": f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{tool_name}_{policy_id}",
            "tool": tool_name,
            "policy_id": policy_id,
            "track": track,
            "task_type": task_type,
            "difficulty": policy.get("difficulty"),
            "expected_output_kind": expected_kind,
            "description": policy.get("description"),
            "prompt": prompt,
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
            "raw_output_path": str(output_yaml_path),
            "attempt": attempt,
            "max_attempts": max_attempts,
            **eval_result,
            "error": run_result.error if not run_result.success else None,
            "yaml_preview": yaml_preview,
            "input_snippet": input_snippet,
            "output_yaml": output_yaml,
            "raw_log": run_result.raw_log,
        }

        if success or attempt == max_attempts:
            break

    # Generate a human-friendly error summary for failed runs
    if not last_result.get("success", False):
        all_errors = (
            ([last_result["error"]] if last_result.get("error") else [])
            + last_result.get("expected_kind_errors", [])
            + last_result.get("schema_errors", [])
            + last_result.get("semantic_errors", [])
        )
        if all_errors:
            last_result["error_summary"] = summarise_errors(
                tool_name=tool_name,
                policy_id=policy_id,
                expected_kind=expected_kind or "",
                errors=all_errors,
            )

    return last_result


def _failure_detail(result: dict) -> str:
    """Build a short diagnostic suffix for failed runs."""
    parts: list[str] = []
    gvk = result.get("generated_kind") or result.get("generated_api_version")
    if gvk:
        api = result.get("generated_api_version", "")
        kind = result.get("generated_kind", "")
        parts.append(f"got {api}/{kind}" if api else f"got {kind}")
    stage = result.get("validation_stage")
    if stage and stage != "passed":
        parts.append(f"stage={stage}")
    errs = (
        result.get("expected_kind_errors")
        or result.get("schema_errors")
        or result.get("error")
    )
    if isinstance(errs, list) and errs:
        parts.append(errs[0][:80])
    elif isinstance(errs, str):
        parts.append(errs[:80])
    return f"  ({'; '.join(parts)})" if parts else ""


def _print_summary(results: list[dict]) -> None:
    """Print a rich summary table to stdout."""
    print()
    hdr = (
        f"  {'Tool':<10} {'Policy':<35} {'Type':<9} {'Kind':<20} "
        f"{'Schema+CEL':>11} {'Functional':>11} {'Time(s)':>8}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    schema_total = schema_pass_n = 0
    semantic_total = semantic_pass_n = 0

    for r in results:
        task_type = r.get("task_type", "convert")
        kind_short = (r.get("expected_output_kind") or "-")[:18]
        t = r.get("conversion_time_seconds")
        time_str = f"{t:.1f}" if t else "-"

        s_pass = r.get("schema_pass")
        sem = r.get("semantic_pass")
        sem_skip = r.get("semantic_skipped", True)

        s_str = "PASS" if s_pass else ("FAIL" if s_pass is not None else "-")
        if sem_skip:
            sem_str = "SKIP"
        elif sem is None:
            sem_str = "-"
        else:
            sem_str = "PASS" if sem else "FAIL"

        lint_warns = r.get("lint_warnings") or []
        lint_tag = f"  WARN: {lint_warns[0][:60]}" if lint_warns else ""

        print(
            f"  {r['tool']:<10} {r['policy_id']:<35} {task_type:<9} {kind_short:<20} "
            f"{s_str:>11} {sem_str:>11} {time_str:>8}{lint_tag}"
        )

        schema_total += 1
        if s_pass:
            schema_pass_n += 1
        if not sem_skip:
            semantic_total += 1
            if sem:
                semantic_pass_n += 1

    print()
    parts = [f"Schema+CEL: {schema_pass_n}/{schema_total}"]
    if semantic_total:
        parts.append(f"Functional: {semantic_pass_n}/{semantic_total}")
    times = [r.get("conversion_time_seconds") for r in results if r.get("conversion_time_seconds")]
    if times:
        parts.append(f"Avg: {sum(times)/len(times):.1f}s")
    print(f"  Summary: {' | '.join(parts)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Policy as Code Benchmark")
    parser.add_argument("--tool", nargs="*", help="Tools to run (default: all enabled)")
    parser.add_argument("--track", help="Filter by conversion track")
    parser.add_argument("--policy-id", nargs="+", help="Run one or more policies by ID")
    parser.add_argument("--difficulty", help="Filter by difficulty (easy, medium, hard, stress)")
    parser.add_argument("--task-type", choices=["convert", "generate", "generate_test"], help="Filter by task type")
    parser.add_argument("--output-kind", help="Filter by expected output kind (e.g. MutatingPolicy)")
    parser.add_argument("--max-attempts", type=int, default=1, help="Max attempts per policy (iterative improvement)")
    parser.add_argument("--workers", type=int, default=1, help="Parallel workers per tool (default: 1 = sequential)")
    parser.add_argument("--tool-script", help="Path to tool runner script (overrides auto-detection from --tool)")
    parser.add_argument("--skip-kyverno-test", action="store_true")
    parser.add_argument("--containerized", action="store_true", help="Run tools in isolated Docker containers (no config/memory/skills leak)")
    parser.add_argument("--persistent", action="store_true", help="Reuse one container per tool across tasks (implies --containerized). Lets the agent accumulate context between conversions.")
    parser.add_argument("--report", action="store_true", help="Generate report from existing results (no runs)")
    args = parser.parse_args()

    # --persistent implies --containerized
    if args.persistent:
        args.containerized = True

    config = _load_config()
    policies = _load_dataset()
    eval_config = config.get("evaluation", {})
    if args.skip_kyverno_test:
        eval_config["skip_kyverno_test"] = True

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
    output_base = REPO_ROOT / "output"

    # All result files are timestamped, so consecutive runs never collide.
    # No wipe needed — results accumulate safely.
    results_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[dict] = []

    num_workers = args.workers

    def _run_tool(tool_name: str) -> tuple[list[dict], list[str]]:
        """Run all jobs for a single tool. Returns (results, log_lines)."""
        tcfg = tool_configs.get(tool_name, {})
        workers_label = f", {num_workers} workers" if num_workers > 1 else ""
        mode_label = " (persistent)" if args.persistent else ""
        # Print the header live so it appears before the container_runner's
        # tee'd output, not buffered with the per-policy status lines.
        print(f"\n--- Running {tool_name}{workers_label}{mode_label} ---", flush=True)
        lines: list[str] = []

        # In persistent mode, create ONE container for the entire tool run.
        # The runner is shared across all tasks so the agent accumulates context.
        shared_runner: ToolRunner | None = None
        if args.persistent:
            from runners.container_runner import ContainerRunner
            shared_runner = ContainerRunner(tool_name, persistent=True)
            shared_runner.setup(tcfg)

        def _execute_job(policy: dict) -> dict:
            return _run_single(
                tool_name,
                tcfg,
                policy,
                max_attempts=args.max_attempts,
                eval_config=eval_config,
                tool_script=args.tool_script,
                containerized=args.containerized,
                persistent=args.persistent,
                runner=shared_runner,
            )

        tool_results: list[dict] = []

        if num_workers <= 1:
            for policy in policies:
                task_type = policy.get("task_type", "convert")
                label = f"{policy['id']} ({task_type}/{policy['track']})"

                result = _execute_job(policy)
                tool_results.append(result)

                status = "PASS" if result.get("success") else "FAIL"
                detail = ""
                if not result.get("success"):
                    detail = _failure_detail(result)
                lines.append(f"  [{tool_name}] {label} ... {status}{detail}")

                run_id = result.get("run_id", f"run_{tool_name}_{policy['id']}")
                out_json = results_dir / f"{run_id}.json"
                out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        else:
            completed = 0
            total = len(policies)
            with ThreadPoolExecutor(max_workers=num_workers) as pool:
                futures = {pool.submit(_execute_job, p): p for p in policies}
                for future in as_completed(futures):
                    policy = futures[future]
                    completed += 1
                    task_type = policy.get("task_type", "convert")
                    label = f"{policy['id']} ({task_type}/{policy['track']})"

                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "tool": tool_name,
                            "policy_id": policy["id"],
                            "track": policy.get("track", "unknown"),
                            "task_type": policy.get("task_type", "convert"),
                            "success": False,
                            "error": f"Unexpected worker error: {exc}",
                            "run_id": f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}_{tool_name}_{policy['id']}",
                        }
                    tool_results.append(result)

                    status = "PASS" if result.get("success") else "FAIL"
                    detail = ""
                    if not result.get("success"):
                        detail = _failure_detail(result)
                    lines.append(f"  [{completed}/{total}] [{tool_name}] {label} ... {status}{detail}")

                    run_id = result.get("run_id", f"run_{tool_name}_{policy['id']}")
                    out_json = results_dir / f"{run_id}.json"
                    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

        # Tear down persistent container after all tasks complete
        if shared_runner is not None:
            shared_runner.teardown()

        return tool_results, lines

    # Run all tools in parallel — each tool gets its own thread.
    # Output is buffered per tool and printed in order after all finish.
    if len(tools_to_run) > 1:
        with ThreadPoolExecutor(max_workers=len(tools_to_run)) as tool_pool:
            tool_futures = {t: tool_pool.submit(_run_tool, t) for t in tools_to_run}
        for tool_name in tools_to_run:
            results, lines = tool_futures[tool_name].result()
            print("\n".join(lines))
            all_results.extend(results)
    else:
        results, lines = _run_tool(tools_to_run[0])
        print("\n".join(lines))
        all_results.extend(results)

    _print_summary(all_results)

    # Write aggregated results
    agg_path = results_dir / f"benchmark_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    agg_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    print(f"  Aggregated results: {agg_path}")

    # Regenerate dashboard from all results
    try:
        from reports.generate import generate_all
        generate_all()
    except Exception as exc:
        print(f"  Warning: dashboard update failed: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
