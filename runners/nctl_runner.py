"""Runner harness for Nirmata nctl AI conversion.

nctl is the subject of the benchmark.  This harness:
  1. Sends the prompt via  nctl ai --prompt "..."
  2. Captures stdout/stderr and checks for skill-loading markers
  3. Measures wall-clock time externally
  4. Estimates tokens from prompt + output sizes (nctl doesn't expose usage)
  5. Returns a RunResult for eval + JSON emission
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
    dir_output_artifact,
    estimate_cost,
    estimate_tokens,
)


class NctlRunner(ToolRunner):
    name = "nctl"

    _CONVERTING_SKILL = "Reading file from ~/.nirmata/nctl/skills/policy-skills/converting-policies/SKILL.md"
    _GENERATING_SKILL = "Reading file from ~/.nirmata/nctl/skills/policy-skills/generating-policies/SKILL.md"
    _AGENT_OK = "Agent completed successfully!"

    def _nctl_bin(self) -> str:
        import os
        return os.environ.get("NCTL_BIN") or shutil.which("nctl") or "nctl"

    def is_available(self) -> bool:
        import os
        return bool(os.environ.get("NCTL_BIN")) or shutil.which("nctl") is not None

    def _get_version(self) -> str | None:
        try:
            proc = subprocess.run(
                [self._nctl_bin(), "version"],
                capture_output=True, text=True, timeout=10,
            )
            return proc.stdout.strip().splitlines()[0] if proc.stdout else None
        except Exception:
            return None

    def run(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        *,
        timeout_seconds: int = 120,
        config: dict | None = None,
    ) -> RunResult:
        nctl_bin = self._nctl_bin()
        if not self.is_available():
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=0,
                success=False,
                error="nctl not found on PATH",
            )

        repo_root = Path(__file__).resolve().parent.parent
        version = self._get_version()
        output_check = dir_output_artifact(output_path)
        if output_check is None:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            nctl_bin, "ai",
            "--provider", "bedrock",
            "--model", "us.anthropic.claude-sonnet-4-6",
            "--allowed-dirs", str(repo_root),
            "--prompt", prompt,
            "--skip-permission-checks",
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
                error=f"nctl timed out after {timeout_seconds}s",
                tool_version=version,
            )

        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        output_check = output_check or output_path
        success = (
            proc.returncode == 0
            and output_check.exists()
            and self._AGENT_OK in log
        )

        # --- step 4: estimate tokens (nctl doesn't expose real counts) ---
        input_toks = estimate_tokens(prompt)
        output_text = ""
        if output_check.is_file():
            try:
                output_text = output_check.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"  Warning: could not read output file: {exc}", file=sys.stderr)
        output_toks = estimate_tokens(output_text)
        cost = estimate_cost(input_toks, output_toks)

        extra = {
            "converting_skill_loaded": self._CONVERTING_SKILL in log,
            "generating_skill_loaded": self._GENERATING_SKILL in log,
            "agent_completed": self._AGENT_OK in log,
        }

        return RunResult(
            output_path=output_path,
            conversion_time_seconds=round(elapsed, 3),
            success=success,
            error=None if success else f"nctl exited {proc.returncode}",
            tool_version=version,
            model="nctl-builtin",
            input_tokens=input_toks,
            output_tokens=output_toks,
            cost_usd=round(cost, 6),
            tokens_estimated=True,
            raw_log=log,
            extra=extra,
        )
