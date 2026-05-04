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
import select
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
        config = config or {}
        task_type = config.get("task_type")

        # dir_output_artifact returns a path if output_path is a pre-created
        # directory (generate_test tasks), None for single-file tasks.
        output_check = dir_output_artifact(output_path, task_type=task_type)
        is_dir_output = output_check is not None
        if not is_dir_output:
            output_path.parent.mkdir(parents=True, exist_ok=True)

        source_arg = str(input_path) if input_path and input_path.is_file() else "none"
        cmd = [str(self._script), source_arg, str(output_path), prompt]

        # Signal dir-output mode to the shell script via environment variable.
        env = {
            **os.environ,
            "BENCH_OUTPUT_KIND": "dir" if is_dir_output else "file",
            "BENCH_TASK_TYPE": str(task_type or "convert"),
        }
        stream_logs = bool(config.get("stream_logs", True))
        if output_check is not None:
            env["BENCH_OUTPUT_ARTIFACT"] = output_check.name

        start = time.monotonic()
        if output_path.is_dir():
            run_log_path = output_path / f"{self.name}-runner.log"
        else:
            run_log_path = output_path.with_suffix(f".{self.name}.runner.log")
        run_log_path.parent.mkdir(parents=True, exist_ok=True)

        log_chunks: list[str] = []
        returncode = -1
        try:
            with run_log_path.open("w", encoding="utf-8") as logf:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(repo_root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                assert proc.stdout is not None

                while True:
                    elapsed = time.monotonic() - start
                    if elapsed > timeout_seconds:
                        proc.kill()
                        tail = proc.stdout.read() or ""
                        if tail:
                            log_chunks.append(tail)
                            logf.write(tail)
                            logf.flush()
                        return RunResult(
                            output_path=output_path,
                            conversion_time_seconds=elapsed,
                            success=False,
                            error=f"{self.name} script timed out after {timeout_seconds}s",
                            raw_log=("".join(log_chunks)[:5000] if log_chunks else None),
                            extra={"run_log_path": str(run_log_path)},
                        )

                    ready, _, _ = select.select([proc.stdout], [], [], 0.5)
                    if ready:
                        line = proc.stdout.readline()
                        if line:
                            log_chunks.append(line)
                            logf.write(line)
                            logf.flush()
                            if stream_logs:
                                print(line, end="")

                    if proc.poll() is not None:
                        tail = proc.stdout.read() or ""
                        if tail:
                            log_chunks.append(tail)
                            logf.write(tail)
                            logf.flush()
                            if stream_logs:
                                print(tail, end="")
                        returncode = proc.returncode
                        break
        except Exception as exc:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=f"{self.name} script runner error: {exc}",
            )

        elapsed = time.monotonic() - start
        log = "".join(log_chunks)
        output_check = output_check or output_path
        success = returncode == 0 and output_check.exists()

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
            error=None if success else f"{self.name} script exited {returncode}",
            model=model or f"{self.name}-script",
            tool_version=tool_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            tokens_estimated=tokens_estimated,
            raw_log=log[:5000] if log.strip() else None,
            extra={"run_log_path": str(run_log_path)},
        )
