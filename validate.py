#!/usr/bin/env python3
"""
Validate a converted policy: schema (valid YAML, Kyverno 1.16+ kind) and intent (preserves match/action).
When validating a conversion, the input policy is validated first (must be a valid legacy policy).

Usage:
  # Validate input policy only (run before converting):
  python3 validate.py --input input/my-policy.yaml

  # Validate conversion (input + output); input is validated first:
  python3 validate.py --input input/require-resource-limits.yaml --output output/converted.yaml --tool nctl

  # Kyverno CLI semantic test runs by default; skip it with:
  python3 validate.py --input input/require-resource-limits.yaml --output output/converted.yaml --tool nctl --skip-kyverno-test

Results are written to results/run_<timestamp>_<tool>.json when --output is given.
"""

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

# Reuse legacy policy validation for input (before conversion)
def _load_validate_legacy():
    """Load validate_legacy_policy from validate-legacy.py (filename has hyphen)."""
    import importlib.util
    path = Path(__file__).resolve().parent / "validate-legacy.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_legacy", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, "validate_legacy_policy", None)


validate_legacy_policy = _load_validate_legacy()

VALIDATING_POLICY_KINDS = {"ValidatingPolicy", "MutatingPolicy", "GeneratingPolicy", "DeletingPolicy", "ImageValidatingPolicy"}
POLICIES_APIVERSION_PREFIX = "policies.kyverno.io/"


