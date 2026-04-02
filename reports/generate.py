#!/usr/bin/env python3
"""
Report generator — reads results/*.json and produces Markdown + HTML reports.

Usage:
  python3 reports/generate.py                          # both formats
  python3 reports/generate.py --format markdown
  python3 reports/generate.py --format html
  python3 reports/generate.py --format all
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    Environment = None  # type: ignore[assignment,misc]
    FileSystemLoader = None  # type: ignore[assignment,misc]

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "results"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _load_results(include_files: list[str] | None = None) -> list[dict]:
    """Load result JSON files from ``results/``.

    Each file may be a JSON array of runs or a single run dict (must include
    ``policy_id`` for dict rows). If *include_files* is set, only those
    basenames under ``results/`` are loaded (useful for a clean demo report).
    """
    results: list[dict] = []
    if include_files:
        candidates = [RESULTS_DIR / name for name in include_files]
    else:
        candidates = sorted(RESULTS_DIR.glob("*.json"))
    for f in candidates:
        if not f.is_file() or f.parent.name == "examples":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                results.extend(data)
            elif isinstance(data, dict) and "policy_id" in data:
                results.append(data)
        except Exception:
            continue
    return results


def _average_runs(results: list[dict]) -> list[dict]:
    """Collapse multiple runs of the same (tool, policy_id) into averaged entries.

    Boolean fields (schema_pass, intent_pass, etc.) use majority vote.
    Numeric fields (time, cost, diff_score, tokens) are averaged.
    If there's only one run per key, the result passes through unchanged.
    """
    from collections import defaultdict

    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        key = (r.get("tool", ""), r.get("policy_id", ""))
        groups[key].append(r)

    averaged: list[dict] = []
    for (_tool, _pid), runs in groups.items():
        if len(runs) == 1:
            averaged.append(runs[0])
            continue

        # Start from the latest run as the base
        base = dict(runs[-1])
        n = len(runs)
        base["runs_averaged"] = n

        # Majority vote for booleans
        for field in ("success", "schema_pass", "intent_pass", "semantic_pass"):
            vals = [r.get(field) for r in runs if r.get(field) is not None]
            if vals:
                base[field] = sum(1 for v in vals if v) > len(vals) / 2
            else:
                base[field] = None

        # Average numerics
        for field in ("conversion_time_seconds", "cost_usd", "diff_score",
                       "input_tokens", "output_tokens", "total_tokens"):
            vals = [r[field] for r in runs if r.get(field) is not None]
            if vals:
                base[field] = round(sum(vals) / len(vals), 4)
            else:
                base[field] = None

        # Pass rate across runs (extra field for the dashboard)
        base["pass_rate_across_runs"] = round(
            sum(1 for r in runs if r.get("success")) / n, 4
        )

        averaged.append(base)

    return averaged


def _aggregate(results: list[dict]) -> dict:
    """Compute per-tool, per-track, per-difficulty, per-output-kind, and per-task-type aggregations."""
    by_tool: dict[str, list[dict]] = defaultdict(list)
    by_track: dict[str, list[dict]] = defaultdict(list)
    by_difficulty: dict[str, list[dict]] = defaultdict(list)
    by_output_kind: dict[str, list[dict]] = defaultdict(list)
    by_task_type: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_tool[r.get("tool", "unknown")].append(r)
        by_track[r.get("track", "unknown")].append(r)
        by_difficulty[r.get("difficulty", "unknown")].append(r)
        by_output_kind[r.get("expected_output_kind", "unknown")].append(r)
        by_task_type[r.get("task_type", "convert")].append(r)

    def _stats(items: list[dict]) -> dict:
        total = len(items)
        passed = sum(1 for i in items if i.get("success"))
        schema_pass = sum(1 for i in items if i.get("schema_pass"))
        semantic_items = [i for i in items if not i.get("semantic_skipped", True)]
        semantic_pass = sum(1 for i in semantic_items if i.get("semantic_pass"))
        times = [i["conversion_time_seconds"] for i in items if i.get("conversion_time_seconds")]
        costs = [i["cost_usd"] for i in items if i.get("cost_usd") is not None]
        return {
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0,
            "schema_pass": schema_pass,
            "semantic_pass": semantic_pass,
            "semantic_total": len(semantic_items),
            "avg_time": round(sum(times) / len(times), 2) if times else None,
            "avg_cost": round(sum(costs) / len(costs), 6) if costs else None,
        }

    tool_stats = {tool: _stats(items) for tool, items in sorted(by_tool.items())}
    track_stats = {track: _stats(items) for track, items in sorted(by_track.items())}
    difficulty_stats = {d: _stats(items) for d, items in sorted(by_difficulty.items())}
    output_kind_stats = {k: _stats(items) for k, items in sorted(by_output_kind.items())}
    task_type_stats = {t: _stats(items) for t, items in sorted(by_task_type.items())}

    return {
        "tool_stats": tool_stats,
        "track_stats": track_stats,
        "difficulty_stats": difficulty_stats,
        "output_kind_stats": output_kind_stats,
        "task_type_stats": task_type_stats,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _compute_leaderboard(tool_stats: dict, config: dict | None = None) -> list[dict]:
    """Rank tools by pass_rate. Speed, cost, diff reported as supplementary metrics."""
    board: list[dict] = []
    for tool, stats in tool_stats.items():
        board.append({
            "tool": tool,
            "pass_rate": stats["pass_rate"],
            "avg_time": stats["avg_time"],
            "avg_cost": stats["avg_cost"],
            **stats,
        })
    board.sort(key=lambda x: (-x["pass_rate"], x["avg_time"] or float("inf")))
    for i, entry in enumerate(board, 1):
        entry["rank"] = i
    return board


def generate_markdown(agg: dict, leaderboard: list[dict]) -> str:
    """Generate a Markdown report (with or without Jinja2)."""
    if Environment and TEMPLATES_DIR.exists():
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        try:
            tpl = env.get_template("report.md.j2")
            return tpl.render(agg=agg, leaderboard=leaderboard)
        except Exception:
            pass

    # Fallback: build markdown directly
    lines: list[str] = []
    lines.append("# Policy Conversion Benchmark Report")
    lines.append(f"\nGenerated: {agg['generated_at']}\n")

    lines.append("## Leaderboard\n")
    lines.append(f"| {'Rank':>4} | {'Tool':<10} | {'Pass Rate':>9} | {'Schema+CEL':>11} | {'Functional':>11} | {'Avg Time':>8} | {'Avg Cost':>8} |")
    lines.append(f"|{'-'*6}|{'-'*12}|{'-'*11}|{'-'*13}|{'-'*13}|{'-'*10}|{'-'*10}|")
    for e in leaderboard:
        t = f"{e['avg_time']:.1f}s" if e["avg_time"] else "-"
        c = f"${e['avg_cost']:.4f}" if e["avg_cost"] else "-"
        lines.append(
            f"| {e['rank']:>4} | {e['tool']:<10} | {e['pass_rate']:>8.0%} | {e['schema_pass']:>5}/{e['total']:<5} | {e['semantic_pass']:>5}/{e['semantic_total']:<5} | {t:>8} | {c:>8} |"
        )

    lines.append("\n## Per-Track Breakdown\n")
    for track, stats in agg["track_stats"].items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{track}**: {stats['passed']}/{stats['total']} passed, avg {t}")

    lines.append("\n## Per-Task-Type Breakdown\n")
    for tt, stats in agg.get("task_type_stats", {}).items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{tt}**: {stats['passed']}/{stats['total']} passed, avg {t}")

    lines.append("\n## Per-Difficulty Breakdown\n")
    for diff, stats in agg.get("difficulty_stats", {}).items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{diff}**: {stats['passed']}/{stats['total']} passed, avg {t}")

    lines.append("\n## Per-Output-Kind Breakdown\n")
    for kind, stats in agg.get("output_kind_stats", {}).items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{kind}**: {stats['passed']}/{stats['total']} passed, avg {t}")

    lines.append("\n## Failures\n")
    failures = [r for r in agg["results"] if not r.get("success")]
    if failures:
        for f in failures:
            err = f.get("error") or "; ".join(f.get("schema_errors", []))
            lines.append(f"- `{f['tool']}` / `{f['policy_id']}`: {err}")
    else:
        lines.append("No failures.")

    lines.append("\n## Raw Data\n")
    lines.append("See `results/` directory for full JSON results.")
    lines.append("")
    return "\n".join(lines)


def generate_html(
    agg: dict, leaderboard: list[dict], config: dict | None = None
) -> str:
    """Generate a self-contained HTML dashboard (combined + conversion + generation)."""
    results_all = agg["results"]
    convert_results = [
        r for r in results_all if r.get("task_type", "convert") == "convert"
    ]
    generate_results = [r for r in results_all if r.get("task_type") == "generate"]
    convert_agg = _aggregate(convert_results)
    generate_agg = _aggregate(generate_results)
    leaderboard_convert = _compute_leaderboard(convert_agg["tool_stats"], config)
    leaderboard_generate = _compute_leaderboard(generate_agg["tool_stats"], config)
    has_convert = bool(convert_results)
    has_generate = bool(generate_results)

    if Environment and TEMPLATES_DIR.exists():
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        try:
            tpl = env.get_template("dashboard.html.j2")
            return tpl.render(
                agg=agg,
                leaderboard=leaderboard,
                leaderboard_convert=leaderboard_convert,
                leaderboard_generate=leaderboard_generate,
                convert_results=convert_results,
                generate_results=generate_results,
                has_convert=has_convert,
                has_generate=has_generate,
            )
        except Exception as exc:
            print(
                f"dashboard.html.j2 render failed ({exc!r}); "
                "using minimal fallback. Install jinja2 (e.g. pip install -r requirements.txt).",
                file=sys.stderr,
            )

    # Fallback: install jinja2 for the full dashboard
    return (
        f"<html><body style='font-family:sans-serif;padding:2rem;background:#0d1117;color:#c9d1d9'>"
        f"<h1>Policy Conversion Benchmark</h1>"
        f"<p>Install Jinja2 for the full dashboard: <code>pip install jinja2</code></p>"
        f"<p>Generated: {agg['generated_at']}</p>"
        f"<p>{len(agg['results'])} results from {len(agg['tool_stats'])} tools</p>"
        f"</body></html>"
    )


def generate_all(config: dict | None = None, include_files: list[str] | None = None) -> None:
    """Load results, generate reports, write to reports/output/."""
    if config is None and yaml:
        cfg_path = REPO_ROOT / "config.yaml"
        if cfg_path.exists():
            config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    results = _load_results(include_files=include_files)
    if not results:
        print("No results found in results/. Run benchmark.py first.", file=sys.stderr)
        return

    # Average multiple runs of the same (tool, policy) before aggregating
    results = _average_runs(results)
    agg = _aggregate(results)
    leaderboard = _compute_leaderboard(agg["tool_stats"], config)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    md = generate_markdown(agg, leaderboard)
    md_path = OUTPUT_DIR / "report.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"  Markdown report: {md_path}")

    html = generate_html(agg, leaderboard, config)
    html_path = OUTPUT_DIR / "dashboard.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"  HTML dashboard:  {html_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate benchmark reports")
    parser.add_argument(
        "--format",
        choices=["markdown", "html", "all"],
        default="all",
        help="Output format",
    )
    parser.add_argument("--output", help="Override output path (single format only)")
    parser.add_argument(
        "--from-results",
        nargs="*",
        metavar="FILE",
        help="Only load these JSON files from results/ (basenames). "
        "Example: --from-results benchmark_demo_conversion_generation.json",
    )
    args = parser.parse_args()

    config = None
    if yaml:
        cfg_path = REPO_ROOT / "config.yaml"
        if cfg_path.exists():
            config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    results = _load_results(include_files=args.from_results)
    if not results:
        print("No results found in results/. Run benchmark.py first.", file=sys.stderr)
        return 1

    # Average multiple runs of the same (tool, policy) before aggregating
    results = _average_runs(results)
    agg = _aggregate(results)
    leaderboard = _compute_leaderboard(agg["tool_stats"], config)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.format in ("markdown", "all"):
        md = generate_markdown(agg, leaderboard)
        p = Path(args.output) if args.output and args.format == "markdown" else OUTPUT_DIR / "report.md"
        p.write_text(md, encoding="utf-8")
        print(f"  Markdown: {p}")

    if args.format in ("html", "all"):
        html = generate_html(agg, leaderboard, config)
        p = Path(args.output) if args.output and args.format == "html" else OUTPUT_DIR / "dashboard.html"
        p.write_text(html, encoding="utf-8")
        print(f"  HTML: {p}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
