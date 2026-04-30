"""Evaluation of AI-generated Kyverno Chainsaw test suites.

A generated suite is expected to include:
  chainsaw-test.yaml  - canonical Chainsaw entry manifest
  policy.yaml         - source policy copy placed by the benchmark harness

Evaluation layers:
  1. Schema: file existence + YAML parse checks
  2. Functional: `chainsaw test --test-dir <generated_dir>` exits zero
    3. Coverage: generated scenario count vs reference scenario count
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _count_scenarios(doc: object) -> int:
    """Count scenarios in a loose, format-tolerant way."""
    if isinstance(doc, dict):
        scenarios = doc.get("scenarios")
        if isinstance(scenarios, list):
            return len(scenarios)
        tests = doc.get("tests")
        if isinstance(tests, list):
            return len(tests)
        return 1
    if isinstance(doc, list):
        return len(doc)
    return 0


def evaluate_chainsawgen(
    *,
    generated_dir: Path,
    source_policy: Path,
    reference_dir: Path | None,
    timeout_sec: int = 120,
) -> dict:
    """Evaluate an AI-generated Chainsaw test suite.

    Returns result keys aligned with existing report fields and a dedicated
    `chainsaw_*` namespace, similar to test-generation benchmarking.
    """
    errors: list[str] = []
    test_manifest = generated_dir / "chainsaw-test.yaml"

    if not test_manifest.exists():
        return _failure_result(["Missing required file: chainsaw-test.yaml"])

    if not source_policy.exists():
        return _failure_result([f"Missing source policy in output dir: {source_policy}"])

    if yaml is None:
        return _failure_result(["PyYAML not installed; cannot validate chainsaw-test.yaml"])

    schema_pass = True
    parsed_doc: object = None
    manifest_text = ""
    try:
        manifest_text = test_manifest.read_text(encoding="utf-8")
        parsed_doc = yaml.safe_load(manifest_text)
        if parsed_doc is None:
            errors.append("chainsaw-test.yaml is empty")
            schema_pass = False
    except Exception as exc:
        errors.append(f"Failed to parse chainsaw-test.yaml: {exc}")
        schema_pass = False

    if not schema_pass:
        return _failure_result(errors)

    lower_manifest = manifest_text.lower()
    has_pass_and_fail = "pass" in lower_manifest and "fail" in lower_manifest

    chainsaw_bin = shutil.which("chainsaw")
    test_pass = False
    test_skipped = True
    if chainsaw_bin:
        test_skipped = False
        try:
            proc = subprocess.run(
                [chainsaw_bin, "test", "--test-dir", str(generated_dir)],
                cwd=str(generated_dir),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            test_pass = proc.returncode == 0
            if not test_pass:
                out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                errors.append(out or "chainsaw test exited non-zero with no output")
        except subprocess.TimeoutExpired:
            errors.append(f"chainsaw test timed out after {timeout_sec}s")
    else:
        errors.append("chainsaw CLI not found; functional validation skipped")

    generated_scenarios = _count_scenarios(parsed_doc)
    reference_scenarios = 0
    if reference_dir and yaml is not None:
        reference_manifest = reference_dir / "chainsaw-test.yaml"
        if reference_manifest.exists():
            try:
                reference_doc = yaml.safe_load(reference_manifest.read_text(encoding="utf-8"))
                reference_scenarios = _count_scenarios(reference_doc)
            except Exception:
                pass

    coverage_score = (
        min(generated_scenarios / reference_scenarios, 1.0) if reference_scenarios > 0 else 0.0
    )

    composite = schema_pass and (test_pass if not test_skipped else False) and has_pass_and_fail

    return {
        "chainsaw_schema_pass": schema_pass,
        "chainsaw_test_pass": test_pass if not test_skipped else None,
        "chainsaw_test_skipped": test_skipped,
        "chainsaw_coverage_score": round(coverage_score, 4),
        "chainsaw_reference_scenarios": reference_scenarios,
        "chainsaw_generated_scenarios": generated_scenarios,
        "chainsaw_has_pass_and_fail": has_pass_and_fail,
        "chainsaw_composite_pass": composite,
        "chainsaw_errors": errors,
        # Mirrors for existing report rendering
        "schema_pass": schema_pass,
        "semantic_pass": test_pass if not test_skipped else None,
        "semantic_skipped": test_skipped,
        "semantic_errors": errors,
    }


def _failure_result(errors: list[str]) -> dict:
    return {
        "chainsaw_schema_pass": False,
        "chainsaw_test_pass": None,
        "chainsaw_test_skipped": True,
        "chainsaw_coverage_score": 0.0,
        "chainsaw_reference_scenarios": 0,
        "chainsaw_generated_scenarios": 0,
        "chainsaw_has_pass_and_fail": False,
        "chainsaw_composite_pass": False,
        "chainsaw_errors": errors,
        # Mirrors
        "schema_pass": False,
        "semantic_pass": None,
        "semantic_skipped": True,
        "semantic_errors": [],
    }