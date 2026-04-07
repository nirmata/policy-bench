"""Runner harness for Cursor Agent conversion.

Cursor is the subject of the benchmark.  This harness:
  1. Sends the prompt via  cursor -p "..." --force --output-format json
     (CLI agent mode, non-interactive)
  2. Captures the raw YAML output (file written by agent, or extracted from response)
  3. Measures wall-clock time externally
  4. Estimates tokens from prompt + output sizes (Cursor uses Claude/GPT
     models underneath but doesn't expose real token counts)
  5. Computes estimated cost based on underlying model pricing
  6. Returns a RunResult for eval + JSON emission

When the CLI is not available, falls back to a manual workflow where the
user pastes the prompt into the Cursor IDE and the harness polls for the
output file.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from .base import (
    RunResult,
    ToolRunner,
    estimate_cost,
    estimate_tokens,
    DEFAULT_INPUT_RATE,
    DEFAULT_OUTPUT_RATE,
    run_cli_subprocess,
)


def _get_cursor_version() -> str | None:
    try:
        proc = subprocess.run(
            ["cursor", "--version"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return None


def _cursor_raw_log(stdout: str, stderr: str) -> str | None:
    """Include both stdout and stderr in the raw log for Cursor runs."""
    combined = (stdout + "\n" + stderr)[:5000]
    return combined or None


class CursorRunner(ToolRunner):
    name = "cursor"

    def is_available(self) -> bool:
        return bool(shutil.which("cursor"))

    # ------------------------------------------------------------------
    # Primary: Cursor CLI agent mode  (cursor -p "..." --force)
    # ------------------------------------------------------------------
    def _run_via_cli(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        timeout_seconds: int,
    ) -> RunResult:
        full_prompt = (
            f"{prompt}\n\n"
            f"The source policy file is at: {input_path}\n"
            f"Write the converted policy to: {output_path}"
        )

        cmd = [
            "cursor",
            "-p", full_prompt,
            "--force",
            "--output-format", "json",
        ]

        return run_cli_subprocess(
            cmd=cmd,
            full_prompt=full_prompt,
            output_path=output_path,
            timeout=timeout_seconds,
            default_model="cursor-agent-cli",
            tool_version=_get_cursor_version(),
            raw_log_builder=_cursor_raw_log,
        )

    # ------------------------------------------------------------------
    # Fallback: manual mode (print prompt, poll for output file)
    # ------------------------------------------------------------------
    def _run_manual(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        timeout_seconds: int,
    ) -> RunResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_path.exists():
            output_path.unlink()

        full_prompt = (
            f"{prompt}\n\n"
            f"The source policy file is at: {input_path}\n"
            f"Write the converted policy to: {output_path}"
        )

        print("\n" + "=" * 60, file=sys.stderr)
        print("  CURSOR AGENT -- manual step required", file=sys.stderr)
        print("  (install cursor CLI for automated runs)", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(f"\n  Paste this prompt into Cursor Agent:\n", file=sys.stderr)
        print(f"  {full_prompt}\n", file=sys.stderr)
        print(
            f"  Expected output: {output_path}\n"
            f"  Timeout: {timeout_seconds}s\n",
            file=sys.stderr,
        )
        print("  Waiting for output file...", file=sys.stderr)

        start = time.monotonic()
        poll_interval = 2.0

        while True:
            elapsed = time.monotonic() - start
            if output_path.exists() and output_path.stat().st_size > 0:
                elapsed = time.monotonic() - start
                print(
                    f"  Output detected after {elapsed:.1f}s\n",
                    file=sys.stderr,
                )

                # Estimate tokens from what we know
                input_tokens = estimate_tokens(full_prompt)
                out_text = ""
                try:
                    out_text = output_path.read_text(encoding="utf-8")
                except Exception:
                    pass
                output_tokens = estimate_tokens(out_text)
                cost = round(
                    estimate_cost(input_tokens, output_tokens, DEFAULT_INPUT_RATE, DEFAULT_OUTPUT_RATE),
                    6,
                )

                return RunResult(
                    output_path=output_path,
                    conversion_time_seconds=round(elapsed, 3),
                    success=True,
                    model="cursor-agent-manual",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    tokens_estimated=True,
                )
            if elapsed >= timeout_seconds:
                return RunResult(
                    output_path=output_path,
                    conversion_time_seconds=round(elapsed, 3),
                    success=False,
                    error=f"Timed out after {timeout_seconds}s waiting for {output_path}",
                    model="cursor-agent-manual",
                    tokens_estimated=True,
                )
            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Dispatch: CLI first, then manual
    # ------------------------------------------------------------------
    def run(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        *,
        timeout_seconds: int = 300,
        config: dict | None = None,
    ) -> RunResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if shutil.which("cursor"):
            return self._run_via_cli(input_path, output_path, prompt, timeout_seconds)

        return self._run_manual(input_path, output_path, prompt, timeout_seconds)
