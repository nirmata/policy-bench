"""Runner harness that executes tools inside isolated Docker containers.

Each invocation:
  1. Creates a temp workspace with the input policy copied in
  2. Runs ``docker run --rm`` with the tool's image
  3. Extracts the converted policy from the container workspace
  4. Cleans up the temp dir

The container sees ONLY /workspace/ (input + empty output dir).
No CLAUDE.md, no memory, no MCP servers, no previous outputs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from .base import (
    RunResult,
    ToolRunner,
    estimate_cost,
    estimate_tokens,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Serializes tee'd stream writes so concurrent workers don't interleave mid-line.
_STREAM_LOCK = threading.Lock()


def _tee_stream(src: IO[str], buf: list[str], dst: IO[str], prefix: str) -> None:
    """Read lines from ``src``, append to ``buf``, and echo to ``dst`` live.

    Used to make ``docker start -a`` output visible in the benchmark terminal
    as it arrives, instead of buffering until the container exits. The full
    captured text is still available via ``"".join(buf)`` for ``raw_log``.
    """
    try:
        for line in iter(src.readline, ""):
            buf.append(line)
            with _STREAM_LOCK:
                try:
                    dst.write(f"{prefix}{line}")
                    dst.flush()
                except (OSError, ValueError):
                    # Terminal closed or dst unwritable — keep capturing to buf.
                    pass
    finally:
        try:
            src.close()
        except (OSError, ValueError):
            pass

# Container paths (what the agent sees inside the container)
_CONTAINER_INPUT = "/workspace/policy.yaml"
_CONTAINER_OUTPUT = "/workspace/output/converted.yaml"

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_ENV_BY_TOOL = {
    "nctl": ["NIRMATA_TOKEN", "NIRMATA_URL"],
    "claude": ["ANTHROPIC_API_KEY"],
    "cursor": ["CURSOR_API_KEY"],
    "codex": ["CODEX_API_KEY"],
}


class ContainerRunner(ToolRunner):
    """Run a benchmark tool inside an isolated Docker container."""

    def __init__(self, tool_name: str):
        self.name = tool_name
        self._image = f"benchmark-{tool_name}"
        self._env_file = REPO_ROOT / "docker" / "secrets" / f"{tool_name}.env"

    def is_available(self) -> bool:
        """Check that Docker is running and the tool image exists."""
        try:
            proc = subprocess.run(
                ["docker", "image", "inspect", self._image],
                capture_output=True, timeout=10,
            )
            return proc.returncode == 0
        except Exception:
            return False

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
        container_id = None

        is_generate = not input_path or not input_path.is_file() or input_path == output_path
        container_prompt = self._rewrite_prompt(prompt, input_path, output_path)

        missing_vars = self._missing_required_env_vars()
        if missing_vars:
            vars_joined = ", ".join(missing_vars)
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=0.0,
                success=False,
                error=(
                    f"Missing required container credentials for {self.name}: {vars_joined}. "
                    f"Set them in your shell environment or in {self._env_file}."
                ),
                model=f"{self.name}-container",
            )

        env_vars = self._container_env_vars(config)
        create_cmd = ["docker", "create", "--network", "host"]
        for key, value in env_vars.items():
            create_cmd += ["-e", f"{key}={value}"]
        create_cmd += [self._image, container_prompt]

        start = time.monotonic()
        try:
            create_proc = subprocess.run(
                create_cmd,
                capture_output=True,
                text=True,
                timeout=20,
            )
            if create_proc.returncode != 0:
                return RunResult(
                    output_path=output_path,
                    conversion_time_seconds=round(time.monotonic() - start, 3),
                    success=False,
                    error=f"Failed to create container: {(create_proc.stderr or '').strip()}",
                    model=f"{self.name}-container",
                )

            container_id = (create_proc.stdout or "").strip()
            if not container_id:
                return RunResult(
                    output_path=output_path,
                    conversion_time_seconds=round(time.monotonic() - start, 3),
                    success=False,
                    error="Failed to create container: empty container id",
                    model=f"{self.name}-container",
                )

            if not is_generate:
                cp_proc = subprocess.run(
                    ["docker", "cp", str(input_path), f"{container_id}:{_CONTAINER_INPUT}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if cp_proc.returncode != 0:
                    return RunResult(
                        output_path=output_path,
                        conversion_time_seconds=round(time.monotonic() - start, 3),
                        success=False,
                        error=f"Failed to copy input policy into container: {(cp_proc.stderr or '').strip()}",
                        model=f"{self.name}-container",
                    )

            # Use Popen + tee threads so container stdout/stderr are visible
            # in the benchmark terminal in real time (the agent streams tool
            # calls, reasoning, and file writes). `raw_log` is still captured
            # in full from the buffers below.
            label = f"[{self.name}/{input_path.stem}] "
            stdout_buf: list[str] = []
            stderr_buf: list[str] = []

            start_proc = subprocess.Popen(
                ["docker", "start", "-a", container_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line-buffered
            )
            t_out = threading.Thread(
                target=_tee_stream,
                args=(start_proc.stdout, stdout_buf, sys.stdout, label),
                daemon=True,
            )
            t_err = threading.Thread(
                target=_tee_stream,
                args=(start_proc.stderr, stderr_buf, sys.stderr, label),
                daemon=True,
            )
            t_out.start()
            t_err.start()

            try:
                start_proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                start_proc.kill()
                start_proc.wait()
                t_out.join(timeout=2)
                t_err.join(timeout=2)
                raise

            t_out.join(timeout=2)
            t_err.join(timeout=2)
            elapsed = time.monotonic() - start

            output_path.parent.mkdir(parents=True, exist_ok=True)
            cp_out_proc = subprocess.run(
                ["docker", "cp", f"{container_id}:{_CONTAINER_OUTPUT}", str(output_path)],
                capture_output=True,
                text=True,
                timeout=30,
            )

            has_output = cp_out_proc.returncode == 0 and output_path.is_file()
            success = start_proc.returncode == 0 and has_output

            out_text = ""
            if has_output:
                try:
                    out_text = output_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    print(f"  Warning: could not read container output: {exc}", file=sys.stderr)
                    success = False
                    error = f"Output file exists but is unreadable: {exc}"

            raw_stdout = "".join(stdout_buf)
            raw_stderr = "".join(stderr_buf)
            raw_log = (raw_stdout + "\n" + raw_stderr).strip()

            input_tokens = estimate_tokens(container_prompt)
            output_tokens = estimate_tokens(out_text)
            cost = round(estimate_cost(input_tokens, output_tokens), 6)

            error = None
            if not success:
                if start_proc.returncode != 0:
                    # Some CLIs (e.g. Claude Code in -p mode) print fatal
                    # errors on stdout, not stderr. Prefer stderr, fall back
                    # to stdout so the message isn't lost.
                    err_source = raw_stderr or raw_stdout
                    err_snippet = err_source.strip()[:500] if err_source else f"exit code {start_proc.returncode}"
                    error = f"Container exited {start_proc.returncode}: {err_snippet}"
                elif not has_output:
                    cp_snippet = (cp_out_proc.stderr or cp_out_proc.stdout or "").strip()
                    cp_snippet = cp_snippet[:500] if cp_snippet else "no docker cp output"
                    error = f"Container exited 0 but no output file was written ({cp_snippet})"

            return RunResult(
                output_path=output_path,
                conversion_time_seconds=round(elapsed, 3),
                success=success,
                error=error,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                model=f"{self.name}-container",
                tokens_estimated=True,
                raw_log=raw_log[:5000] if raw_log else None,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error="Container operation timed out",
                model=f"{self.name}-container",
            )
        except Exception as exc:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=f"Container launch failed: {exc}",
                model=f"{self.name}-container",
            )
        finally:
            if container_id:
                try:
                    subprocess.run(
                        ["docker", "rm", "-f", container_id],
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:
                    pass  # best-effort cleanup
            if container_id:
                subprocess.run(
                    ["docker", "rm", "-f", container_id],
                    capture_output=True,
                    timeout=10,
                )

    def _missing_required_env_vars(self) -> list[str]:
        required = _REQUIRED_ENV_BY_TOOL.get(self.name, [])
        if not required:
            return []

        found = set(self._container_env_vars().keys())

        return [k for k in required if k not in found]

    def _container_env_vars(self, config: dict | None = None) -> dict[str, str]:
        env_vars = self._load_env_file()

        # Host env takes precedence over env-file values for required credentials.
        for key in _REQUIRED_ENV_BY_TOOL.get(self.name, []):
            value = os.environ.get(key)
            if value:
                env_vars[key] = value

        if config:
            for key, val in config.get("container_env", {}).items():
                if not _ENV_KEY_RE.match(key):
                    raise ValueError(f"Invalid env var name in container_env: {key!r}")
                env_vars[key] = val

        return env_vars

    def _load_env_file(self) -> dict[str, str]:
        if not self._env_file.is_file():
            return {}

        env_vars: dict[str, str] = {}
        try:
            for line in self._env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if _ENV_KEY_RE.match(key) and value.strip():
                    env_vars[key] = value.strip()
        except OSError as exc:
            print(f"  Warning: could not read {self._env_file}: {exc}", file=sys.stderr)

        return env_vars

    @staticmethod
    def _rewrite_prompt(prompt: str, input_path: Path | None, output_path: Path) -> str:
        """Replace host-absolute paths in the prompt with container paths.

        For generation tasks the orchestrator passes ``input_path == output_path``
        as a sentinel (no source file exists). In that case, replacing
        ``input_path`` first would consume the host output path string before
        the second replace can rewrite it to ``_CONTAINER_OUTPUT``, leaving
        the prompt telling the agent to write to ``/workspace/policy.yaml`` —
        which the orchestrator never reads back. Skip the input replace
        whenever ``input_path == output_path``.
        """
        rewritten = prompt
        if input_path and input_path != output_path and str(input_path) in rewritten:
            rewritten = rewritten.replace(str(input_path), _CONTAINER_INPUT)
        rewritten = rewritten.replace(str(output_path), _CONTAINER_OUTPUT)
        return rewritten
