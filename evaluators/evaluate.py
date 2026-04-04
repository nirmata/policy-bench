"""Main evaluation entry point.

Orchestrates schema/CEL validation and functional testing for a converted policy.

Supports two modes:
  - **Conversion** (input + output): schema + CEL + functional test
  - **Generation** (output only): schema + CEL + functional test (if test dir provided)
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from .go_validator import validate_with_go
from .schema_validator import validate_schema
from .semantic_validator import run_kyverno_test
from .structural_lint import lint as structural_lint
from .input_validators import (
    cluster_policy,
    gatekeeper,
    opa,
    sentinel,
    cleanup,
)

INPUT_VALIDATORS = {
    "cluster-policy": cluster_policy.validate,
    "gatekeeper": gatekeeper.validate,
    "opa": opa.validate,
    "sentinel": sentinel.validate,
    "cleanup": cleanup.validate,
}


def validate_input(track: str, input_path: Path, **kwargs) -> tuple[bool, list[str]]:
    """Validate the source policy based on its track."""
    validator = INPUT_VALIDATORS.get(track)
    if validator is None:
        return False, [f"No input validator for track {track!r}"]
    return validator(input_path, **kwargs)


def evaluate(
    track: str,
    input_path: Path | None,
    output_path: Path,
    *,
    expected_output_kind: str | None = None,
    skip_kyverno_test: bool = False,
    kyverno_test_dir: Path | None = None,
    task_type: str = "convert",
) -> dict:
    """Run evaluation and return a results dict.

    Two layers:
      1. Schema + CEL (Go validator preferred, Python fallback)
      2. Functional test (kyverno test with real resources)

    Keys: schema_pass, schema_errors, semantic_pass, semantic_errors,
          semantic_skipped.
    """
    result: dict = {}

    # --- Schema + CEL (Go validator preferred, Python fallback) ---
    go_result = validate_with_go(output_path)
    if go_result is not None:
        schema_pass = go_result["schema_pass"] and go_result["cel_pass"]
        schema_errors = list(go_result.get("errors", []))

        if schema_pass and expected_output_kind:
            actual_kind = go_result.get("policy_kind", "")
            allowed = {expected_output_kind}
            if expected_output_kind == "DeletingPolicy":
                allowed.add("NamespacedDeletingPolicy")
            if actual_kind not in allowed:
                schema_pass = False
                schema_errors.append(
                    f"Expected kind {sorted(allowed)}, got {actual_kind!r}"
                )
    else:
        schema_pass, schema_errors = validate_schema(
            output_path, expected_kind=expected_output_kind
        )
    result["schema_pass"] = schema_pass
    result["schema_errors"] = schema_errors

    # --- Structural lint (between schema and functional test) ---
    lint_pass, lint_warnings = structural_lint(output_path)
    result["lint_pass"] = lint_pass
    result["lint_warnings"] = lint_warnings

    # --- Functional test (kyverno test) ---
    semantic_pass = None
    semantic_errors: list[str] = []
    semantic_skipped = True

    if not skip_kyverno_test and kyverno_test_dir:
        # Reuse Go validator output if available to avoid re-parsing YAML
        if go_result is not None:
            output_policy_name = go_result.get("policy_name")
            output_policy_kind = go_result.get("policy_kind", "")
        else:
            output_doc: dict = {}
            if yaml:
                try:
                    output_doc = yaml.safe_load(
                        output_path.read_text(encoding="utf-8", errors="replace")
                    ) or {}
                except Exception:
                    output_doc = {}
            output_policy_name = (output_doc.get("metadata") or {}).get("name")
            output_policy_kind = output_doc.get("kind", "")
        semantic_pass, semantic_errors, semantic_skipped = run_kyverno_test(
            kyverno_test_dir,
            output_policy_name=output_policy_name,
            output_policy_kind=output_policy_kind,
            policy_under_test=output_path,
        )

    result["semantic_pass"] = semantic_pass if not semantic_skipped else None
    result["semantic_errors"] = semantic_errors
    result["semantic_skipped"] = semantic_skipped

    return result
