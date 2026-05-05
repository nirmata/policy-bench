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

import json as _json
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
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


def dir_output_artifact(output_path: Path, task_type: str | None = None) -> Path | None:
    """Return the canonical artifact for directory-output tasks, or None.

    Directory-output tasks pre-create ``output_path`` as a directory and the AI
    writes task-specific files there. Checking ``output_path.exists()`` is
    always True; callers need a concrete artifact path to determine success.

    Known artifacts:
      - generate_test: ``kyverno-test.yaml``
      - generate_chainsaw_test: ``chainsaw-test.yaml``

    Returns ``None`` for single-file tasks so callers can use:
    ``output_check = dir_output_artifact(path, task_type) or path``.
    """
    if not output_path.is_dir():
        return None
    if task_type == "generate_chainsaw_test":
        return output_path / "chainsaw-test.yaml"
    return output_path / "kyverno-test.yaml"


# -----------------------------------------------------------------------
# Centralized model pricing (USD per 1M tokens: input, output)
# -----------------------------------------------------------------------

MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Claude models
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-opus-20240229": (15.0, 75.0),
    "claude-3-haiku-20240307": (0.25, 1.25),
    # Cursor routes to Claude Sonnet by default
    "cursor-agent-cli": (3.0, 15.0),
    "cursor-agent-manual": (3.0, 15.0),
}

# Fallback rates when model is not in the pricing table
DEFAULT_INPUT_RATE = 3.0
DEFAULT_OUTPUT_RATE = 15.0


def model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Compute cost for a known model, falling back to default rates."""
    inp_rate, out_rate = MODEL_PRICING.get(
        model, (DEFAULT_INPUT_RATE, DEFAULT_OUTPUT_RATE)
    )
    return estimate_cost(input_tokens, output_tokens, inp_rate, out_rate)


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
# Shared CLI subprocess helper
# -----------------------------------------------------------------------

def run_cli_subprocess(
    *,
    cmd: list[str],
    full_prompt: str,
    output_path: Path,
    timeout: int,
    default_model: str,
    tool_version: str | None,
    raw_log_builder: Callable[[str, str], str | None] | None = None,
    output_check_path: Path | None = None,
) -> RunResult:
    """Run a CLI tool as a subprocess and build a RunResult.

    This captures the common flow shared by Claude Code CLI and Cursor CLI:
      - subprocess.run with timeout handling
      - JSON envelope parsing for real token counts
      - YAML fallback extraction when the agent doesn't write the file
      - Token estimation when real counts aren't available
      - Cost calculation and RunResult construction

    Parameters
    ----------
    cmd : list[str]
        The full command to execute.
    full_prompt : str
        The prompt sent to the tool (used for token estimation fallback).
    output_path : Path
        Where the converted policy (or test-suite directory) should be written.
    timeout : int
        Subprocess timeout in seconds.
    default_model : str
        Model name to use if the JSON envelope doesn't include one.
    tool_version : str | None
        Version string for the tool (e.g. "claude 1.0.3").
    raw_log_builder : callable | None
        Optional function ``(stdout: str, stderr: str) -> str`` to build the
        raw_log field.  Defaults to ``stdout[:5000]``.
    output_check_path : Path | None
        For directory-output tasks (generate_test), the harness pre-creates
        ``output_path`` as a directory, so ``output_path.exists()`` is always
        True.  Pass the canonical artifact (e.g. ``output_path /
        "kyverno-test.yaml"``) here so the success check and token estimation
        target the right file.  Defaults to ``output_path`` (file-output mode).
    """
    repo_root = Path(__file__).resolve().parent.parent

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(repo_root),
        )
        elapsed = time.monotonic() - start
    except subprocess.TimeoutExpired:
        return RunResult(
            output_path=output_path,
            conversion_time_seconds=time.monotonic() - start,
            success=False,
            error=f"{cmd[0]} CLI timed out after {timeout}s",
            model=default_model,
            tool_version=tool_version,
        )

    # -- Parse JSON envelope for real token usage --------------------------
    raw_text = proc.stdout or ""
    model_name = default_model
    real_input_tokens = None
    real_output_tokens = None
    real_cost = None

    try:
        data = _json.loads(raw_text)
        raw_text = data.get("result", raw_text)
        model_name = data.get("model", model_name)
        usage = data.get("usage", {})
        real_input_tokens = usage.get("input_tokens")
        real_output_tokens = usage.get("output_tokens")
        if real_input_tokens and real_output_tokens:
            real_cost = round(model_cost(model_name, real_input_tokens, real_output_tokens), 6)
    except (_json.JSONDecodeError, TypeError):
        pass

    artifact = output_check_path if output_check_path is not None else output_path

    # -- Determine success / YAML fallback ---------------------------------
    success = proc.returncode == 0 and artifact.exists()

    if proc.returncode == 0 and not artifact.exists() and output_check_path is None:
        # YAML extraction only applies to single-file (convert/generate) output.
        yaml_text = extract_yaml_block(raw_text)
        if yaml_text:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(yaml_text + "\n", encoding="utf-8")
            success = True

    # -- Token estimation fallback -----------------------------------------
    tokens_estimated = real_input_tokens is None
    input_tokens = real_input_tokens if real_input_tokens is not None else estimate_tokens(full_prompt)

    if real_output_tokens is not None:
        output_tokens = real_output_tokens
    else:
        out_text = ""
        if artifact.is_file():
            try:
                out_text = artifact.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"  Warning: could not read output file: {exc}", file=sys.stderr)
        output_tokens = estimate_tokens(out_text)
        tokens_estimated = True

    # -- Cost calculation --------------------------------------------------
    cost = real_cost if real_cost is not None else round(
        model_cost(model_name, input_tokens, output_tokens), 6
    )

    # -- Build raw_log -----------------------------------------------------
    if raw_log_builder is not None:
        raw_log = raw_log_builder(proc.stdout or "", proc.stderr or "")
    else:
        raw_log = raw_text[:5000] if raw_text else None

    return RunResult(
        output_path=output_path,
        conversion_time_seconds=round(elapsed, 3),
        success=success,
        error=None if success else (
            f"{cmd[0]} CLI exited {proc.returncode}"
            if proc.returncode != 0
            else f"{cmd[0]} exited 0 but did not write output to {artifact}"
        ),
        model=model_name,
        tool_version=tool_version,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        tokens_estimated=tokens_estimated,
        raw_log=raw_log,
    )


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
