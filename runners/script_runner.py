"""Generic runner harness that delegates to a shell script.

Any tool can be benchmarked by providing a script that follows this contract:

    ./run_tool_<name>.sh <source-policy-path> <output-path> "<prompt>"
    # Exit 0 on success, non-zero on failure.
    # The converted/generated policy must be written to <output-path>.
    # <source-policy-path> is "none" for generation tasks.

For generate_test tasks, BENCH_OUTPUT_KIND=dir is set in the subprocess
environment and <output-path> is a pre-created directory.  The script should
write kyverno-test.yaml and resources.yaml into it.

After the script exits, the harness:
  - Checks for the output file (or kyverno-test.yaml in dir mode)
  - Reads an optional sidecar <output-path>.meta.json for real token counts:
      {"input_tokens": N, "output_tokens": N, "model": "...", "tool_version": "..."}
  - Falls back to heuristic token estimation if no sidecar is found
  - Returns a RunResult for eval + JSON emission
"""

from __future__ import annotations

import json as _json
import os
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


class ScriptRunner(ToolRunner):
    """Wraps an arbitrary shell script as a benchmark runner."""

    def __init__(self, tool_name: str, script_path: Path) -> None:
        self.name = tool_name
        self._script = script_path

    def is_available(self) -> bool:
        return self._script.is_file()

    def run(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        *,
        timeout_seconds: int = 120,
        config: dict | None = None,
    ) -> RunResult:
        repo_root = Path(__file__).resolve().parent.parent

        # dir_output_artifact returns a path if output_path is a pre-created
        # directory (generate_test tasks), None for single-file tasks.
        output_check = dir_output_artifact(output_path)
        is_dir_output = output_check is not None
        if not is_dir_output:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        source_arg = str(input_path) if input_path and input_path.is_file() else "none"
        cmd = [str(self._script), source_arg, str(output_path), prompt]

        # Signal dir-output mode to the shell script via environment variable.
        env = {**os.environ, "BENCH_OUTPUT_KIND": "dir" if is_dir_output else "file"}

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(repo_root),
                env=env,
            )
            elapsed = time.monotonic() - start
        except subprocess.TimeoutExpired:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=f"{self.name} script timed out after {timeout_seconds}s",
            )

        log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        output_check = output_check or output_path
        success = proc.returncode == 0 and output_check.exists()

        # Read optional sidecar metadata
        meta_path = Path(str(output_path) + ".meta.json")
        input_tokens = None
        output_tokens = None
        cost = None
        model = None
        tool_version = None
        tokens_estimated = True

        if meta_path.exists():
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                input_tokens = meta.get("input_tokens")
                output_tokens = meta.get("output_tokens")
                model = meta.get("model")
                tool_version = meta.get("tool_version")
                if input_tokens is not None and output_tokens is not None:
                    tokens_estimated = False
                    cost = round(estimate_cost(input_tokens, output_tokens), 6)
            except (_json.JSONDecodeError, OSError):
                pass

        # Fallback: estimate tokens from text sizes
        if input_tokens is None:
            input_tokens = estimate_tokens(prompt)
        if output_tokens is None:
            out_text = ""
            if output_check.is_file():
                try:
                    out_text = output_check.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    print(f"  Warning: could not read output file: {exc}", file=sys.stderr)
            output_tokens = estimate_tokens(out_text)
        if cost is None:
            cost = round(estimate_cost(input_tokens, output_tokens), 6)

        return RunResult(
            output_path=output_path,
            conversion_time_seconds=round(elapsed, 3),
            success=success,
            error=None if success else f"{self.name} script exited {proc.returncode}",
            model=model or f"{self.name}-script",
            tool_version=tool_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            tokens_estimated=tokens_estimated,
            raw_log=log[:5000] if log.strip() else None,
        )
