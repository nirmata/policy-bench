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
    """If multiple runs exist for the same (tool, policy_id, task_type), keep the best one.

    Prefers records that carry a ``runs`` array (multi-run aggregated data)
    over plain single-run records.  Among candidates of the same kind, the
    latest by ``timestamp`` wins.
    """
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in results:
        key = (r.get("tool", ""), r.get("policy_id", ""), r.get("task_type", "convert"))
        groups[key].append(r)

    def _best(runs: list[dict]) -> dict:
        # Prefer records with 'runs' array (multi-run aggregated)
        with_runs = [r for r in runs if r.get("runs")]
        pool = with_runs if with_runs else runs
        timestamped = [(r.get("timestamp", ""), r) for r in pool]
        return max(timestamped, key=lambda t: t[0])[1]

    return [_best(runs) for runs in groups.values()]


def _backfill_output_yaml(results: list[dict]) -> None:
    """Populate ``output_yaml`` from ``raw_output_path`` when missing.

    Older benchmark runs only stored a 25-line ``yaml_preview``.  This reads
    the full generated file (up to 10 000 chars) so the dashboard modal can
    show the complete policy.
    """
    for r in results:
        if r.get("output_yaml"):
            continue
        raw_path = r.get("raw_output_path")
        if not raw_path:
            continue
        p = Path(raw_path)
        if not p.is_file():
            # Try resolving relative to repo root (path may have been
            # recorded on a different machine with a different prefix).
            rel = None
            for part_idx, part in enumerate(p.parts):
                if part == "output":
                    rel = Path(*p.parts[part_idx:])
                    break
            if rel:
                p = REPO_ROOT / rel
            if not p.is_file():
                continue
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
            r["output_yaml"] = raw[:10_000]
        except OSError:
            pass


