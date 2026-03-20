"""Input validator for legacy Kyverno ClusterPolicy (kyverno.io/v1)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def validate(path: Path, *, use_kubectl: bool = True) -> tuple[bool, list[str]]:
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
                validate_block = rule.get("validate")
                if not validate_block and "validate" not in rule:
                    if not rule.get("mutate") and not rule.get("generate") and not rule.get(
                        "verifyImages"
                    ):
                        errors.append(
                            f"Rule {name}: missing 'validate', 'mutate', 'generate', or 'verifyImages'"
                        )
                if validate_block and isinstance(validate_block, dict):
                    if not any(
                        k in validate_block
                        for k in ("pattern", "anyPattern", "deny", "message")
                    ):
                        errors.append(
                            f"Rule {name}: validate should have pattern/anyPattern/deny and message"
                        )

    if errors:
        return False, errors

    if use_kubectl and shutil.which("kubectl"):
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", str(path), "--dry-run=client"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()
                if not any(
                    s in err.lower()
                    for s in ("no matches for kind", "ensure crds")
                ):
                    errors.append(f"kubectl dry-run: {err[:400]}")
        except Exception:
            pass

    return len(errors) == 0, errors
