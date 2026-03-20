"""Input validator for Kyverno CleanupPolicy."""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def validate(path: Path, **_kwargs) -> tuple[bool, list[str]]:
    """Validate a Kyverno CleanupPolicy YAML. Returns (passed, errors)."""
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
    api = (doc.get("apiVersion") or "").strip()

    if kind != "CleanupPolicy":
        errors.append(f"Expected kind: CleanupPolicy, got {kind!r}")
    if not api.startswith("kyverno.io/"):
        errors.append(f"Expected apiVersion starting with 'kyverno.io/', got {api!r}")

    spec = doc.get("spec") or {}
    if not spec.get("match"):
        errors.append("Missing spec.match")
    if not spec.get("schedule"):
        errors.append("Missing spec.schedule (CleanupPolicy requires a cron schedule)")

    return len(errors) == 0, errors
