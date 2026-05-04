"""Input validator for Terraform-track ValidatingPolicy (JSON eval mode)."""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def validate(path: Path, **kwargs) -> tuple[bool, list[str]]:
    """Validate a Terraform-track ValidatingPolicy. Returns (passed, errors)."""
    errors: list[str] = []

    if not yaml:
        return False, ["PyYAML not installed. pip install pyyaml"]

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        doc = yaml.safe_load(raw)
    except Exception as exc:
        return False, [f"Invalid YAML: {exc}"]

    if not doc or not isinstance(doc, dict):
        return False, ["Empty or non-dict YAML document"]

    kind = (doc.get("kind") or "").strip()
    api_version = (doc.get("apiVersion") or "").strip()

    if kind != "ValidatingPolicy":
        errors.append(f"Expected kind: ValidatingPolicy, got {kind!r}")
    if not api_version.startswith("policies.kyverno.io/"):
        errors.append(f"Expected apiVersion starting with 'policies.kyverno.io/', got {api_version!r}")

    spec = doc.get("spec") or {}
    evaluation = spec.get("evaluation") or {}
    if evaluation.get("mode") != "JSON":
        errors.append("spec.evaluation.mode must be 'JSON' for Terraform policies")

    match_conditions = spec.get("matchConditions") or []
    has_plan_check = any(
        "object.planned_values" in (c.get("expression") or "")
        for c in match_conditions
        if isinstance(c, dict)
    )
    if not has_plan_check:
        errors.append(
            "matchConditions must include a condition checking has(object.planned_values) "
            "to identify Terraform plan resources"
        )

    validations = spec.get("validations") or []
    if not validations:
        errors.append("spec.validations must be a non-empty list")

    return len(errors) == 0, errors
