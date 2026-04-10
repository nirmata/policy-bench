"""Output schema validation (Python fallback when Go validator is unavailable)."""

from __future__ import annotations

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


def validate_schema(
    output_path: Path,
) -> tuple[bool, list[str], dict | None]:
    """Validate the converted policy file against Kyverno 1.16+ schema.

    Returns (passed, errors, parsed_doc). This is the Python fallback — the Go
    validator (cmd/validate-policy) is preferred and handles schema + CEL
    compilation.  The parsed doc is returned so callers can extract identity
    fields without re-reading the file.

    Note: expected-kind checking is handled earlier in evaluate() as a
    separate fail-fast step, so this only validates against the full set
    of known policy kinds.
    """
    errors: list[str] = []

    if not yaml:
        return False, ["PyYAML not installed. pip install pyyaml"], None

    try:
        raw = output_path.read_text(encoding="utf-8", errors="replace")
        doc = yaml.safe_load(raw)
    except Exception as exc:
        return False, [f"Invalid YAML: {exc}"], None

    if not doc or not isinstance(doc, dict):
        return False, ["Empty or non-dict YAML"], None

    kind = doc.get("kind") or ""
    api_version = doc.get("apiVersion") or ""

    if kind not in VALIDATING_POLICY_KINDS:
        errors.append(
            f"Expected kind in {sorted(VALIDATING_POLICY_KINDS)}, got {kind!r}"
        )
    if not api_version.startswith(POLICIES_APIVERSION_PREFIX):
        errors.append(
            f"Expected apiVersion starting with {POLICIES_APIVERSION_PREFIX!r}, got {api_version!r}"
        )

    return len(errors) == 0, errors, doc
