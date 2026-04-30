#!/usr/bin/env python3
"""Merge multiple benchmark runs into a single results file with mean pass rates.

Usage:
  python3 scripts/merge_runs.py results/run1/benchmark_*.json results/run2/benchmark_*.json ...
  python3 scripts/merge_runs.py results/run*/benchmark_*.json

Reads the aggregated benchmark JSON from each run, groups by (tool, policy_id, task_type),
and produces a merged JSON where each entry has:
  - pass_rate: mean of successes across runs (0.0, 0.333, 0.667, 1.0 for 3 runs)
  - n_runs_aggregated: number of runs merged
  - pass_per_run: [bool, ...] per-run success
  - aggregation_method: "mean"
  - runs: list of per-run records (shared/identical fields stripped to save space)

The representative entry (validation details, timing, etc.) is taken from the
FIRST successful run, or the last run if all failed.

Output: results/benchmark_latest.json (overwritten)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Fields that are identical across runs of the same (tool, policy) —
# stripped from sub-run records to avoid bloating the JSON.
SHARED_FIELDS = {
    "prompt", "input_snippet", "description", "difficulty",
    "track", "task_type", "expected_output_kind",
}


def merge(run_files: list[Path]) -> list[dict]:
    """Merge multiple run files into a single results list."""
    # Group all entries by (tool, policy_id, task_type)
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)

    for run_file in run_files:
        data = json.loads(run_file.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print(f"  Warning: {run_file} is not a list, skipping", file=sys.stderr)
            continue
        for entry in data:
            key = (entry["tool"], entry["policy_id"], entry.get("task_type", "convert"))
            by_key[key].append(entry)

    merged: list[dict] = []
    for (tool, policy_id, _task_type), entries in sorted(by_key.items()):
        n_runs = len(entries)
        pass_per_run = [e.get("success", False) for e in entries]
        pass_rate = round(sum(1 for p in pass_per_run if p) / n_runs, 4)

        # Pick the representative entry: first success, or last failure
        representative = entries[-1]
        for e in entries:
            if e.get("success"):
                representative = e
                break

        # Merge timing and cost as averages
        times = [e["conversion_time_seconds"] for e in entries if e.get("conversion_time_seconds")]
        costs = [e["cost_usd"] for e in entries if e.get("cost_usd") is not None]

        # Build slim per-run records (strip shared fields to save space)
        slim_runs = []
        for e in entries:
            slim = {k: v for k, v in e.items() if k not in SHARED_FIELDS}
            slim_runs.append(slim)

        # Per-stage-per-run arrays so the dashboard can show accurate
        # Schema+CEL and Functional totals across all N runs.
        schema_pass_per_run = [bool(e.get("schema_pass")) for e in entries]
        semantic_pass_per_run = [
            bool(e.get("semantic_pass")) for e in entries
            if not e.get("semantic_skipped", True)
        ]

        result = dict(representative)
        result["success"] = pass_rate >= 0.5  # majority for binary compat
        result["n_runs_aggregated"] = n_runs
        result["pass_rate"] = pass_rate
        result["pass_per_run"] = pass_per_run
        result["schema_pass_per_run"] = schema_pass_per_run
        result["semantic_pass_per_run"] = semantic_pass_per_run
        result["aggregation_method"] = "mean"
        result["runs"] = slim_runs

        if times:
            result["conversion_time_seconds"] = round(sum(times) / len(times), 3)
        if costs:
            result["cost_usd"] = round(sum(costs) / len(costs), 6)

        merged.append(result)

    return merged


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/merge_runs.py <run1.json> <run2.json> ...", file=sys.stderr)
        return 1

    run_files = [Path(f) for f in sys.argv[1:]]
    for f in run_files:
        if not f.is_file():
            print(f"Error: {f} not found", file=sys.stderr)
            return 1

    print(f"Merging {len(run_files)} run files...", file=sys.stderr)
    merged = merge(run_files)

    # Summary
    tools = set(e["tool"] for e in merged)
    for tool in sorted(tools):
        entries = [e for e in merged if e["tool"] == tool]
        pass_rates = [e["pass_rate"] for e in entries]
        mean_rate = sum(pass_rates) / len(pass_rates)
        n_runs = entries[0].get("n_runs_aggregated", 1)
        print(f"  {tool}: {mean_rate:.1%} mean (N={n_runs}, {len(entries)} policies)", file=sys.stderr)

    out_path = REPO_ROOT / "results" / "benchmark_latest.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nWritten to {out_path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
