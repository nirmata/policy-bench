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

import json as _json
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
    extract_yaml_block,
)

# Cursor typically routes to Claude Sonnet — use those rates as a baseline
_DEFAULT_INPUT_RATE = 3.0   # USD per 1M tokens
_DEFAULT_OUTPUT_RATE = 15.0


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


class CursorRunner(ToolRunner):
    name = "cursor"

    def is_available(self) -> bool:
        if shutil.which("cursor"):
            return True
        return True  # manual fallback is always available

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
        repo_root = Path(__file__).resolve().parent.parent
        version = _get_cursor_version()

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

        # --- step 3: measure wall-clock time ---
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
                error=f"cursor CLI timed out after {timeout_seconds}s",
                model="cursor-agent-cli",
                tool_version=version,
            )

        # --- step 2: capture the raw output ---
        raw_text = proc.stdout or ""
        stderr_text = proc.stderr or ""

        # Try to parse JSON envelope from --output-format json
        real_input_tokens = None
        real_output_tokens = None
        model_name = "cursor-agent-cli"
        try:
            data = _json.loads(raw_text)
            raw_text = data.get("result", raw_text)
            model_name = data.get("model", model_name)
            usage = data.get("usage", {})
            real_input_tokens = usage.get("input_tokens")
            real_output_tokens = usage.get("output_tokens")
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

        # --- step 4: estimate tokens ---
        tokens_estimated = real_input_tokens is None
        input_tokens = real_input_tokens or estimate_tokens(full_prompt)

        output_file_text = ""
        if output_path.exists():
            try:
                output_file_text = output_path.read_text(encoding="utf-8")
            except Exception:
                pass
        output_tokens = real_output_tokens or estimate_tokens(output_file_text)

        # --- step 5: compute cost ---
        cost = round(
            estimate_cost(input_tokens, output_tokens, _DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE),
            6,
        )

        return RunResult(
            output_path=output_path,
            conversion_time_seconds=round(elapsed, 3),
            success=success,
            error=None if success else f"cursor CLI exited {proc.returncode}",
            model=model_name,
            tool_version=version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            tokens_estimated=tokens_estimated,
            raw_log=(raw_text + "\n" + stderr_text)[:5000] or None,
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
                    estimate_cost(input_tokens, output_tokens, _DEFAULT_INPUT_RATE, _DEFAULT_OUTPUT_RATE),
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
