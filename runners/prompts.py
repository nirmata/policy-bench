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
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno 1.16+ "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cluster-policy", "MutatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno 1.16+ "
        "MutatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cluster-policy", "GeneratingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno 1.16+ "
        "GeneratingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cluster-policy", "ImageValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno 1.16+ "
        "ImageValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("gatekeeper", None): (
        "Convert the Gatekeeper policy in {input_path} to a Kyverno 1.16+ "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("opa", None): (
        "Convert the OPA/Rego policy in {input_path} to a Kyverno 1.16+ "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("sentinel", None): (
        "Convert the HashiCorp Sentinel policy in {input_path} to a Kyverno 1.16+ "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cleanup", None): (
        "Convert the Kyverno CleanupPolicy in {input_path} to a Kyverno 1.16+ "
        "DeletingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
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
    "Write a Kyverno 1.16+ {output_kind} (apiVersion: policies.kyverno.io/v1) "
    "that {description} Write the policy to {output_path}."
)


_REFERENCE_CLAUSE = (
    "\n\nReference conversion examples are in {reference_dir} — "
    "each subdirectory has a before.yaml (old ClusterPolicy) and after.yaml "
    "(converted policy). Study these examples before converting."
    "\n\nFor additional examples and documentation on the new Kyverno 1.16+ policy types, "
    "refer to:"
    "\n- Migration guide: https://kyverno.io/docs/guides/migration-to-cel/"
    "\n- Policy type docs: https://kyverno.io/docs/policy-types/"
    "\n- Community policy examples: https://github.com/kyverno/kyverno-policies "
    "(look at *-vpol/, *-mpol/, *-gpol/ directories for converted examples)"
)


def build_prompt(
    track: str,
    input_path: str | None,
    output_path: str,
    *,
    output_kind: str | None = None,
    task_type: str = "convert",
    description: str | None = None,
    reference_dir: str | None = None,
) -> str:
    """Return a formatted prompt for the given track/task.

    For *convert* tasks, looks up the template by (track, output_kind).
    For *generate* tasks, uses the generation template with *description*.

    When *reference_dir* is provided, appends a clause pointing the tool
    at before/after conversion examples.
    """
    if task_type == "generate":
        prompt = _GENERATION_PROMPT.format(
            output_kind=output_kind or "ValidatingPolicy",
            description=description or "enforces the desired policy.",
            output_path=output_path,
        )
        if reference_dir:
            prompt += _REFERENCE_CLAUSE.format(reference_dir=reference_dir)
        return prompt

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

    prompt = template.format(
        input_path=input_path,
        output_path=output_path,
        description_clause=description_clause,
    )

    if reference_dir:
        prompt += _REFERENCE_CLAUSE.format(reference_dir=reference_dir)

    return prompt
