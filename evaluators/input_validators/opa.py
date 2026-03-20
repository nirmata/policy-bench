"""Input validator for OPA / Rego policy files."""

from __future__ import annotations

import re
from pathlib import Path


def validate(path: Path, **_kwargs) -> tuple[bool, list[str]]:
    """Validate an OPA Rego file. Returns (passed, errors)."""
    errors: list[str] = []

    if not path.exists():
        return False, [f"File not found: {path}"]

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, [f"Cannot read file: {exc}"]

    if not text.strip():
        return False, ["Empty Rego file"]

    if not re.search(r"^\s*package\s+", text, re.MULTILINE):
        errors.append("Missing 'package' declaration")

    has_deny = bool(re.search(r"\bdeny\b", text))
    has_violation = bool(re.search(r"\bviolation\b", text))
    has_allow = bool(re.search(r"\ballow\b", text))
    if not (has_deny or has_violation or has_allow):
        errors.append(
            "No deny/violation/allow rule found — expected at least one "
            "policy decision point"
        )

    return len(errors) == 0, errors
