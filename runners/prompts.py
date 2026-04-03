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

_CONVERSION_PROMPTS: dict[tuple[str, str | None], str] = {
    ("cluster-policy", "ValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a "
        "ValidatingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("cluster-policy", "MutatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a "
        "MutatingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("cluster-policy", "GeneratingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a "
        "GeneratingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("cluster-policy", "ImageValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to an "
        "ImageValidatingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("gatekeeper", None): (
        "Convert the Gatekeeper policy in {input_path} to a Kyverno "
        "ValidatingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("opa", None): (
        "Convert the OPA/Rego policy in {input_path} to a Kyverno "
        "ValidatingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("sentinel", None): (
        "Convert the HashiCorp Sentinel policy in {input_path} to a Kyverno "
        "ValidatingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
    ),
    ("cleanup", None): (
        "Convert the Kyverno CleanupPolicy in {input_path} to a "
        "DeletingPolicy.{description_clause} Write the converted policy "
        "to {output_path}."
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
    "Write a Kyverno {output_kind} that {description} "
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

    description_clause = ""
    if description:
        description_clause = f" The policy {description}."

    return template.format(
        input_path=input_path,
        output_path=output_path,
        description_clause=description_clause,
    )
