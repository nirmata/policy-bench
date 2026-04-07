"""Input validator for legacy Kyverno ClusterPolicy (kyverno.io/v1)."""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def validate(path: Path, **kwargs) -> tuple[bool, list[str]]:
    """Validate a legacy ClusterPolicy YAML file. Returns (passed, errors)."""
    errors: list[str] = []

    if not yaml:
        return False, ["PyYAML not installed. pip install pyyaml"]

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        docs = list(yaml.safe_load_all(raw))
        doc = docs[0] if docs else None
    except Exception as exc:
        return False, [f"Invalid YAML: {exc}"]

    if not doc or not isinstance(doc, dict):
        return False, ["Empty or non-dict YAML document"]

    kind = (doc.get("kind") or "").strip()
    api_version = (doc.get("apiVersion") or "").strip()

    if kind != "ClusterPolicy":
        errors.append(f"Expected kind: ClusterPolicy, got {kind!r}")
    if not api_version.startswith("kyverno.io/"):
        errors.append(f"Expected apiVersion starting with 'kyverno.io/', got {api_version!r}")

    spec = doc.get("spec")
    if not spec or not isinstance(spec, dict):
        errors.append("Missing or invalid spec")
    else:
        rules = spec.get("rules")
        if not rules or not isinstance(rules, list):
            errors.append("spec.rules must be a non-empty list")
        else:
            for i, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    errors.append(f"Rule {i}: not a dict")
                    continue
                name = rule.get("name") or f"<rule {i}>"
                if not rule.get("match"):
                    errors.append(f"Rule {name}: missing 'match'")
                if not any(rule.get(k) for k in ("validate", "mutate", "generate", "verifyImages")):
                    errors.append(
                        f"Rule {name}: missing 'validate', 'mutate', 'generate', or 'verifyImages'"
                    )

    return len(errors) == 0, errors