def _aggregate(results: list[dict]) -> dict:
    """Compute per-tool, per-track, per-difficulty, per-output-kind, and per-task-type aggregations."""
    by_tool: dict[str, list[dict]] = defaultdict(list)
    by_track: dict[str, list[dict]] = defaultdict(list)
    by_difficulty: dict[str, list[dict]] = defaultdict(list)
    by_output_kind: dict[str, list[dict]] = defaultdict(list)
    by_task_type: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_tool[r.get("tool") or "unknown"].append(r)
        by_track[r.get("track") or "unknown"].append(r)
        by_difficulty[r.get("difficulty") or "unknown"].append(r)
        by_output_kind[r.get("expected_output_kind") or "unknown"].append(r)
        by_task_type[r.get("task_type") or "convert"].append(r)

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

        # Schema+CEL and Functional counts (task-level).
        # When per-run arrays exist (schema_pass_per_run, semantic_pass_per_run),
        # use mean-weighted counts consistent with the headline pass_rate.
        # Each task contributes its per-run pass fraction (e.g. 2/3 = 0.667)
        # instead of a binary 0 or 1, then the sum is rounded to an integer.
        has_per_run_stages = any("schema_pass_per_run" in i for i in items)
        if has_per_run_stages:
            schema_pass_sum = 0.0
            for i in items:
                sppr = i.get("schema_pass_per_run")
                if sppr:
                    schema_pass_sum += sum(1 for x in sppr if x) / len(sppr)
                elif i.get("schema_pass"):
                    schema_pass_sum += 1.0
            schema_pass = round(schema_pass_sum)

            semantic_pass_sum = 0.0
            semantic_total = 0
            for i in items:
                seppr = i.get("semantic_pass_per_run")
                if seppr:
                    semantic_total += 1
                    semantic_pass_sum += sum(1 for x in seppr if x) / len(seppr)
                elif not i.get("semantic_skipped", True):
                    semantic_total += 1
                    if i.get("semantic_pass"):
                        semantic_pass_sum += 1.0
                elif i.get("semantic_skipped", True) and not i.get("success") and not i.get("schema_pass"):
                    semantic_total += 1  # no-output skip counts against total
            semantic_pass = round(semantic_pass_sum)
        else:
            # Fallback: single-run representative counts
            schema_pass = sum(1 for i in items if i.get("schema_pass"))
            semantic_items = [i for i in items if not i.get("semantic_skipped", True)]
            no_output_skips = [
                i for i in items
                if i.get("semantic_skipped", True) and not i.get("success") and not i.get("schema_pass")
            ]
            semantic_pass = sum(1 for i in semantic_items if i.get("semantic_pass"))
            semantic_total = len(semantic_items) + len(no_output_skips)

        # Overall multi-run totals and robust/flaky/always-fail classification
        total_runs = 0
        passed_runs = 0
        robust = 0
        flaky = 0
        always_fail = 0
        for i in items:
            ppr = i.get("pass_per_run")
            if ppr:
                total_runs += len(ppr)
                p = sum(1 for x in ppr if x)
                passed_runs += p
                if p == len(ppr):
                    robust += 1
                elif p == 0:
                    always_fail += 1
                else:
                    flaky += 1
            else:
                total_runs += 1
                if i.get("success"):
                    passed_runs += 1
                    robust += 1
                else:
                    always_fail += 1

        times = [i["conversion_time_seconds"] for i in items if i.get("conversion_time_seconds")]
        costs = [i["cost_usd"] for i in items if i.get("cost_usd") is not None]
        return {
            "total": total,
            "passed": passed,
            "pass_rate": pass_rate,
            "n_runs_aggregated": n_runs_aggregated,
            "schema_pass": schema_pass,
            "semantic_pass": semantic_pass,
            "semantic_total": semantic_total,
            "total_runs": total_runs,
            "passed_runs": passed_runs,
            "robust": robust,
            "flaky": flaky,
            "always_fail": always_fail,
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


def _generate_tool_summaries(results: list[dict], tool_stats: dict) -> dict[str, str]:
    """Generate an AI summary per tool using Claude Haiku.

    Uses the same ``ANTHROPIC_API_KEY`` that the Claude runner uses for
    benchmarks — no additional configuration needed.

    Returns a mapping of ``{tool_name: summary_text}``.  Silently returns an
    empty dict when the Anthropic SDK is unavailable or the API call fails.
    """
    try:
        import anthropic
    except ImportError:
        return {}

    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_tool[r.get("tool", "unknown")].append(r)

    summaries: dict[str, str] = {}
    client = anthropic.Anthropic()

    for tool, items in by_tool.items():
        stats = tool_stats.get(tool, {})
        passed = [r for r in items if r.get("success")]
        failed = [r for r in items if not r.get("success")]

        # Collect unique error snippets (truncated) for failed tasks
        error_samples: list[str] = []
        for r in failed[:15]:
            errs = (
                ([r["error"]] if r.get("error") else [])
                + r.get("schema_errors", [])
                + r.get("semantic_errors", [])
            )
            if errs:
                error_samples.append(
                    f"  - {r.get('policy_id', '?')}: {'; '.join(e[:120] for e in errs[:2])}"
                )

        passed_ids = ", ".join(r.get("policy_id", "?") for r in passed)
        failed_ids = ", ".join(r.get("policy_id", "?") for r in failed)

        prompt = (
            f"You are a Kubernetes / Kyverno policy expert analysing benchmark results.\n\n"
            f"Tool: {tool}\n"
            f"Total tasks: {stats.get('total', len(items))}\n"
            f"Pass rate: {stats.get('pass_rate', 0):.0%}\n"
            f"Schema+CEL passed: {stats.get('schema_pass', 0)}/{stats.get('total', 0)}\n"
            f"Functional passed: {stats.get('semantic_pass', 0)}/{stats.get('semantic_total', 0)}\n"
            f"Avg time: {stats.get('avg_time') or 'N/A'}s\n"
            f"Avg cost: ${stats.get('avg_cost') or 0:.4f}\n\n"
            f"Passed policies: {passed_ids or 'none'}\n"
            f"Failed policies: {failed_ids or 'none'}\n\n"
            f"Sample errors from failed tasks:\n"
            + ("\n".join(error_samples) if error_samples else "  (no errors captured)")
            + "\n\n"
            f"Write a concise summary (4-6 sentences) of how this tool performed overall. "
            f"Cover: what it did well, its main failure patterns, and how it compares in "
            f"terms of accuracy. Write in plain English for someone who may not be a "
            f"Kyverno expert. Do NOT suggest fixes or next steps."
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
                timeout=20,
            )
            text = (response.content[0].text or "").strip() if response.content else ""
            if text:
                summaries[tool] = text
        except Exception:
            continue

    return summaries


def _compute_testgen_leaderboard(testgen_results: list[dict]) -> list[dict]:
    """Per-tool stats for generate_test runs: composite pass, coverage, has_pass_and_fail."""
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for r in testgen_results:
        by_tool[r.get("tool", "unknown")].append(r)

    board = []
    for tool, items in sorted(by_tool.items()):
        total = len(items)
        composite_pass = sum(1 for i in items if i.get("testgen_composite_pass"))
        coverage_scores = [i["testgen_coverage_score"] for i in items if i.get("testgen_oracle_tuples", 0) > 0]
        has_pf = sum(1 for i in items if i.get("testgen_has_pass_and_fail"))
        times = [i["conversion_time_seconds"] for i in items if i.get("conversion_time_seconds")]
        costs = [i["cost_usd"] for i in items if i.get("cost_usd") is not None]
        board.append({
            "tool": tool,
            "total": total,
            "composite_pass": composite_pass,
            "composite_pass_rate": round(composite_pass / total, 4) if total else 0,
            "avg_coverage": round(sum(coverage_scores) / len(coverage_scores), 4) if coverage_scores else 0,
            "has_pass_and_fail": has_pf,
            "avg_time": round(sum(times) / len(times), 2) if times else None,
            "avg_cost": round(sum(costs) / len(costs), 6) if costs else None,
        })
    board.sort(key=lambda x: (-x["composite_pass_rate"], x["avg_time"] or float("inf")))
    return board


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

    testgen = [r for r in agg["results"] if r.get("task_type") == "generate_test"]
    if testgen:
        tg_board = _compute_testgen_leaderboard(testgen)
        lines.append("\n## Kyverno CLI Test Generation\n")
        lines.append(f"| {'Tool':<10} | {'Composite Pass':>14} | {'Avg Coverage':>12} | {'Has Pass+Fail':>13} | {'Avg Time':>8} |")
        lines.append(f"|{'-'*12}|{'-'*16}|{'-'*14}|{'-'*15}|{'-'*10}|")
        for e in tg_board:
            t = f"{e['avg_time']:.1f}s" if e["avg_time"] else "-"
            lines.append(
                f"| {e['tool']:<10} | {e['composite_pass']:>5}/{e['total']:<8} | "
                f"{e['avg_coverage']:>11.0%} | {e['has_pass_and_fail']:>5}/{e['total']:<7} | {t:>8} |"
            )

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
    """Generate a self-contained HTML dashboard (combined + conversion + generation + test-gen)."""
    results_all = agg["results"]
    convert_results = [
        r for r in results_all if r.get("task_type", "convert") == "convert"
    ]
    generate_results = [r for r in results_all if r.get("task_type") == "generate"]
    testgen_results = [r for r in results_all if r.get("task_type") == "generate_test"]
    convert_agg = _aggregate(convert_results)
    generate_agg = _aggregate(generate_results)
    leaderboard_convert = _compute_leaderboard(convert_agg["tool_stats"], config)
    leaderboard_generate = _compute_leaderboard(generate_agg["tool_stats"], config)
    leaderboard_testgen = _compute_testgen_leaderboard(testgen_results)
    has_convert = bool(convert_results)
    has_generate = bool(generate_results)
    has_testgen = bool(testgen_results)

    if Environment and TEMPLATES_DIR.exists():
        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        try:
            tpl = env.get_template("dashboard.html.j2")
            return tpl.render(
                agg=agg,
                leaderboard=leaderboard,
                leaderboard_convert=leaderboard_convert,
                leaderboard_generate=leaderboard_generate,
                leaderboard_testgen=leaderboard_testgen,
                convert_results=convert_results,
                generate_results=generate_results,
                testgen_results=testgen_results,
                has_convert=has_convert,
                has_generate=has_generate,
                has_testgen=has_testgen,
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
    _backfill_output_yaml(results)
    agg = _aggregate(results)
    agg["tool_summaries"] = _generate_tool_summaries(results, agg["tool_stats"])
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
