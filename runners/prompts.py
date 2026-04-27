"""Standardized prompt templates per conversion track and task type.

Every benchmark run uses these prompts so results are comparable across tools.

Two categories:
  - **Conversion** prompts: parameterized by track + expected output kind.
  - **Generation** prompts: natural-language description → write a new policy.
"""

from __future__ import annotations

KYVERNO_VERSION = "1.17+"

# ---------------------------------------------------------------------------
# Conversion prompts (source → target)
# ---------------------------------------------------------------------------

_CONVERSION_PROMPTS: dict[tuple[str, str | None], str] = {
    ("cluster-policy", "ValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno {kyverno_version} "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cluster-policy", "MutatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno {kyverno_version} "
        "MutatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cluster-policy", "GeneratingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno {kyverno_version} "
        "GeneratingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cluster-policy", "ImageValidatingPolicy"): (
        "Convert the Kyverno ClusterPolicy in {input_path} to a Kyverno {kyverno_version} "
        "ImageValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("gatekeeper", None): (
        "Convert the Gatekeeper policy in {input_path} to a Kyverno {kyverno_version} "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("opa", None): (
        "Convert the OPA/Rego policy in {input_path} to a Kyverno {kyverno_version} "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("sentinel", None): (
        "Convert the HashiCorp Sentinel policy in {input_path} to a Kyverno {kyverno_version} "
        "ValidatingPolicy (apiVersion: policies.kyverno.io/v1)."
        "{description_clause} Write the converted policy to {output_path}."
    ),
    ("cleanup", None): (
        "Convert the Kyverno CleanupPolicy in {input_path} to a Kyverno {kyverno_version} "
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
    "Write a Kyverno {kyverno_version} {output_kind} (apiVersion: policies.kyverno.io/v1) "
    "that {description} Write the policy to {output_path}."
)


_DOCS_CLAUSE = (
    f"\n\nLook up Kyverno {KYVERNO_VERSION} documentation and examples before writing the policy:"
    "\n- https://kyverno.io/docs/"
    "\n- https://github.com/kyverno/kyverno-policies"
)


def build_prompt(
    track: str,
    input_path: str | None,
    output_path: str,
    *,
    output_kind: str | None = None,
    task_type: str = "convert",
    description: str | None = None,
    include_docs: bool = False,
) -> str:
    """Return a formatted prompt for the given track/task.

    For *convert* tasks, looks up the template by (track, output_kind).
    For *generate* tasks, uses the generation template with *description*.

    When *include_docs* is True, appends a clause pointing the tool at the
    canonical Kyverno docs and the community policy repo.
    """
    if task_type == "generate":
        prompt = _GENERATION_PROMPT.format(
            kyverno_version=KYVERNO_VERSION,
            output_kind=output_kind or "ValidatingPolicy",
            description=description or "enforces the desired policy.",
            output_path=output_path,
        )
        if include_docs:
            prompt += _DOCS_CLAUSE
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
        kyverno_version=KYVERNO_VERSION,
        input_path=input_path,
        output_path=output_path,
        description_clause=description_clause,
    )

    if include_docs:
        prompt += _DOCS_CLAUSE

    return prompt
