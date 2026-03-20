"""Input validator for Gatekeeper ConstraintTemplate + Constraint."""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def validate(path: Path, **_kwargs) -> tuple[bool, list[str]]:
    """Validate a Gatekeeper ConstraintTemplate (+ optional Constraint) YAML.

    Returns (passed, errors).
    """
    errors: list[str] = []

    if not yaml:
        return False, ["PyYAML not installed. pip install pyyaml"]

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        docs = list(yaml.safe_load_all(raw))
    except Exception as exc:
        return False, [f"Invalid YAML: {exc}"]

    if not docs:
        return False, ["Empty YAML file"]

    has_template = False
    for doc in docs:
        if not doc or not isinstance(doc, dict):
            continue
        kind = (doc.get("kind") or "").strip()
        api = (doc.get("apiVersion") or "").strip()

        if kind == "ConstraintTemplate":
            has_template = True
            if not api.startswith("templates.gatekeeper.sh/"):
                errors.append(
                    f"ConstraintTemplate: expected apiVersion starting with "
                    f"'templates.gatekeeper.sh/', got {api!r}"
                )
            spec = doc.get("spec") or {}
            if not spec.get("crd"):
                errors.append("ConstraintTemplate: missing spec.crd")
            targets = spec.get("targets") or []
            if not targets:
                errors.append("ConstraintTemplate: missing spec.targets")
            else:
                for i, t in enumerate(targets):
                    if not isinstance(t, dict):
                        continue
                    if not t.get("rego"):
                        errors.append(f"ConstraintTemplate target {i}: missing rego")

    if not has_template:
        errors.append("No ConstraintTemplate document found in file")

    return len(errors) == 0, errors
