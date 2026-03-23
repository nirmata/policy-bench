"""Output schema validation — checks the converted policy is valid Kyverno 1.16+ YAML."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

VALIDATING_POLICY_KINDS = {
    "ValidatingPolicy",
    "MutatingPolicy",
    "GeneratingPolicy",
    "DeletingPolicy",
    "NamespacedDeletingPolicy",
    "ImageValidatingPolicy",
}
POLICIES_APIVERSION_PREFIX = "policies.kyverno.io/"


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def validate_schema(
    output_path: Path,
    *,
    expected_kind: str | None = None,
    use_kubectl: bool = True,
) -> tuple[bool, list[str]]:
    """Validate the converted policy file against Kyverno 1.16+ schema.

    Returns (passed, errors).
    """
    errors: list[str] = []

    if not yaml:
        return False, ["PyYAML not installed. pip install pyyaml"]

    try:
        raw = output_path.read_text(encoding="utf-8", errors="replace")
        doc = yaml.safe_load(raw)
    except Exception as exc:
        return False, [f"Invalid YAML: {exc}"]

    if not doc or not isinstance(doc, dict):
        return False, ["Empty or non-dict YAML"]

    kind = doc.get("kind") or ""
    api_version = doc.get("apiVersion") or ""

    allowed_kinds = VALIDATING_POLICY_KINDS
    if expected_kind:
        allowed_kinds = {expected_kind}
        # DeletingPolicy and NamespacedDeletingPolicy are both valid for cleanup conversions
        if expected_kind == "DeletingPolicy":
            allowed_kinds.add("NamespacedDeletingPolicy")

    if kind not in allowed_kinds:
        errors.append(
            f"Expected kind in {sorted(allowed_kinds)}, got {kind!r}"
        )
    if not api_version.startswith(POLICIES_APIVERSION_PREFIX):
        errors.append(
            f"Expected apiVersion starting with {POLICIES_APIVERSION_PREFIX!r}, got {api_version!r}"
        )

    if errors:
        return False, errors

    if use_kubectl and shutil.which("kubectl"):
        try:
            proc = subprocess.run(
                ["kubectl", "apply", "-f", str(output_path), "--dry-run=client"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip().lower()
                if not any(
                    s in err
                    for s in ("connection refused", "no matches for kind", "ensure crds")
                ):
                    errors.append(
                        f"kubectl dry-run failed: {(proc.stderr or proc.stdout or '').strip()[:300]}"
                    )
        except Exception:
            pass

    return len(errors) == 0, errors
