"""Summarise benchmark errors into a human-friendly message using Claude."""

from __future__ import annotations


def summarise_errors(
    *,
    tool_name: str,
    policy_id: str,
    expected_kind: str,
    errors: list[str],
    model: str = "claude-haiku-4-5-20251001",
) -> str | None:
    """Call Claude to produce a short, plain-English summary of the errors.

    Uses the same ``ANTHROPIC_API_KEY`` that the Claude runner uses for
    benchmarks — no additional configuration needed.

    Returns ``None`` silently when the Anthropic SDK is unavailable or the
    API call fails — error summarisation is best-effort and must never block
    the benchmark run.
    """
    try:
        import anthropic
    except ImportError:
        return None

    error_block = "\n".join(f"- {e}" for e in errors)
    prompt = (
        f"You are a Kubernetes / Kyverno policy expert reviewing benchmark results.\n\n"
        f"Tool: {tool_name}\n"
        f"Policy: {policy_id}\n"
        f"Expected output kind: {expected_kind}\n\n"
        f"The following errors were reported:\n{error_block}\n\n"
        f"Write a short (2-3 sentence) plain-English summary explaining what went wrong "
        f"and why. Avoid jargon where possible — the reader may not be a Kyverno expert. "
        f"Do NOT repeat the raw errors verbatim; instead explain the root cause. "
        f"Do NOT suggest fixes."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        return (response.content[0].text or "").strip() if response.content else None
    except Exception:
        return None
