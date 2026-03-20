"""Main evaluation entry point.

Orchestrates schema validation, intent validation, semantic validation,
and diff scoring for a converted policy.

Supports two modes:
  - **Conversion** (input + output): full pipeline (schema + intent + semantic + diff)
  - **Generation** (output only): schema + semantic only (no intent or diff)
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from . import diff_scorer
from .intent_validator import validate_intent
from .schema_validator import validate_schema
from .semantic_validator import run_kyverno_test
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
    use_kubectl: bool = True,
    skip_kyverno_test: bool = False,
    kyverno_test_dir: Path | None = None,
    task_type: str = "convert",
) -> dict:
    """Run full evaluation and return a results dict.

    When *task_type* is ``"generate"`` (or *input_path* is ``None``),
    intent validation and diff scoring are skipped — only schema and
    semantic validation apply.

    Keys: schema_pass, schema_errors, intent_pass, intent_errors,
          semantic_pass, semantic_errors, semantic_skipped, diff_score.
    """
    is_generate = task_type == "generate" or input_path is None
    result: dict = {}

    # --- Schema ---
    schema_pass, schema_errors = validate_schema(
        output_path, expected_kind=expected_output_kind, use_kubectl=use_kubectl
    )
    result["schema_pass"] = schema_pass
    result["schema_errors"] = schema_errors

    # --- Load documents for intent + diff ---
    input_docs: list[dict] | None = None
    input_text: str | None = None
    output_doc: dict = {}

    if yaml:
        if input_path and not is_generate:
            try:
                raw = input_path.read_text(encoding="utf-8", errors="replace")
                input_text = raw
                if track in ("opa", "sentinel"):
                    pass  # raw text is enough
                else:
                    input_docs = [
                        d for d in yaml.safe_load_all(raw) if d and isinstance(d, dict)
                    ]
            except Exception:
                input_docs = None

        try:
            output_doc = yaml.safe_load(
                output_path.read_text(encoding="utf-8", errors="replace")
            ) or {}
        except Exception:
            output_doc = {}

    # --- Intent (skipped for generation tasks) ---
    if is_generate:
        result["intent_pass"] = None
        result["intent_errors"] = []
    else:
        intent_pass, intent_errors = validate_intent(
            track,
            str(input_path) if input_path else None,
            output_doc,
            input_docs=input_docs,
            input_text=input_text,
        )
        result["intent_pass"] = intent_pass
        result["intent_errors"] = intent_errors

    # --- Semantic ---
    semantic_pass = None
    semantic_errors: list[str] = []
    semantic_skipped = True

    if not skip_kyverno_test and kyverno_test_dir:
        output_policy_name = (output_doc.get("metadata") or {}).get("name")
        semantic_pass, semantic_errors, semantic_skipped = run_kyverno_test(
            kyverno_test_dir,
            output_policy_name=output_policy_name,
            policy_under_test=output_path,
        )

    result["semantic_pass"] = semantic_pass if not semantic_skipped else None
    result["semantic_errors"] = semantic_errors
    result["semantic_skipped"] = semantic_skipped

    # --- Diff score (skipped for generation tasks) ---
    if is_generate:
        result["diff_score"] = None
    else:
        input_doc_for_score = input_docs[0] if input_docs else None
        result["diff_score"] = diff_scorer.score(input_doc_for_score, output_doc)

    return result
