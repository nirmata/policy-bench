"""Runner harness for Claude Code conversion.

Claude is the subject of the benchmark.  This harness:
  1. Sends the prompt via  claude -p "..." --output-format json  (CLI first)
     or via the Anthropic Messages API (fallback)
  2. Captures the raw YAML output (file written by agent, or extracted from response)
  3. Measures wall-clock time externally
  4. Extracts real tokens from the API/CLI JSON, or estimates from text sizes
  5. Computes cost from token counts + model pricing
  6. Returns a RunResult for eval + JSON emission
"""

from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .base import (
    RunResult,
    ToolRunner,
    estimate_cost,
    estimate_tokens,
    extract_yaml_block,
)

# Model pricing (USD per 1 M tokens) — update when pricing changes
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-20250514": (3.0, 15.0),
    "claude-3-5-sonnet-20241022": (3.0, 15.0),
    "claude-3-opus-20240229": (15.0, 75.0),
    "claude-3-haiku-20240307": (0.25, 1.25),
}


def _model_cost(model: str, in_tok: int, out_tok: int) -> float:
    inp_rate, out_rate = _PRICING.get(model, (3.0, 15.0))
    return estimate_cost(in_tok, out_tok, inp_rate, out_rate)


def _get_claude_version() -> str | None:
    try:
        proc = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


class ClaudeRunner(ToolRunner):
    name = "claude"

    def is_available(self) -> bool:
        return bool(shutil.which("claude") or os.environ.get("ANTHROPIC_API_KEY"))

    # ------------------------------------------------------------------
    # Primary: Claude Code CLI
    # ------------------------------------------------------------------
    def _run_via_cli(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        timeout_seconds: int,
    ) -> RunResult:
        repo_root = Path(__file__).resolve().parent.parent
        version = _get_claude_version()

        full_prompt = (
            f"{prompt}\n\n"
            f"The source policy file is at: {input_path}\n"
            f"Write the converted policy to: {output_path}"
        )

        cmd = [
            "claude",
            "-p", full_prompt,
            "--output-format", "json",
            "--allowedTools", "Read,Write,Bash",
            "--model", "sonnet",
        ]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True,
                timeout=timeout_seconds,
                cwd=str(repo_root),
            )
            elapsed = time.monotonic() - start
        except subprocess.TimeoutExpired:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=f"claude CLI timed out after {timeout_seconds}s",
                model="claude-code-cli",
                tool_version=version,
            )

        # Parse JSON output for real token usage
        input_tokens = None
        output_tokens = None
        cost = None
        tokens_estimated = False
        model_name = "claude-code-cli"
        raw_text = proc.stdout or ""

        try:
            data = _json.loads(raw_text)
            raw_text = data.get("result", raw_text)
            model_name = data.get("model", model_name)
            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            if input_tokens and output_tokens:
                cost = round(_model_cost(model_name, input_tokens, output_tokens), 6)
        except (_json.JSONDecodeError, TypeError):
            pass

        success = proc.returncode == 0 and output_path.exists()

        # If agent didn't write the file, try extracting YAML from response
        if proc.returncode == 0 and not output_path.exists():
            yaml_text = extract_yaml_block(raw_text)
            if yaml_text:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(yaml_text + "\n", encoding="utf-8")
                success = True

        # Fallback: estimate tokens from prompt + output file sizes
        if input_tokens is None:
            input_tokens = estimate_tokens(full_prompt)
            tokens_estimated = True
        if output_tokens is None:
            out_text = ""
            if output_path.exists():
                try:
                    out_text = output_path.read_text(encoding="utf-8")
                except Exception:
                    pass
            output_tokens = estimate_tokens(out_text)
            tokens_estimated = True
        if cost is None:
            cost = round(_model_cost(model_name, input_tokens, output_tokens), 6)

        return RunResult(
            output_path=output_path,
            conversion_time_seconds=round(elapsed, 3),
            success=success,
            error=None if success else f"claude CLI exited {proc.returncode}",
            model=model_name,
            tool_version=version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            tokens_estimated=tokens_estimated,
            raw_log=raw_text[:5000] if raw_text else None,
        )

    # ------------------------------------------------------------------
    # Fallback: Anthropic Messages API (real token counts)
    # ------------------------------------------------------------------
    def _run_via_api(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        model: str,
        timeout_seconds: int,
    ) -> RunResult:
        try:
            import anthropic
        except ImportError:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=0,
                success=False,
                error="anthropic package not installed (pip install anthropic)",
            )

        policy_content = input_path.read_text(encoding="utf-8")
        full_prompt = (
            f"{prompt}\n\n"
            f"Here is the source policy:\n\n```yaml\n{policy_content}\n```\n\n"
            "Return ONLY the converted policy YAML inside a ```yaml fenced block."
        )

        client = anthropic.Anthropic()
        start = time.monotonic()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                messages=[{"role": "user", "content": full_prompt}],
                timeout=timeout_seconds,
            )
            elapsed = time.monotonic() - start
        except Exception as exc:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=str(exc),
                model=model,
            )

        text = response.content[0].text if response.content else ""
        input_tokens = getattr(response.usage, "input_tokens", 0)
        output_tokens = getattr(response.usage, "output_tokens", 0)
        cost = round(_model_cost(model, input_tokens, output_tokens), 6)

        yaml_text = extract_yaml_block(text)
        if yaml_text is None:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=round(elapsed, 3),
                success=False,
                error="No YAML block found in Claude API response",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                raw_log=text,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_text + "\n", encoding="utf-8")

        return RunResult(
            output_path=output_path,
            conversion_time_seconds=round(elapsed, 3),
            success=True,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            raw_log=text,
        )

    # ------------------------------------------------------------------
    # Dispatch: CLI first, then API
    # ------------------------------------------------------------------
    def run(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        *,
        timeout_seconds: int = 120,
        config: dict | None = None,
    ) -> RunResult:
        config = config or {}
        model = config.get("model", "claude-sonnet-4-20250514")

        if shutil.which("claude"):
            return self._run_via_cli(input_path, output_path, prompt, timeout_seconds)

        if os.environ.get("ANTHROPIC_API_KEY"):
            return self._run_via_api(input_path, output_path, prompt, model, timeout_seconds)

        return RunResult(
            output_path=output_path,
            conversion_time_seconds=0,
            success=False,
            error="Neither claude CLI nor ANTHROPIC_API_KEY available",
        )
