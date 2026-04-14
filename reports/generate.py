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
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  Warning: skipping corrupt result file {f.name}: {exc}", file=sys.stderr)
            continue
        except OSError as exc:
            print(f"  Warning: could not read result file {f.name}: {exc}", file=sys.stderr)
            continue
    return results


def _deduplicate_runs(results: list[dict]) -> list[dict]:
    """If multiple runs exist for the same (tool, policy_id), keep the latest only.

    Uses the ``timestamp`` field (ISO-8601) for ordering so that results are
    correctly ranked regardless of the alphabetical sort order of result filenames
    (e.g. ``run_*`` files sort after ``benchmark_*`` files even if older).
    Falls back to load order when timestamp is absent.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in results:
        key = (r.get("tool", ""), r.get("policy_id", ""))
        groups[key].append(r)

    def _latest(runs: list[dict]) -> dict:
        timestamped = [(r.get("timestamp", ""), r) for r in runs]
        return max(timestamped, key=lambda t: t[0])[1]

    return [_latest(runs) for runs in groups.values()]


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
        # Per-policy "passed" stays binary (majority-vote / single-run success)
        # for the per-policy display tables. The headline pass_rate, however,
        # uses the per-policy `pass_rate` field when present (multi-run mean
        # methodology) so flaky policies don't get rounded up to 100% pass.
        # Falls back to binary success when records are single-run.
        passed = sum(1 for i in items if i.get("success"))
        if items and any("pass_rate" in i for i in items):
            pass_rate_sum = sum(
                i.get("pass_rate", 1.0 if i.get("success") else 0.0) for i in items
            )
            pass_rate = round(pass_rate_sum / total, 4)
            n_runs_aggregated = max(
                (i.get("n_runs_aggregated", 1) for i in items), default=1
            )
        else:
            pass_rate = round(passed / total, 4) if total else 0
            n_runs_aggregated = 1
        schema_pass = sum(1 for i in items if i.get("schema_pass"))
        # Count functional tests: if a kyverno_test_dir exists, the test is
        # applicable even if the tool produced no output (semantic_skipped due
        # to no output should count as a failure, not reduce the denominator).
        semantic_items = [i for i in items if not i.get("semantic_skipped", True)]
        # Policies skipped only because tool produced no output count against total
        no_output_skips = [
            i for i in items
            if i.get("semantic_skipped", True) and not i.get("success") and not i.get("schema_pass")
        ]
        semantic_pass = sum(1 for i in semantic_items if i.get("semantic_pass"))
        times = [i["conversion_time_seconds"] for i in items if i.get("conversion_time_seconds")]
        costs = [i["cost_usd"] for i in items if i.get("cost_usd") is not None]
        return {
            "total": total,
            "passed": passed,
            "pass_rate": pass_rate,
            "n_runs_aggregated": n_runs_aggregated,
            "schema_pass": schema_pass,
            "semantic_pass": semantic_pass,
            "semantic_total": len(semantic_items) + len(no_output_skips),
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
        except Exception as exc:
            print(f"  Warning: report.md.j2 render failed ({exc!r}); using fallback", file=sys.stderr)

    # Fallback: build markdown directly
    lines: list[str] = []
    lines.append("# Policy as Code Benchmark Report")
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

    def _rate_str(stats: dict) -> str:
        n_runs = stats.get("n_runs_aggregated", 1)
        if n_runs > 1:
            return f"{stats['pass_rate']:.1%} mean (N={n_runs})"
        return f"{stats['passed']}/{stats['total']} passed"

    lines.append("\n## Per-Track Breakdown\n")
    for track, stats in agg["track_stats"].items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{track}**: {_rate_str(stats)}, avg {t}")

    lines.append("\n## Per-Task-Type Breakdown\n")
    for tt, stats in agg.get("task_type_stats", {}).items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{tt}**: {_rate_str(stats)}, avg {t}")

    lines.append("\n## Per-Difficulty Breakdown\n")
    for diff, stats in agg.get("difficulty_stats", {}).items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{diff}**: {_rate_str(stats)}, avg {t}")

    lines.append("\n## Per-Output-Kind Breakdown\n")
    for kind, stats in agg.get("output_kind_stats", {}).items():
        t = f"{stats['avg_time']:.1f}s" if stats["avg_time"] else "-"
        lines.append(f"- **{kind}**: {_rate_str(stats)}, avg {t}")

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
                f"dashboard.html.j2 template rendering failed ({exc!r}); "
                "using minimal fallback.",
                file=sys.stderr,
            )

    # Fallback: minimal dashboard (Jinja2 missing or template error)
    return (
        f"<html><body style='font-family:sans-serif;padding:2rem;background:#0d1117;color:#c9d1d9'>"
        f"<h1>Policy as Code Benchmark</h1>"
        f"<p>Jinja2 template rendering failed or Jinja2 is not installed. Install or fix: <code>pip install jinja2</code></p>"
        f"<p>Generated: {agg['generated_at']}</p>"
        f"<p>{len(agg['results'])} results from {len(agg['tool_stats'])} tools</p>"
        f"</body></html>"
    )


def _build_report_data(
    config: dict | None = None, include_files: list[str] | None = None
) -> tuple[dict, list[dict], dict | None] | None:
    """Shared pipeline: load config, load results, deduplicate, aggregate, compute leaderboard.

    Returns ``(agg, leaderboard, config)`` or ``None`` when no results are found.
    """
    if config is None and yaml:
        cfg_path = REPO_ROOT / "config.yaml"
        if cfg_path.exists():
            config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))

    results = _load_results(include_files=include_files)
    if not results:
        print("No results found in results/. Run benchmark.py first.", file=sys.stderr)
        return None

    results = _deduplicate_runs(results)
    agg = _aggregate(results)
    leaderboard = _compute_leaderboard(agg["tool_stats"], config)
    return agg, leaderboard, config


def generate_all(config: dict | None = None, include_files: list[str] | None = None) -> None:
    """Load results, generate reports, write to reports/output/."""
    data = _build_report_data(config=config, include_files=include_files)
    if data is None:
        return
    agg, leaderboard, config = data

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

    data = _build_report_data(include_files=args.from_results)
    if data is None:
        return 1
    agg, leaderboard, config = data

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
