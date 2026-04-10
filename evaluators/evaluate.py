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

    Four layers (in order):
      1. Expected kind (cheap fail-fast — skipped when not specified)
      2. Schema + CEL (Go validator preferred, Python fallback)
      3. Structural lint (catches common MutatingPolicy issues)
      4. Functional test (kyverno test with real resources)

    Keys: expected_kind_pass, expected_kind_errors, expected_kind_skipped,
          schema_pass, schema_errors, lint_pass, lint_warnings,
          semantic_pass, semantic_errors, semantic_skipped, validator_used.
    """
    result: dict = {}

    # --- Expected kind (cheapest possible fail-fast check) ---
    kind_pass = True
    kind_errors: list[str] = []
    kind_skipped = expected_output_kind is None

    if expected_output_kind and yaml is None:
        kind_pass = False
        kind_errors.append("PyYAML not installed; cannot check expected kind")
    elif expected_output_kind:
        try:
            raw = output_path.read_text(encoding="utf-8", errors="replace")
            doc = yaml.safe_load(raw)
            if isinstance(doc, dict):
                actual_kind = doc.get("kind", "")
                allowed = {expected_output_kind}
                if expected_output_kind == "DeletingPolicy":
                    allowed.add("NamespacedDeletingPolicy")
                if actual_kind not in allowed:
                    kind_pass = False
                    kind_errors.append(
                        f"Expected kind {sorted(allowed)}, got {actual_kind!r}"
                    )
                    # Populate identity for diagnostics on the early-return path;
                    # on the happy path the downstream validator sets these.
                    result["generated_api_version"] = doc.get("apiVersion") or ""
                    result["generated_kind"] = actual_kind
                    result["generated_name"] = (doc.get("metadata") or {}).get("name") or ""
            else:
                kind_pass = False
                kind_errors.append("Output is not a YAML mapping")
        except Exception as exc:
            kind_pass = False
            kind_errors.append(f"Failed to parse YAML for kind check: {exc}")

    result["expected_kind_pass"] = kind_pass
    result["expected_kind_errors"] = kind_errors
    result["expected_kind_skipped"] = kind_skipped

    if not kind_pass:
        result["validation_stage"] = "expected_kind"
        result.setdefault("generated_api_version", "")
        result.setdefault("generated_kind", "")
        result.setdefault("generated_name", "")
        result["schema_pass"] = False
        result["schema_errors"] = []
        result["validator_used"] = "skipped"
        result["lint_pass"] = None
        result["lint_warnings"] = []
        result["semantic_pass"] = None
        result["semantic_errors"] = []
        result["semantic_skipped"] = True
        return result

    # --- Schema + CEL (Go validator preferred, Python fallback) ---
    go_result = validate_with_go(output_path)
    if go_result is not None:
        result["validator_used"] = go_result.get("validator_used", "go")
        schema_pass = go_result["schema_pass"] and go_result["cel_pass"]
        schema_errors = list(go_result.get("errors", []))

        # Propagate generated policy identity for diagnostics
        result["generated_api_version"] = go_result.get("api_version", "")
        result["generated_kind"] = go_result.get("policy_kind", "")
        result["generated_name"] = go_result.get("policy_name", "")
        result["validation_stage"] = go_result.get("validation_stage", "")
    else:
        result["validator_used"] = "python_fallback"
        schema_pass, schema_errors, parsed_doc = validate_schema(output_path)
        # Extract identity from the already-parsed doc (no re-read)
        if parsed_doc and isinstance(parsed_doc, dict):
            result["generated_api_version"] = parsed_doc.get("apiVersion") or ""
            result["generated_kind"] = parsed_doc.get("kind") or ""
            result["generated_name"] = (parsed_doc.get("metadata") or {}).get("name") or ""
        else:
            result["generated_api_version"] = ""
            result["generated_kind"] = ""
            result["generated_name"] = ""
        # Classify stage based on which schema check failed
        if schema_pass:
            result["validation_stage"] = "passed"
        elif any("Invalid YAML" in e for e in schema_errors):
            result["validation_stage"] = "yaml_parse"
        elif any("apiVersion" in e for e in schema_errors):
            result["validation_stage"] = "schema_lookup"
        else:
            result["validation_stage"] = "schema_validation"

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
        # Reuse Go validator output or parsed doc to avoid re-reading YAML
        if go_result is not None:
            output_policy_name = go_result.get("policy_name")
            output_policy_kind = go_result.get("policy_kind", "")
        elif parsed_doc and isinstance(parsed_doc, dict):
            output_policy_name = (parsed_doc.get("metadata") or {}).get("name")
            output_policy_kind = parsed_doc.get("kind", "")
        else:
            output_policy_name = None
            output_policy_kind = ""
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
