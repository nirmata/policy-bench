"""Base classes and data structures for benchmark tool runners.

Every runner is a *wrapper harness* around the tool being benchmarked.
The contract is the same for every tool:

  1. Send prompt to the tool
  2. Capture the raw output (file written or text returned)
  3. Measure wall-clock time externally
  4. Estimate tokens and cost (real if available, heuristic otherwise)
  5. Return a RunResult so benchmark.py can run eval + emit JSON
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


# -----------------------------------------------------------------------
# Token / cost estimation utilities (shared across all runners)
# -----------------------------------------------------------------------

# Rough chars-per-token ratio for modern LLMs (GPT-4, Claude, etc.)
_CHARS_PER_TOKEN = 3.8


def estimate_tokens(text: str) -> int:
    """Approximate token count from raw text.

    Uses ~3.8 chars/token which is a reasonable average for English +
    YAML across GPT-4 / Claude tokenizers.
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate_per_m: float = 3.0,
    output_rate_per_m: float = 15.0,
) -> float:
    """Compute cost in USD from token counts and per-million-token rates."""
    return (input_tokens * input_rate_per_m + output_tokens * output_rate_per_m) / 1_000_000


def extract_yaml_block(text: str) -> str | None:
    """Pull the first fenced YAML block from LLM output."""
    m = re.search(r"```ya?ml\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    if text.strip().startswith("apiVersion"):
        return text.strip()
    return None


# -----------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------

@dataclass
class RunResult:
    """Result of running a single tool on a single policy."""

    output_path: Path
    conversion_time_seconds: float
    success: bool
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    tool_version: str | None = None
    raw_log: str | None = None
    tokens_estimated: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is not None and self.output_tokens is not None:
            return self.input_tokens + self.output_tokens
        return None


# -----------------------------------------------------------------------
# Abstract runner
# -----------------------------------------------------------------------

class ToolRunner(ABC):
    """Abstract interface every tool runner must implement.

    Each runner wraps a tool (nctl, claude, cursor) as the *subject* of
    the benchmark.  The runner is the harness — it sends the prompt,
    captures output, measures time, and estimates tokens/cost.
    """

    name: str = "unknown"

    @abstractmethod
    def run(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        *,
        timeout_seconds: int = 120,
        config: dict | None = None,
    ) -> RunResult:
        """Execute the conversion and return a RunResult."""
        ...

    def is_available(self) -> bool:
        """Return True if the tool is installed / reachable."""
        return True
