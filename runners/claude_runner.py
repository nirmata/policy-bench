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

import os
import shutil
import subprocess
import time
from pathlib import Path

from .base import (
    RunResult,
    ToolRunner,
    dir_output_artifact,
    extract_yaml_block,
    model_cost,
    run_cli_subprocess,
)


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
        output_check_path = dir_output_artifact(output_path)

        if output_check_path is not None:
            full_prompt = (
                f"{prompt}\n\n"
                f"The policy file is already at: {output_path / 'policy.yaml'}\n"
                f"Write kyverno-test.yaml and resources.yaml to: {output_path}"
            )
        else:
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

        return run_cli_subprocess(
            cmd=cmd,
            full_prompt=full_prompt,
            output_path=output_path,
            timeout=timeout_seconds,
            default_model="claude-code-cli",
            tool_version=_get_claude_version(),
            output_check_path=output_check_path,
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
        if output_path.is_dir():
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=0,
                success=False,
                error="generate_test tasks require the Claude CLI; API mode cannot write multiple files",
            )

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
        cost = round(model_cost(model, input_tokens, output_tokens), 6)

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
