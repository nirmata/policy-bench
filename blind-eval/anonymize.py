#!/usr/bin/env python3
"""
Anonymize converted policy outputs for blind human evaluation.

Reads output/<tool>/<policy_id>.yaml files, strips tool identity,
assigns random IDs, and writes to blind-eval/anonymized/.
A sealed mapping is saved to blind-eval/.mapping.json (gitignored).

Usage:
  python3 blind-eval/anonymize.py
  python3 blind-eval/anonymize.py --output-dir output/
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BLIND_DIR = Path(__file__).resolve().parent
ANON_DIR = BLIND_DIR / "anonymized"
MAPPING_FILE = BLIND_DIR / ".mapping.json"


def anonymize(output_dir: Path | None = None) -> list[dict]:
    """Anonymize all outputs and return the mapping entries."""
    output_dir = output_dir or (REPO_ROOT / "output")
    if not output_dir.is_dir():
        print(f"Error: output directory not found: {output_dir}", file=sys.stderr)
        return []

    if ANON_DIR.exists():
        shutil.rmtree(ANON_DIR)
    ANON_DIR.mkdir(parents=True)

    mapping: list[dict] = []

    for tool_dir in sorted(output_dir.iterdir()):
        if not tool_dir.is_dir() or tool_dir.name.startswith("."):
            continue
        tool_name = tool_dir.name

        for policy_file in sorted(tool_dir.glob("*.yaml")):
            anon_id = f"submission_{secrets.token_hex(4)}"
            dest = ANON_DIR / f"{anon_id}.yaml"
            shutil.copy(policy_file, dest)

            mapping.append({
                "anon_id": anon_id,
                "tool": tool_name,
                "policy_id": policy_file.stem,
                "original_path": str(policy_file),
                "anonymized_path": str(dest),
            })

    MAPPING_FILE.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    print(f"  Anonymized {len(mapping)} outputs -> {ANON_DIR}")
    print(f"  Mapping (sealed): {MAPPING_FILE}")
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description="Anonymize outputs for blind evaluation")
    parser.add_argument("--output-dir", type=Path, help="Override output directory")
    args = parser.parse_args()

    entries = anonymize(args.output_dir)
    if not entries:
        print("No outputs found to anonymize.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
