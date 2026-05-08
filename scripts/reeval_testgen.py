#!/usr/bin/env python3
"""Re-evaluate existing tg_ output directories with the current evaluator code
and append results into benchmark_latest.json.

Does NOT re-run any AI tools — only re-runs the evaluator on output files
already on disk (output/{tool}/tg_*/).

Usage:
  python3 scripts/reeval_testgen.py          # dry-run: show what would change
  python3 scripts/reeval_testgen.py --write   # actually update benchmark_latest.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evaluators.testgen_validator import evaluate_testgen

TOOLS = ["nctl", "claude", "cursor"]
DATASET_INDEX = REPO_ROOT / "dataset" / "index.yaml"
OUTPUT_DIR = REPO_ROOT / "output"
LATEST_PATH = REPO_ROOT / "results" / "benchmark_latest.json"


def load_tg_policies() -> list[dict]:
    with open(DATASET_INDEX) as f:
        data = yaml.safe_load(f)
    return [p for p in data["policies"] if p["id"].startswith("tg_")]


def main() -> int:
    write_mode = "--write" in sys.argv

    policies = load_tg_policies()
    print(f"Found {len(policies)} tg_ policies in dataset/index.yaml", file=sys.stderr)

    results: list[dict] = []

    for policy in policies:
        pid = policy["id"]
        source_policy = REPO_ROOT / "dataset" / policy["path"]
        oracle_dir = (
            REPO_ROOT / "dataset" / policy["kyverno_test_dir"]
            if policy.get("kyverno_test_dir")
            else None
        )

        for tool in TOOLS:
            generated_dir = OUTPUT_DIR / tool / pid
            if not generated_dir.exists():
                print(f"  SKIP {tool}/{pid} — output dir missing", file=sys.stderr)
                continue

            test_file = generated_dir / "kyverno-test.yaml"
            resources_file = generated_dir / "resources.yaml"
            if not test_file.exists() or not resources_file.exists():
                print(
                    f"  SKIP {tool}/{pid} — missing "
                    f"{'kyverno-test.yaml' if not test_file.exists() else 'resources.yaml'}",
                    file=sys.stderr,
                )
                continue

            eval_result = evaluate_testgen(
                generated_dir=generated_dir,
                source_policy=source_policy,
                oracle_dir=oracle_dir,
                timeout_sec=60,
            )

            composite = eval_result.get("testgen_composite_pass", False)
            status = "PASS" if composite else "FAIL"
            print(f"  {status} {tool}/{pid}", file=sys.stderr)

            now = datetime.now(timezone.utc).isoformat()
            entry = {
                "run_id": f"reeval_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{tool}_{pid}",
                "tool": tool,
                "policy_id": pid,
                "track": policy.get("track", "kyverno-test-gen"),
                "task_type": policy.get("task_type", "generate_test"),
                "difficulty": policy.get("difficulty", ""),
                "expected_output_kind": policy.get("expected_output_kind"),
                "description": policy.get("description", ""),
                "prompt": "",
                "timestamp": now,
                "success": composite,
                "conversion_time_seconds": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0,
                "tokens_estimated": False,
                "model": f"{tool}-reeval",
                "tool_version": None,
                "raw_output_path": str(generated_dir),
                "attempt": 1,
                "max_attempts": 1,
                **eval_result,
                "error": None,
                "yaml_preview": "",
                "input_snippet": "",
                "output_yaml": "",
                "raw_log": "",
                "n_runs_aggregated": 1,
                "pass_rate": 1.0 if composite else 0.0,
                "pass_per_run": [composite],
                "schema_pass_per_run": [eval_result.get("testgen_schema_pass", False)],
                "semantic_pass_per_run": [eval_result.get("testgen_kyverno_test_pass", False)],
                "aggregation_method": "single-reeval",
            }
            results.append(entry)

    print(f"\nRe-evaluated {len(results)} entries", file=sys.stderr)
    pass_count = sum(1 for r in results if r["success"])
    print(f"  {pass_count}/{len(results)} composite pass", file=sys.stderr)

    for tool in TOOLS:
        tool_results = [r for r in results if r["tool"] == tool]
        tool_pass = sum(1 for r in tool_results if r["success"])
        print(f"  {tool}: {tool_pass}/{len(tool_results)}", file=sys.stderr)

    if not write_mode:
        print("\nDry run — pass --write to update benchmark_latest.json", file=sys.stderr)
        return 0

    existing = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    existing_tg_ids = {
        (e["tool"], e["policy_id"])
        for e in existing
        if e.get("policy_id", "").startswith("tg_")
    }
    if existing_tg_ids:
        print(
            f"\nReplacing {len(existing_tg_ids)} existing tg_ entries in benchmark_latest.json",
            file=sys.stderr,
        )
        existing = [
            e for e in existing if not e.get("policy_id", "").startswith("tg_")
        ]

    existing.extend(results)
    LATEST_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nWritten {len(existing)} total entries to {LATEST_PATH}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
