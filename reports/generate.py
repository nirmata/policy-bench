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
        intent_items = [i for i in items if i.get("intent_pass") is not None]
        intent_pass = sum(1 for i in intent_items if i.get("intent_pass"))
        semantic_items = [i for i in items if not i.get("semantic_skipped", True)]
        semantic_pass = sum(1 for i in semantic_items if i.get("semantic_pass"))
        times = [i["conversion_time_seconds"] for i in items if i.get("conversion_time_seconds")]
        costs = [i["cost_usd"] for i in items if i.get("cost_usd") is not None]
        diffs = [i["diff_score"] for i in items if i.get("diff_score") is not None]
        return {
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0,
            "schema_pass": schema_pass,
            "intent_pass": intent_pass,
            "intent_total": len(intent_items),
            "semantic_pass": semantic_pass,
            "semantic_total": len(semantic_items),
            "avg_time": round(sum(times) / len(times), 2) if times else None,
            "avg_cost": round(sum(costs) / len(costs), 6) if costs else None,
            "avg_diff_score": round(sum(diffs) / len(diffs), 4) if diffs else None,
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
    """Rank tools by composite score."""
    weights = (config or {}).get("leaderboard", {}).get("weights", {})
    w_pass = weights.get("pass_rate", 0.5)
    w_speed = weights.get("speed", 0.2)
    w_diff = weights.get("diff_score", 0.2)
    w_cost = weights.get("cost", 0.1)

    # Normalize time and cost (lower is better)
    max_time = max(
        (s["avg_time"] for s in tool_stats.values() if s["avg_time"]), default=1
    ) or 1
    max_cost = max(
        (s["avg_cost"] for s in tool_stats.values() if s["avg_cost"]), default=1
    ) or 1

    board: list[dict] = []
    for tool, stats in tool_stats.items():
        norm_time = 1 - ((stats["avg_time"] or 0) / max_time)
        norm_cost = 1 - ((stats["avg_cost"] or 0) / max_cost)
        composite = (
            w_pass * stats["pass_rate"]
            + w_speed * norm_time
            + w_diff * (stats["avg_diff_score"] or 0)
            + w_cost * norm_cost
        )
        board.append({
            "tool": tool,
            "pass_rate": stats["pass_rate"],
            "avg_time": stats["avg_time"],
            "avg_cost": stats["avg_cost"],
            "avg_diff_score": stats["avg_diff_score"],
            "composite_score": round(composite, 4),
            **stats,
        })
    board.sort(key=lambda x: x["composite_score"], reverse=True)
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
    lines.append(f"| {'Rank':>4} | {'Tool':<10} | {'Pass Rate':>9} | {'Avg Time':>8} | {'Avg Cost':>8} | {'Diff':>6} | {'Score':>6} |")
    lines.append(f"|{'-'*6}|{'-'*12}|{'-'*11}|{'-'*10}|{'-'*10}|{'-'*8}|{'-'*8}|")
    for e in leaderboard:
        t = f"{e['avg_time']:.1f}s" if e["avg_time"] else "-"
        c = f"${e['avg_cost']:.4f}" if e["avg_cost"] else "-"
        d = f"{e['avg_diff_score']:.2f}" if e["avg_diff_score"] is not None else "-"
        lines.append(
            f"| {e['rank']:>4} | {e['tool']:<10} | {e['pass_rate']:>8.0%} | {t:>8} | {c:>8} | {d:>6} | {e['composite_score']:>6.2f} |"
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

    # Fallback: inline HTML
    tools_json = json.dumps([e["tool"] for e in leaderboard])
    pass_rates_json = json.dumps([e["pass_rate"] * 100 for e in leaderboard])
    times_json = json.dumps([e["avg_time"] or 0 for e in leaderboard])
    costs_json = json.dumps([e["avg_cost"] or 0 for e in leaderboard])
    scores_json = json.dumps([e["composite_score"] for e in leaderboard])

    rows_html = ""
    for r in agg["results"]:
        status = "PASS" if r.get("success") else "FAIL"
        t = f"{r.get('conversion_time_seconds', 0):.1f}s" if r.get("conversion_time_seconds") else "-"
        c = f"${r.get('cost_usd', 0):.4f}" if r.get("cost_usd") else "-"
        d = f"{r['diff_score']:.2f}" if r.get("diff_score") is not None else "-"
        rows_html += (
            f"<tr><td>{r.get('tool','')}</td><td>{r.get('policy_id','')}</td>"
            f"<td>{r.get('track','')}</td><td>{status}</td>"
            f"<td>{t}</td><td>{c}</td><td>{d}</td></tr>\n"
        )

    lb_rows = ""
    for e in leaderboard:
        t = f"{e['avg_time']:.1f}s" if e["avg_time"] else "-"
        c = f"${e['avg_cost']:.4f}" if e["avg_cost"] else "-"
        d = f"{e['avg_diff_score']:.2f}" if e["avg_diff_score"] is not None else "-"
        lb_rows += (
            f"<tr><td>{e['rank']}</td><td>{e['tool']}</td>"
            f"<td>{e['pass_rate']:.0%}</td><td>{t}</td><td>{c}</td>"
            f"<td>{d}</td><td>{e['composite_score']:.2f}</td></tr>\n"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Policy conversion &amp; generation benchmark</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {{ --bg: #0d1117; --fg: #c9d1d9; --card: #161b22; --border: #30363d; --accent: #58a6ff; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--fg); padding: 2rem; }}
  h1 {{ color: var(--accent); margin-bottom: 0.5rem; }}
  .meta {{ color: #8b949e; margin-bottom: 2rem; font-size: 0.9rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); gap: 1.5rem; margin-bottom: 2rem; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; }}
  .card h2 {{ font-size: 1.1rem; margin-bottom: 1rem; color: var(--accent); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--accent); font-weight: 600; }}
  canvas {{ max-height: 300px; }}
  .filter {{ margin-bottom: 1rem; }}
  .filter select {{ background: var(--card); color: var(--fg); border: 1px solid var(--border);
                    padding: 0.4rem 0.8rem; border-radius: 4px; }}
</style>
</head>
<body>
<h1>Policy conversion &amp; generation benchmark</h1>
<p class="meta">Generated: {agg['generated_at']}</p>

<div class="grid">
  <div class="card">
    <h2>Leaderboard</h2>
    <table>
      <tr><th>Rank</th><th>Tool</th><th>Pass Rate</th><th>Avg Time</th><th>Avg Cost</th><th>Diff</th><th>Score</th></tr>
      {lb_rows}
    </table>
  </div>

  <div class="card">
    <h2>Pass Rate (%)</h2>
    <canvas id="passChart"></canvas>
  </div>

  <div class="card">
    <h2>Avg Conversion Time (s)</h2>
    <canvas id="timeChart"></canvas>
  </div>

  <div class="card">
    <h2>Composite Score</h2>
    <canvas id="scoreChart"></canvas>
  </div>
</div>

<div class="card">
  <h2>All Results</h2>
  <div class="filter">
    <select id="toolFilter" onchange="filterTable()">
      <option value="">All tools</option>
    </select>
    <select id="trackFilter" onchange="filterTable()">
      <option value="">All tracks</option>
    </select>
  </div>
  <table id="resultsTable">
    <thead><tr><th>Tool</th><th>Policy</th><th>Track</th><th>Status</th><th>Time</th><th>Cost</th><th>Diff</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>

<script>
const tools = {tools_json};
const passRates = {pass_rates_json};
const times = {times_json};
const costs = {costs_json};
const scores = {scores_json};

const chartOpts = {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }};
const colors = ['#58a6ff','#f78166','#7ee787','#d2a8ff','#79c0ff','#ffa657'];

function mkBar(id, labels, data, label) {{
  new Chart(document.getElementById(id), {{
    type: 'bar',
    data: {{ labels, datasets: [{{ label, data, backgroundColor: colors.slice(0, labels.length) }}] }},
    options: chartOpts
  }});
}}
mkBar('passChart', tools, passRates, 'Pass Rate %');
mkBar('timeChart', tools, times, 'Avg Time (s)');
mkBar('scoreChart', tools, scores, 'Composite Score');

// Populate filters
const tbl = document.getElementById('resultsTable');
const rows = tbl.querySelectorAll('tbody tr');
const toolSet = new Set(), trackSet = new Set();
rows.forEach(r => {{ toolSet.add(r.cells[0].textContent); trackSet.add(r.cells[2].textContent); }});
const tf = document.getElementById('toolFilter'), tkf = document.getElementById('trackFilter');
toolSet.forEach(t => {{ const o = document.createElement('option'); o.value=t; o.textContent=t; tf.appendChild(o); }});
trackSet.forEach(t => {{ const o = document.createElement('option'); o.value=t; o.textContent=t; tkf.appendChild(o); }});

function filterTable() {{
  const tv = tf.value, tkv = tkf.value;
  rows.forEach(r => {{
    const show = (!tv || r.cells[0].textContent===tv) && (!tkv || r.cells[2].textContent===tkv);
    r.style.display = show ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""


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
