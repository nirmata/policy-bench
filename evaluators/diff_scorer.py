"""Structural similarity scoring between source and converted policies.

Produces a 0.0–1.0 diff_score measuring how well the converted policy
preserves the source's intent (target kinds, rule count, field coverage).
This is NOT a literal YAML diff — it measures semantic preservation.
"""

from __future__ import annotations

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _extract_kinds(doc: dict) -> set[str]:
    """Extract target resource kinds from any Kyverno-style policy."""
    kinds: set[str] = set()
    for rule in (doc.get("spec") or {}).get("rules") or []:
        match = rule.get("match") or {}
        for block in match.get("any") or match.get("all") or []:
            for k in (block.get("resources") or {}).get("kinds") or []:
                kk = k.strip().lower()
                if kk:
                    kinds.add(kk)
    return kinds


def _extract_rule_names(doc: dict) -> list[str]:
    return [
        r.get("name", f"rule-{i}")
        for i, r in enumerate((doc.get("spec") or {}).get("rules") or [])
    ]


def _extract_messages(doc: dict) -> set[str]:
    messages: set[str] = set()
    for rule in (doc.get("spec") or {}).get("rules") or []:
        val = rule.get("validate") or {}
        msg = val.get("message") or ""
        if msg:
            messages.add(msg.strip().lower())
        # 1.16+ structure
        for assertion in (doc.get("spec") or {}).get("assertions") or []:
            m = assertion.get("message") or ""
            if m:
                messages.add(m.strip().lower())
    return messages


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score(input_doc: dict | None, output_doc: dict | None) -> float:
    """Return a 0.0–1.0 structural similarity score.

    Components (equally weighted):
      - kinds overlap (Jaccard)
      - rule count ratio (min/max)
      - message overlap (Jaccard)
    """
    if not input_doc or not output_doc:
        return 0.0

    in_kinds = _extract_kinds(input_doc)
    out_kinds = _extract_kinds(output_doc)
    kinds_score = _jaccard(in_kinds, out_kinds)

    in_rules = _extract_rule_names(input_doc)
    out_rules = _extract_rule_names(output_doc)
    if in_rules and out_rules:
        rule_count_score = min(len(in_rules), len(out_rules)) / max(
            len(in_rules), len(out_rules)
        )
    elif not in_rules and not out_rules:
        rule_count_score = 1.0
    else:
        rule_count_score = 0.0

    in_msgs = _extract_messages(input_doc)
    out_msgs = _extract_messages(output_doc)
    msg_score = _jaccard(in_msgs, out_msgs)

    return round((kinds_score + rule_count_score + msg_score) / 3.0, 4)


def score_from_files(input_path: str, output_path: str) -> float:
    """Convenience: load two YAML files and score them."""
    if not yaml:
        return 0.0
    try:
        from pathlib import Path

        in_docs = list(yaml.safe_load_all(Path(input_path).read_text(encoding="utf-8")))
        input_doc = in_docs[0] if in_docs else None
        output_doc = yaml.safe_load(
            Path(output_path).read_text(encoding="utf-8")
        )
    except Exception:
        return 0.0
    return score(input_doc, output_doc)