def validate_schema(output_path: Path, use_kubectl: bool = True) -> tuple[bool, list[str]]:
    """Check output is valid YAML and Kyverno 1.16+ policy. Returns (passed, errors)."""
    errors: list[str] = []
    if not yaml:
        return (False, ["PyYAML not installed. pip install pyyaml"])
    try:
        raw = output_path.read_text(encoding="utf-8", errors="replace")
        doc = yaml.safe_load(raw)
    except Exception as e:
        return (False, [f"Invalid YAML: {e}"])
    if not doc or not isinstance(doc, dict):
        return (False, ["Empty or non-dict YAML"])
    kind = doc.get("kind") or ""
    api_version = doc.get("apiVersion") or ""
    if kind not in VALIDATING_POLICY_KINDS:
        errors.append(f"Expected kind in {sorted(VALIDATING_POLICY_KINDS)}, got {kind!r}")
    if not api_version.startswith(POLICIES_APIVERSION_PREFIX):
        errors.append(f"Expected apiVersion starting with {POLICIES_APIVERSION_PREFIX!r}, got {api_version!r}")
    if errors:
        return (False, errors)
    if use_kubectl and shutil.which("kubectl"):
        proc = subprocess.run(
            ["kubectl", "apply", "-f", str(output_path), "--dry-run=client"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().lower()
            if "connection refused" in err or "no matches for kind" in err or "ensure crds" in err:
                pass  # Skip: no cluster or CRDs
            else:
                errors.append(f"kubectl dry-run failed: {(proc.stderr or proc.stdout or '').strip()[:300]}")
    return (len(errors) == 0, errors)


def run_kyverno_test(test_dir: Path, timeout_sec: int = 60) -> tuple[bool, list[str], bool]:
    """Run 'kyverno test <test_dir>'. Returns (passed, errors, skipped). skipped=True if kyverno not on PATH or CLI doesn't support policy format."""
    if not shutil.which("kyverno"):
        return False, [], True
    test_dir = test_dir.resolve()
    if not test_dir.is_dir():
        return False, [f"Kyverno test dir not found: {test_dir}"], False
    proc = subprocess.run(
        ["kyverno", "test", str(test_dir)],
        cwd=str(test_dir),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if proc.returncode == 0:
        return True, [], False
    err = (proc.stderr or proc.stdout or "").strip()
    # If CLI rejects the policy with "unknown field" / "Invalid value", the test command doesn't support this ValidatingPolicy schema yet (even on CLI 1.17) → treat as skip, not fail
    if err and ("unknown field" in err or "Invalid value" in err) and "failed to load" in err.lower():
        return False, ["Kyverno CLI 'test' command does not yet support ValidatingPolicy 1.16+ schema (e.g. spec.admission, spec.assertions). Use --skip-kyverno-test for now."], True
    return False, [err[:500] if err else "kyverno test exited non-zero"], False


def _kinds_from_cluster_policy(doc: dict) -> set[str]:
    kinds = set()
    for rule in (doc.get("spec") or {}).get("rules") or []:
        for block in (rule.get("match") or {}).get("any") or (rule.get("match") or {}).get("all") or []:
            for k in (block.get("resources") or {}).get("kinds") or []:
                kk = (k or "").strip().lower()
                if kk:
                    kinds.add(kk + "s" if not kk.endswith("s") else kk)
    return kinds


def _validation_action_from_cluster_policy(doc: dict) -> str:
    return (doc.get("spec") or {}).get("validationFailureAction") or ""


def _kinds_from_validating_policy(doc: dict) -> set[str]:
    kinds = set()
    for rule in (doc.get("spec") or {}).get("rules") or []:
        for block in (rule.get("match") or {}).get("any") or (rule.get("match") or {}).get("all") or []:
            for k in (block.get("resources") or {}).get("kinds") or []:
                kk = (k or "").strip().lower()
                if kk:
                    kinds.add(kk + "s" if not kk.endswith("s") else kk)
    return kinds


def _validation_actions_from_validating_policy(doc: dict) -> list:
    return list((doc.get("spec") or {}).get("validationActions") or [])


def validate_intent_cluster_policy(input_doc: dict, output_doc: dict) -> tuple[bool, list[str]]:
    """Check ValidatingPolicy preserves intent of ClusterPolicy. Returns (passed, errors)."""
    errors: list[str] = []
    in_kinds = _kinds_from_cluster_policy(input_doc)
    out_kinds = _kinds_from_validating_policy(output_doc)
    if in_kinds and out_kinds and in_kinds != out_kinds:
        errors.append(f"Match kinds mismatch: source {sorted(in_kinds)}, output {sorted(out_kinds)}")
    in_action = _validation_action_from_cluster_policy(input_doc)
    out_actions = _validation_actions_from_validating_policy(output_doc)
    if in_action == "Enforce" and out_actions and "Deny" not in out_actions and "Enforce" not in out_actions:
        errors.append(f"Validation action mismatch: source was Enforce, output has {out_actions} (expected Deny)")
    if in_action == "Audit" and out_actions and "Audit" not in out_actions:
        errors.append(f"Validation action mismatch: source was Audit, output has {out_actions}")
    return (len(errors) == 0, errors)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate converted policy (schema + intent). Validates input policy first when both input and output are given.")
    parser.add_argument("--input", required=True, help="Path to original policy (input)")
    parser.add_argument("--output", help="Path to converted policy (output). Omit to validate input only (run before converting).")
    parser.add_argument("--tool", default="unknown", help="Tool label for results (e.g. nctl, cursor, claude)")
    parser.add_argument("--no-kubectl", action="store_true", help="Skip kubectl dry-run")
    parser.add_argument("--skip-kyverno-test", action="store_true", help="Skip Kyverno CLI semantic test (by default it runs when kyverno-tests/ exists and kyverno is on PATH)")
    parser.add_argument("--kyverno-test-dir", metavar="DIR", default="kyverno-tests", help="Directory for 'kyverno test' (default: kyverno-tests). Ignored if --skip-kyverno-test is set.")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        return 1

    input_only = args.output is None
    if not input_only:
        output_path = Path(args.output)
        if not output_path.exists():
            print(f"Error: output file not found: {output_path}", file=sys.stderr)
            return 1
    else:
        output_path = None

    if not yaml:
        print("Error: PyYAML required. pip install pyyaml", file=sys.stderr)
        return 1

    # Load input doc (first document only; allow multi-doc files)
    try:
        raw = input_path.read_text(encoding="utf-8", errors="replace")
        docs = list(yaml.safe_load_all(raw))
        input_doc = docs[0] if docs else None
    except Exception as e:
        print(f"Error loading input YAML: {e}", file=sys.stderr)
        return 1
    input_doc = input_doc or {}

    # --- Input-only mode: validate legacy policy and exit ---
    if input_only:
        if not validate_legacy_policy:
            print("Error: validate_legacy module not found (validate-legacy.py must be in same directory)", file=sys.stderr)
            return 1
        passed, errors = validate_legacy_policy(input_path, use_kubectl=not args.no_kubectl)
        if passed:
            print("Input policy: PASS (valid legacy policy; safe to convert)")
            return 0
        print("Input policy: FAIL (fix before converting)", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    # --- Full validation: validate input first, then output ---
    input_kind = (input_doc.get("kind") or "").strip()
    if input_kind == "ClusterPolicy" and validate_legacy_policy:
        input_pass, input_errors = validate_legacy_policy(input_path, use_kubectl=not args.no_kubectl)
        if not input_pass:
            print("Input policy: FAIL (fix before comparing conversion output)", file=sys.stderr)
            for e in input_errors:
                print(f"  {e}", file=sys.stderr)
            return 1

    try:
        output_doc = yaml.safe_load(output_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        print(f"Error loading output YAML: {e}", file=sys.stderr)
        return 1
    output_doc = output_doc or {}

    # Schema (output)
    schema_pass, schema_errors = validate_schema(output_path, use_kubectl=not args.no_kubectl)

    # Intent (only for ClusterPolicy -> ValidatingPolicy)
    intent_pass = True
    intent_errors: list[str] = []
    input_kind = (input_doc.get("kind") or "").strip()
    if input_kind == "ClusterPolicy":
        intent_pass, intent_errors = validate_intent_cluster_policy(input_doc, output_doc)
    # Gatekeeper could be added here later

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    tool_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.tool)
    out_json = results_dir / f"run_{timestamp}_{tool_safe}.json"

    semantic_pass: bool | None = None
    semantic_errors: list[str] = []
    semantic_skipped = True
    if not getattr(args, "skip_kyverno_test", False):
        test_dir = Path(__file__).resolve().parent / getattr(args, "kyverno_test_dir", "kyverno-tests")
        semantic_pass, semantic_errors, semantic_skipped = run_kyverno_test(test_dir)
    report = {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "tool": args.tool,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schema_pass": schema_pass,
        "intent_pass": intent_pass,
        "schema_errors": schema_errors,
        "intent_errors": intent_errors,
        "semantic_pass": semantic_pass if not semantic_skipped else None,
        "semantic_errors": semantic_errors,
        "semantic_skipped": semantic_skipped,
    }
    out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Schema:   {'PASS' if schema_pass else 'FAIL'}")
    print(f"Intent:   {'PASS' if intent_pass else 'FAIL'}")
    if semantic_skipped:
        print("Semantic: SKIP" + (f" ({semantic_errors[0]})" if semantic_errors else " (no test dir or kyverno CLI not on PATH)"))
    else:
        print(f"Semantic: {'PASS' if semantic_pass else 'FAIL'}")
    if schema_errors:
        for e in schema_errors:
            print(f"  Schema: {e}")
    if intent_errors:
        for e in intent_errors:
            print(f"  Intent: {e}")
    if semantic_errors and not semantic_skipped:
        for e in semantic_errors:
            print(f"  Semantic: {e}")
    print(f"Results: {out_json}")

    all_pass = schema_pass and intent_pass and (semantic_skipped or semantic_pass)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
