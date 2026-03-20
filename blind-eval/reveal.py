#!/usr/bin/env python3
"""
Reveal tool identities and merge human scores back into results.

Reads .mapping.json and scores.json, then writes merged results
to blind-eval/revealed_scores.json.

Usage:
  python3 blind-eval/reveal.py
  python3 blind-eval/reveal.py --scores blind-eval/scores.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BLIND_DIR = Path(__file__).resolve().parent
MAPPING_FILE = BLIND_DIR / ".mapping.json"
DEFAULT_SCORES = BLIND_DIR / "scores.json"


def reveal(scores_path: Path | None = None) -> list[dict]:
    """Merge anonymized scores with the tool mapping."""
    scores_path = scores_path or DEFAULT_SCORES

    if not MAPPING_FILE.exists():
        print(f"Error: mapping not found at {MAPPING_FILE}. Run anonymize.py first.", file=sys.stderr)
        return []
    if not scores_path.exists():
        print(f"Error: scores file not found at {scores_path}.", file=sys.stderr)
        print("Use the judge_form.html to score submissions, then save as scores.json.", file=sys.stderr)
        return []

    mapping = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    scores = json.loads(scores_path.read_text(encoding="utf-8"))

    # Build lookup: anon_id -> scores dict
    scores_by_id: dict[str, dict] = {}
    if isinstance(scores, list):
        for s in scores:
            scores_by_id[s.get("anon_id", "")] = s
    elif isinstance(scores, dict):
        scores_by_id = scores

    merged: list[dict] = []
    for entry in mapping:
        anon_id = entry["anon_id"]
        score_data = scores_by_id.get(anon_id, {})
        merged.append({
            **entry,
            "correctness": score_data.get("correctness"),
            "completeness": score_data.get("completeness"),
            "readability": score_data.get("readability"),
            "notes": score_data.get("notes", ""),
        })

    out_path = BLIND_DIR / "revealed_scores.json"
    out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"  Revealed scores: {out_path}")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="Reveal blind evaluation results")
    parser.add_argument("--scores", type=Path, help="Path to scores JSON")
    args = parser.parse_args()

    entries = reveal(args.scores)
    if not entries:
        return 1

    print(f"\n  {'Tool':<10} {'Policy':<35} {'Correct':>8} {'Complete':>9} {'Readable':>9}")
    print("  " + "-" * 75)
    for e in entries:
        c = e.get("correctness") or "-"
        comp = e.get("completeness") or "-"
        r = e.get("readability") or "-"
        print(f"  {e['tool']:<10} {e['policy_id']:<35} {str(c):>8} {str(comp):>9} {str(r):>9}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
