"""Input validator for HashiCorp Sentinel policy files."""

from __future__ import annotations

import re
from pathlib import Path


def validate(path: Path, **_kwargs) -> tuple[bool, list[str]]:
    """Validate a Sentinel policy file. Returns (passed, errors)."""
    errors: list[str] = []

    if not path.exists():
        return False, [f"File not found: {path}"]

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, [f"Cannot read file: {exc}"]

    if not text.strip():
        return False, ["Empty Sentinel file"]

    if not re.search(r"\bmain\s*=\s*rule\b", text):
        errors.append("Missing 'main = rule' — Sentinel policies require a main rule")

    return len(errors) == 0, errors
