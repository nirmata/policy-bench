"""Intent validation — checks the converted policy preserves the source policy's intent.

Dispatches to track-specific logic.  Supports all Kyverno 1.16+ output kinds:
ValidatingPolicy, MutatingPolicy, GeneratingPolicy, ImageValidatingPolicy,
DeletingPolicy.
"""

from __future__ import annotations

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers for extracting kinds and actions from different policy formats
# ---------------------------------------------------------------------------

def _kinds_from_cluster_policy(doc: dict) -> set[str]:
    kinds: set[str] = set()
    for rule in (doc.get("spec") or {}).get("rules") or []:
        for block in (
            (rule.get("match") or {}).get("any")
            or (rule.get("match") or {}).get("all")
            or []
        ):
            for k in (block.get("resources") or {}).get("kinds") or []:
                kk = (k or "").strip().lower()
                if kk:
                    kinds.add(kk + "s" if not kk.endswith("s") else kk)
    return kinds


def _validation_action_from_cluster_policy(doc: dict) -> str:
    return (doc.get("spec") or {}).get("validationFailureAction") or ""


def _kinds_from_output_policy(doc: dict) -> set[str]:
    """Extract target resource kinds from any Kyverno 1.16+ output policy.

    Kyverno 1.16+ uses spec.matchConstraints.resourceRules instead of
    spec.rules[].match, but also falls back to spec.rules for compat.
    """
    kinds: set[str] = set()
    spec = doc.get("spec") or {}

    # Kyverno 1.16+: spec.matchConstraints.resourceRules
    for rr in (spec.get("matchConstraints") or {}).get("resourceRules") or []:
        for k in rr.get("resources") or []:
            kk = (k or "").strip().lower()
            if kk:
                kinds.add(kk + "s" if not kk.endswith("s") else kk)

    # Legacy / fallback: spec.rules[].match
    if not kinds:
        for rule in spec.get("rules") or []:
            for block in (
                (rule.get("match") or {}).get("any")
                or (rule.get("match") or {}).get("all")
                or []
            ):
                for k in (block.get("resources") or {}).get("kinds") or []:
                    kk = (k or "").strip().lower()
                    if kk:
                        kinds.add(kk + "s" if not kk.endswith("s") else kk)

    return kinds


def _validation_actions_from_validating_policy(doc: dict) -> list[str]:
    return list((doc.get("spec") or {}).get("validationActions") or [])


def _has_rules(doc: dict) -> bool:
    """Check if output policy has rules/validations/mutations/generate content.

    Kyverno 1.16+ uses kind-specific field names instead of spec.rules:
      - ValidatingPolicy: spec.validations
      - MutatingPolicy: spec.mutations
      - GeneratingPolicy: spec.rules or spec.generate
      - ImageValidatingPolicy: spec.validations or spec.imageRules
    """
    spec = doc.get("spec") or {}
    return bool(
        spec.get("rules")
        or spec.get("validations")
        or spec.get("mutations")
        or spec.get("generate")
        or spec.get("imageRules")
        or spec.get("attestors")
    )


# ---------------------------------------------------------------------------
# Track-specific intent validators
# ---------------------------------------------------------------------------

def _validate_intent_cpol_validate(
    input_doc: dict, output_doc: dict
) -> tuple[bool, list[str]]:
    """ClusterPolicy (validate) -> ValidatingPolicy."""
    errors: list[str] = []
    in_kinds = _kinds_from_cluster_policy(input_doc)
    out_kinds = _kinds_from_output_policy(output_doc)
    if in_kinds and out_kinds and in_kinds != out_kinds:
        errors.append(
            f"Match kinds mismatch: source {sorted(in_kinds)}, output {sorted(out_kinds)}"
        )
    in_action = _validation_action_from_cluster_policy(input_doc)
    out_actions = _validation_actions_from_validating_policy(output_doc)
    if (
        in_action == "Enforce"
        and out_actions
        and "Deny" not in out_actions
        and "Enforce" not in out_actions
    ):
        errors.append(
            f"Validation action mismatch: source was Enforce, output has {out_actions} (expected Deny)"
        )
    if in_action == "Audit" and out_actions and "Audit" not in out_actions:
        errors.append(
            f"Validation action mismatch: source was Audit, output has {out_actions}"
        )
    return len(errors) == 0, errors


def _validate_intent_cpol_mutate(
    input_doc: dict, output_doc: dict
) -> tuple[bool, list[str]]:
    """ClusterPolicy (mutate) -> MutatingPolicy."""
    errors: list[str] = []
    in_kinds = _kinds_from_cluster_policy(input_doc)
    out_kinds = _kinds_from_output_policy(output_doc)
    if in_kinds and out_kinds and in_kinds != out_kinds:
        errors.append(
            f"Match kinds mismatch: source {sorted(in_kinds)}, output {sorted(out_kinds)}"
        )
    out_kind = (output_doc.get("kind") or "")
    if out_kind and out_kind != "MutatingPolicy":
        errors.append(f"Expected MutatingPolicy, got {out_kind!r}")
    if not _has_rules(output_doc):
        errors.append("MutatingPolicy has no rules (source had mutate rules)")
    return len(errors) == 0, errors


