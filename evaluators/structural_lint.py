"""Structural lint for converted policies.

Catches common conversion issues that pass CEL compilation but fail
semantic tests — missing matchConditions, wrong container ordering, etc.
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

_CONTAINER_APPEND_RE = re.compile(
    r"object\.spec(?:\.template)?\.spec\.(?:containers|initContainers|volumes)\s*\+\s*\["
)


def lint(output_path: Path) -> tuple[bool, list[str]]:
    """Run structural lint checks on a converted policy.

    Returns (passed, warnings).  Warnings are advisory — they indicate
    likely semantic test failures, not schema errors.
    """
    if yaml is None:
        return True, []

    try:
        doc = yaml.safe_load(output_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return True, []  # schema validator handles parse failures

    kind = doc.get("kind", "")
    spec = doc.get("spec") or {}

    warnings: list[str] = []

    if kind in ("MutatingPolicy", "NamespacedMutatingPolicy"):
        _lint_mutating_policy(spec, warnings)

    return len(warnings) == 0, warnings


def _lint_mutating_policy(spec: dict, warnings: list[str]) -> None:
    """Check MutatingPolicy-specific structural issues."""
    exprs = _collect_mutation_expressions(spec)
    if not exprs:
        return

    combined = "\n".join(exprs)
    has_match_conds = bool(spec.get("matchConditions"))

    # Append instead of prepend for injected containers.
    if _CONTAINER_APPEND_RE.search(combined):
        warnings.append(
            "Appends injected containers (existing + [new]) instead of prepending "
            "([new] + existing). patchStrategicMerge places new items first."
        )

    if not has_match_conds and "containers" in combined:
        # .filter() on containers without matchConditions.
        if ".filter(" in combined:
            warnings.append(
                "Uses .filter() on containers but has no matchConditions. "
                "Empty filter results still fire the mutation (pass instead of skip)."
            )
        # Add-if-absent pattern: .map() + .orValue() without .filter() means
        # the mutation always applies even when all values already exist.
        elif ".map(" in combined and ".orValue(" in combined:
            warnings.append(
                "Uses add-if-absent pattern (.orValue defaults) without matchConditions. "
                "Mutation fires even when all containers already have the values (pass instead of skip)."
            )


def _collect_mutation_expressions(spec: dict) -> list[str]:
    """Extract all CEL expressions from spec.mutations[]."""
    exprs = []
    for m in spec.get("mutations") or []:
        if not isinstance(m, dict):
            continue
        ac = m.get("applyConfiguration") or {}
        expr = ac.get("expression", "")
        if expr:
            exprs.append(expr)
        jp = m.get("jsonPatch") or {}
        expr = jp.get("expression", "")
        if expr:
            exprs.append(expr)
    return exprs
