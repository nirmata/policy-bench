#!/usr/bin/env python3
"""
Validate a converted or generated policy.

Three modes:
  1. **Input-only** — validate a source policy before converting.
  2. **Conversion** (input + output) — schema + CEL + functional test.
  3. **Generation / output-only** (output only, no input) — schema + CEL.

Usage:
  # Validate input policy only:
  python3 validate.py --input input/my-policy.yaml

  # Validate conversion (input + output):
  python3 validate.py --input input/require-resource-limits.yaml --output output/converted.yaml --tool nctl

  # Validate generated policy (output only — no source to compare):
  python3 validate.py --output output/generated-vpol.yaml --tool claude

  # Skip Kyverno CLI semantic test:
  python3 validate.py --input input/policy.yaml --output output/policy.yaml --tool nctl --skip-kyverno-test

Results are written to results/run_<timestamp>_<tool>.json when --output is given.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

from evaluators.evaluate import evaluate, validate_input


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate converted or generated policy (schema + CEL + functional). "
        "Omit --input for generation-only validation (schema + CEL)."
    )
    parser.add_argument(
        "--input",
        required=False,
        help="Path to original policy (omit for generation-only validation)",
    )
    parser.add_argument(
        "--output",
        help="Path to converted/generated policy. Omit to validate input only.",
    )
    parser.add_argument(
        "--tool", default="unknown", help="Tool label for results (e.g. nctl, cursor, claude)"
    )
    parser.add_argument("--track", default=None, help="Conversion track (auto-detected if omitted)")
    parser.add_argument(
        "--skip-kyverno-test",
        action="store_true",
        help="Skip Kyverno CLI semantic test",
    )
    parser.add_argument(
        "--kyverno-test-dir",
        metavar="DIR",
        default="kyverno-tests",
        help="Kyverno test directory: repo-relative (default: kyverno-tests) or under dataset/ (e.g. imported/kyverno-tests/cp_require_labels)",
    )
    parser.add_argument(
        "--expected-kind",
        help="Expected output kind (e.g. ValidatingPolicy, MutatingPolicy)",
    )
    args = parser.parse_args()

    if args.input is None and args.output is None:
        parser.error("At least one of --input or --output is required.")

    input_path: Path | None = None
    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: input file not found: {input_path}", file=sys.stderr)
            return 1

    output_path: Path | None = None
    if args.output:
        output_path = Path(args.output)
        if not output_path.exists():
            print(f"Error: output file not found: {output_path}", file=sys.stderr)
            return 1

    # Auto-detect track from input (or default for generation)
    track = args.track
    if not track and input_path:
        track = _detect_track(input_path)
    if not track:
        track = "cluster-policy"

    # Determine mode
    is_generate = input_path is None and output_path is not None
    input_only = output_path is None and input_path is not None

    # --- Input-only mode ---
    if input_only:
        passed, errors = validate_input(track, input_path, use_kubectl=True)
        if passed:
            print("Input policy: PASS (valid legacy policy; safe to convert)")
            return 0
        print("Input policy: FAIL (fix before converting)", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    # --- Conversion mode: validate input first ---
    if not is_generate and input_path:
        input_pass, input_errors = validate_input(
            track, input_path, use_kubectl=True
        )
        if not input_pass:
            print("Input policy: FAIL (fix before comparing conversion output)", file=sys.stderr)
            for e in input_errors:
                print(f"  {e}", file=sys.stderr)
            return 1

    repo_root = Path(__file__).resolve().parent
    rel = args.kyverno_test_dir.strip("/").replace("\\", "/")
    if rel.startswith("imported/") or rel.startswith("local/"):
        kyverno_test_dir = repo_root / "dataset" / rel
    else:
        kyverno_test_dir = repo_root / rel
    task_type = "generate" if is_generate else "convert"
    eval_result = evaluate(
        track,
        input_path,
        output_path,
        expected_output_kind=args.expected_kind,
        skip_kyverno_test=args.skip_kyverno_test,
        kyverno_test_dir=kyverno_test_dir if kyverno_test_dir.is_dir() else None,
        task_type=task_type,
    )

    # --- Write results JSON ---
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    tool_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.tool)
    out_json = results_dir / f"run_{timestamp}_{tool_safe}.json"

    report = {
        "input_path": str(input_path) if input_path else None,
        "output_path": str(output_path),
        "tool": args.tool,
        "track": track,
        "task_type": task_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **eval_result,
    }
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # --- Pretty summary ---
    schema_pass = eval_result["schema_pass"]
    semantic_pass = eval_result.get("semantic_pass")
    semantic_skipped = eval_result.get("semantic_skipped", True)
    schema_errors = eval_result.get("schema_errors", [])
    semantic_errors = eval_result.get("semantic_errors", [])

    print()
    mode_label = "Generation" if is_generate else "Conversion"
    print(f"  {mode_label} validation results")
    print("  " + "-" * 40)

    # Show generated policy identity
    gen_api = eval_result.get("generated_api_version", "")
    gen_kind = eval_result.get("generated_kind", "")
    gen_name = eval_result.get("generated_name", "")
    if gen_api or gen_kind or gen_name:
        print(f"  Generated: apiVersion={gen_api}  kind={gen_kind}  name={gen_name}")
    stage = eval_result.get("validation_stage", "")
    if stage and stage != "passed":
        print(f"  Failed at: {stage}")

    print(
        f"  1. Schema+CEL  {'PASS' if schema_pass else 'FAIL'}"
        f"  -- valid structure, CEL compiles"
    )
    for e in schema_errors:
        print(f"      - {e}")

    if semantic_skipped:
        reason = semantic_errors[0] if semantic_errors else "no test dir or kyverno CLI not on PATH"
        print(f"  2. Functional  SKIP  -- {reason}")
    else:
        print(
            f"  2. Functional  {'PASS' if semantic_pass else 'FAIL'}"
            f"  -- kyverno test (policy behavior)"
        )
        for e in semantic_errors:
            for i, line in enumerate(e.splitlines()):
                prefix = "      - " if i == 0 else "        "
                print(f"{prefix}{line}")

    print("  " + "-" * 40)
    print(f"  Results: {out_json}")
    print()

    all_pass = schema_pass and (semantic_skipped or semantic_pass)
    return 0 if all_pass else 1


def _detect_track(input_path: Path) -> str:
    """Guess the conversion track from the input file."""
    suffix = input_path.suffix.lower()
    if suffix == ".rego":
        return "opa"
    if suffix == ".sentinel":
        return "sentinel"

    if yaml:
        try:
            raw = input_path.read_text(encoding="utf-8", errors="replace")
            docs = list(yaml.safe_load_all(raw))
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                kind = (doc.get("kind") or "").strip()
                if kind == "ClusterPolicy":
                    return "cluster-policy"
                if kind == "ConstraintTemplate":
                    return "gatekeeper"
                if kind == "CleanupPolicy":
                    return "cleanup"
        except Exception:
            pass

    return "cluster-policy"


if __name__ == "__main__":
    sys.exit(main())