def _validate_intent_cpol_generate(
    input_doc: dict, output_doc: dict
) -> tuple[bool, list[str]]:
    """ClusterPolicy (generate) -> GeneratingPolicy."""
    errors: list[str] = []
    in_kinds = _kinds_from_cluster_policy(input_doc)
    out_kinds = _kinds_from_output_policy(output_doc)
    if in_kinds and out_kinds and in_kinds != out_kinds:
        errors.append(
            f"Match kinds mismatch: source {sorted(in_kinds)}, output {sorted(out_kinds)}"
        )
    out_kind = (output_doc.get("kind") or "")
    if out_kind and out_kind != "GeneratingPolicy":
        errors.append(f"Expected GeneratingPolicy, got {out_kind!r}")
    if not _has_rules(output_doc):
        errors.append("GeneratingPolicy has no rules (source had generate rules)")
    return len(errors) == 0, errors


def _validate_intent_cpol_image_verify(
    input_doc: dict, output_doc: dict
) -> tuple[bool, list[str]]:
    """ClusterPolicy (verifyImages) -> ImageValidatingPolicy."""
    errors: list[str] = []
    out_kind = (output_doc.get("kind") or "")
    if out_kind and out_kind != "ImageValidatingPolicy":
        errors.append(f"Expected ImageValidatingPolicy, got {out_kind!r}")
    if not _has_rules(output_doc):
        errors.append("ImageValidatingPolicy has no rules (source had verifyImages rules)")
    return len(errors) == 0, errors


def _validate_intent_cluster_policy(
    input_doc: dict, output_doc: dict
) -> tuple[bool, list[str]]:
    """Dispatch ClusterPolicy intent check based on output kind."""
    out_kind = (output_doc.get("kind") or "")

    if out_kind == "MutatingPolicy":
        return _validate_intent_cpol_mutate(input_doc, output_doc)
    if out_kind == "GeneratingPolicy":
        return _validate_intent_cpol_generate(input_doc, output_doc)
    if out_kind == "ImageValidatingPolicy":
        return _validate_intent_cpol_image_verify(input_doc, output_doc)

    return _validate_intent_cpol_validate(input_doc, output_doc)


def _validate_intent_gatekeeper(
    input_docs: list[dict], output_doc: dict
) -> tuple[bool, list[str]]:
    """Check Gatekeeper -> Kyverno intent: target kinds and enforcement."""
    errors: list[str] = []

    constraint = None
    for d in input_docs:
        if (d.get("kind") or "").endswith("Labels") or d.get("kind") not in (
            "ConstraintTemplate",
            None,
        ):
            constraint = d

    if constraint:
        gk_kinds: set[str] = set()
        for match_entry in (constraint.get("spec") or {}).get("match", {}).get(
            "kinds", []
        ):
            for k in match_entry.get("kinds", []):
                kk = k.strip().lower()
                if kk:
                    gk_kinds.add(kk + "s" if not kk.endswith("s") else kk)

        out_kinds = _kinds_from_output_policy(output_doc)
        if gk_kinds and out_kinds and gk_kinds != out_kinds:
            errors.append(
                f"Match kinds mismatch: Gatekeeper constraint targets {sorted(gk_kinds)}, "
                f"output targets {sorted(out_kinds)}"
            )

    return len(errors) == 0, errors


def _validate_intent_opa(
    _input_text: str, output_doc: dict
) -> tuple[bool, list[str]]:
    """Basic OPA -> Kyverno intent check: output must have rules/validations."""
    errors: list[str] = []
    if not _has_rules(output_doc):
        errors.append("Converted policy has no rules (OPA source should produce at least one)")
    return len(errors) == 0, errors


def _validate_intent_sentinel(
    _input_text: str, output_doc: dict
) -> tuple[bool, list[str]]:
    """Basic Sentinel -> Kyverno intent check."""
    errors: list[str] = []
    if not _has_rules(output_doc):
        errors.append(
            "Converted policy has no rules (Sentinel source should produce at least one)"
        )
    return len(errors) == 0, errors


def _validate_intent_cleanup(
    input_doc: dict, output_doc: dict
) -> tuple[bool, list[str]]:
    """Check CleanupPolicy -> DeletingPolicy intent: target kinds preserved."""
    errors: list[str] = []

    in_kinds: set[str] = set()
    for block in (
        (input_doc.get("spec") or {}).get("match", {}).get("any")
        or (input_doc.get("spec") or {}).get("match", {}).get("all")
        or []
    ):
        for k in (block.get("resources") or {}).get("kinds") or []:
            kk = k.strip().lower()
            if kk:
                in_kinds.add(kk + "s" if not kk.endswith("s") else kk)

    out_kinds = _kinds_from_output_policy(output_doc)
    if in_kinds and out_kinds and in_kinds != out_kinds:
        errors.append(
            f"Match kinds mismatch: cleanup targets {sorted(in_kinds)}, "
            f"output targets {sorted(out_kinds)}"
        )
    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def validate_intent(
    track: str,
    input_path: str | None,
    output_doc: dict,
    *,
    input_docs: list[dict] | None = None,
    input_text: str | None = None,
) -> tuple[bool, list[str]]:
    """Dispatch to the correct intent validator for the given track.

    For YAML-based tracks, pass input_docs (parsed YAML documents).
    For Rego / Sentinel, pass input_text (raw file content).
    """
    if track == "cluster-policy":
        if not input_docs:
            return True, []
        return _validate_intent_cluster_policy(input_docs[0], output_doc)

    if track == "gatekeeper":
        if not input_docs:
            return True, []
        return _validate_intent_gatekeeper(input_docs, output_doc)

    if track == "opa":
        return _validate_intent_opa(input_text or "", output_doc)

    if track == "sentinel":
        return _validate_intent_sentinel(input_text or "", output_doc)

    if track == "cleanup":
        if not input_docs:
            return True, []
        return _validate_intent_cleanup(input_docs[0], output_doc)

    return True, [f"No intent validator for track {track!r}"]
