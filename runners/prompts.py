"""Standardized prompt templates per conversion track and task type.

Every benchmark run uses these prompts so results are comparable across tools.

Two categories:
  - **Conversion** prompts: parameterized by track + expected output kind.
  - **Generation** prompts: natural-language description → write a new policy.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Conversion prompts (source → target)
# ---------------------------------------------------------------------------

# Maps (track, output_kind) → template.  Falls back to (track, None) when the
# output kind is not explicitly listed, which matches the original behavior.

_CONVERSION_PROMPTS: dict[tuple[str, str | None], str] = {
    # ClusterPolicy → ValidatingPolicy
    ("cluster-policy", "ValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1alpha2, Kyverno 1.16+) "
        "using CEL-based validation where appropriate. Write the converted policy "
        "to {output_path}."
    ),
    # ClusterPolicy → MutatingPolicy
    ("cluster-policy", "MutatingPolicy"): (
        "Convert the Kyverno ClusterPolicy (mutate) in {input_path} to a Kyverno "
        "MutatingPolicy (apiVersion: policies.kyverno.io/v1alpha2, Kyverno 1.16+). "
        "Preserve the mutation logic using CEL expressions where appropriate. "
        "Write the converted policy to {output_path}."
    ),
    # ClusterPolicy → GeneratingPolicy
    ("cluster-policy", "GeneratingPolicy"): (
        "Convert the Kyverno ClusterPolicy (generate) in {input_path} to a Kyverno "
        "GeneratingPolicy (apiVersion: policies.kyverno.io/v1alpha2, Kyverno 1.16+). "
        "Preserve the resource generation logic. Write the converted policy "
        "to {output_path}."
    ),
    # ClusterPolicy → ImageValidatingPolicy
    ("cluster-policy", "ImageValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy (verifyImages) in {input_path} to a "
        "Kyverno ImageValidatingPolicy (apiVersion: policies.kyverno.io/v1alpha2, "
        "Kyverno 1.16+). Preserve the image verification rules. Write the "
        "converted policy to {output_path}."
    ),
    # Gatekeeper → ValidatingPolicy
    ("gatekeeper", None): (
        "Convert the Gatekeeper ConstraintTemplate and Constraint in "
        "{input_path} to a Kyverno ValidatingPolicy (apiVersion: "
        "policies.kyverno.io/v1alpha2, Kyverno 1.16+) using CEL-based validation "
        "where appropriate. Write the converted policy to {output_path}."
    ),
    # OPA → ValidatingPolicy
    ("opa", None): (
        "Convert the OPA/Rego policy in {input_path} to a Kyverno "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1alpha2, Kyverno 1.16+) "
        "using CEL-based validation where appropriate. Write the converted "
        "policy to {output_path}."
    ),
    # Sentinel → ValidatingPolicy
    ("sentinel", None): (
        "Convert the HashiCorp Sentinel policy in {input_path} to a Kyverno "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1alpha2, Kyverno 1.16+) "
        "using CEL-based validation where appropriate. Write the converted "
        "policy to {output_path}."
    ),
    # CleanupPolicy → DeletingPolicy
    ("cleanup", None): (
        "Convert the Kyverno CleanupPolicy in {input_path} to a Kyverno "
        "DeletingPolicy (apiVersion: policies.kyverno.io/v1alpha2, Kyverno 1.16+). "
        "Write the converted policy to {output_path}."
    ),
}

# Backward-compatible flat lookup used when output_kind is not specified.
PROMPTS: dict[str, str] = {
    "cluster-policy": _CONVERSION_PROMPTS[("cluster-policy", "ValidatingPolicy")],
    "gatekeeper": _CONVERSION_PROMPTS[("gatekeeper", None)],
    "opa": _CONVERSION_PROMPTS[("opa", None)],
    "sentinel": _CONVERSION_PROMPTS[("sentinel", None)],
    "cleanup": _CONVERSION_PROMPTS[("cleanup", None)],
}

# ---------------------------------------------------------------------------
# Generation prompts (no source policy — produce a new policy from NL)
# ---------------------------------------------------------------------------

_GENERATION_PROMPT = (
    "Write a Kyverno {output_kind} (apiVersion: policies.kyverno.io/v1alpha2) "
    "that {description} Use CEL expressions for validation where appropriate. "
    "Write the policy to {output_path}."
)


def build_prompt(
    track: str,
    input_path: str | None,
    output_path: str,
    *,
    output_kind: str | None = None,
    task_type: str = "convert",
    description: str | None = None,
) -> str:
    """Return a formatted prompt for the given track/task.

    For *convert* tasks, looks up the template by (track, output_kind).
    For *generate* tasks, uses the generation template with *description*.
    """
    if task_type == "generate":
        return _GENERATION_PROMPT.format(
            output_kind=output_kind or "ValidatingPolicy",
            description=description or "enforces the desired policy.",
            output_path=output_path,
        )

    # Conversion: try (track, output_kind) first, fall back to (track, None)
    template = _CONVERSION_PROMPTS.get((track, output_kind))
    if template is None:
        template = _CONVERSION_PROMPTS.get((track, None))
    if template is None:
        template = PROMPTS.get(track)
    if template is None:
        raise ValueError(f"Unknown track {track!r}. Known tracks: {sorted(PROMPTS)}")
    return template.format(input_path=input_path, output_path=output_path)
