"""Runner harness that executes tools inside isolated Docker containers.

Supports two modes:

**Ephemeral** (default): one container per task.
  1. ``docker create`` with the tool's entrypoint
  2. ``docker cp`` input policy in
  3. ``docker start -a`` (runs the conversion, then exits)
  4. ``docker cp`` output out
  5. ``docker rm``

**Persistent** (``persistent=True``): one long-lived container per tool.
  The container stays running across all tasks for a tool, allowing the
  agent to accumulate context (files, memory, notes) between conversions —
  just like a real user would.  Between tasks the harness cleans only the
  input and output files; everything else the agent created is preserved.

  Tool-to-tool isolation is guaranteed: each tool gets its own container.

  1. ``setup()``  → ``docker create --entrypoint sleep`` + start
  2. ``run()`` × N → clean workspace, ``docker cp`` input in,
     ``docker exec`` entrypoint, ``docker cp`` output out
  3. ``teardown()`` → ``docker rm -f``
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
_CONTAINER_OUTPUT_DIR = "/workspace/output"

# Entrypoint paths inside the container image (set by Dockerfile)
_ENTRYPOINTS = {
    "claude": "/entrypoints/run-claude.sh",
    "cursor": "/entrypoints/run-cursor.sh",
    "nctl": "/entrypoints/run-nctl.sh",
    "codex": "/entrypoints/run-codex.sh",
}

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_ENV_BY_TOOL = {
    "nctl": ["NIRMATA_TOKEN", "NIRMATA_URL"],
    "claude": ["ANTHROPIC_API_KEY"],
    "cursor": ["CURSOR_API_KEY"],
    "codex": ["CODEX_API_KEY"],
}

# Upstream-network failures we retry automatically in persistent mode.
# These are all Go-net / HTTP-client error texts; we match substrings so they
# hit even when wrapped in outer error messages (e.g. nctl's
# "failed to run conversation: Post \"...\": unexpected EOF").
_TRANSIENT_ERROR_PATTERNS = (
    "unexpected EOF",
    "connection reset",
    "i/o timeout",
    "context deadline exceeded",
)
_MAX_TRANSIENT_ATTEMPTS = 3  # 1 initial + up to 2 retries
_TRANSIENT_RETRY_SLEEP = 10  # seconds between retries


def _is_transient_error(text: str) -> bool:
    return any(p in text for p in _TRANSIENT_ERROR_PATTERNS)


class ContainerRunner(ToolRunner):
    """Run a benchmark tool inside an isolated Docker container.

    When ``persistent=True``, call ``setup()`` before the first ``run()``
    and ``teardown()`` after the last.  The same container is reused for
    every task, giving the agent continuity between conversions.
    """

    def __init__(self, tool_name: str, *, persistent: bool = False):
        self.name = tool_name
        self._image = f"benchmark-{tool_name}"
        self._env_file = REPO_ROOT / "docker" / "secrets" / f"{tool_name}.env"
        self._persistent = persistent
        self._container_id: str | None = None

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

    # ------------------------------------------------------------------
    # Persistent container lifecycle
    # ------------------------------------------------------------------

    def setup(self, config: dict | None = None) -> None:
        """Create and start a long-lived container (persistent mode only).

        The container runs ``sleep infinity`` so it stays alive between
        tasks.  The original entrypoint is invoked per-task via
        ``docker exec``.
        """
        if not self._persistent:
            return
        if self._container_id:
            return  # already running

        config = config or {}
        missing = self._missing_required_env_vars()
        if missing:
            raise RuntimeError(
                f"Missing required credentials for {self.name}: {', '.join(missing)}"
            )

        env_vars = self._container_env_vars(config)
        create_cmd = [
            "docker", "create",
            "--network", "host",
            "--entrypoint", "sleep",
        ]
        for key, value in env_vars.items():
            create_cmd += ["-e", f"{key}={value}"]
        create_cmd += [self._image, "infinity"]

        proc = subprocess.run(
            create_cmd, capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create persistent container: {(proc.stderr or '').strip()}"
            )

        self._container_id = (proc.stdout or "").strip()

        # Start the container (sleep infinity keeps it alive)
        start_proc = subprocess.run(
            ["docker", "start", self._container_id],
            capture_output=True, text=True, timeout=20,
        )
        if start_proc.returncode != 0:
            raise RuntimeError(
                f"Failed to start persistent container: {(start_proc.stderr or '').strip()}"
            )

        print(f"  [{self.name}] Persistent container started: {self._container_id[:12]}", file=sys.stderr)

    def teardown(self) -> None:
        """Stop and remove the persistent container."""
        if self._container_id:
            print(f"  [{self.name}] Tearing down persistent container: {self._container_id[:12]}", file=sys.stderr)
            subprocess.run(
                ["docker", "rm", "-f", self._container_id],
                capture_output=True, timeout=10,
            )
            self._container_id = None

    # ------------------------------------------------------------------
    # Main run method — dispatches to ephemeral or persistent
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
        if self._persistent:
            return self._run_persistent(input_path, output_path, prompt, timeout_seconds)
        return self._run_ephemeral(input_path, output_path, prompt, timeout_seconds, config)

    # ------------------------------------------------------------------
    # Persistent mode: exec into the long-lived container
    # ------------------------------------------------------------------

    def _run_persistent(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        timeout_seconds: int,
    ) -> RunResult:
        cid = self._container_id
        if not cid:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=0,
                success=False,
                error="Persistent container not started — call setup() first",
                model=f"{self.name}-container",
            )

        is_generate = not input_path or not input_path.is_file() or input_path == output_path
        container_prompt = self._rewrite_prompt(prompt, input_path, output_path)

        # Clean workspace from previous task: remove old input + output.
        # Everything else the agent created (CLAUDE.md, notes, etc.) is preserved.
        subprocess.run(
            ["docker", "exec", cid, "sh", "-c",
             f"rm -f {_CONTAINER_INPUT} && rm -rf {_CONTAINER_OUTPUT_DIR} && mkdir -p {_CONTAINER_OUTPUT_DIR}"],
            capture_output=True, text=True, timeout=10,
        )

        # Copy new input policy
        if not is_generate:
            cp_proc = subprocess.run(
                ["docker", "cp", str(input_path), f"{cid}:{_CONTAINER_INPUT}"],
                capture_output=True, text=True, timeout=30,
            )
            if cp_proc.returncode != 0:
                return RunResult(
                    output_path=output_path,
                    conversion_time_seconds=0,
                    success=False,
                    error=f"Failed to copy input: {(cp_proc.stderr or '').strip()}",
                    model=f"{self.name}-container",
                )

        # Execute the tool's entrypoint via docker exec
        entrypoint = _ENTRYPOINTS.get(self.name)
        if not entrypoint:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=0,
                success=False,
                error=f"No entrypoint known for tool {self.name!r}",
                model=f"{self.name}-container",
            )

        label = f"[{self.name}/{input_path.stem}] "
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        start = time.monotonic()
        exec_proc = None
        try:
            # Retry loop for transient upstream failures (e.g. nctl's Nirmata
            # LLM proxy closing the connection mid-response with "unexpected
            # EOF"). Non-transient exits break out immediately so we don't
            # waste attempts on real conversion errors.
            for attempt in range(1, _MAX_TRANSIENT_ATTEMPTS + 1):
                per_stdout: list[str] = []
                per_stderr: list[str] = []

                exec_proc = subprocess.Popen(
                    ["docker", "exec", cid, entrypoint, container_prompt],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
                t_out = threading.Thread(
                    target=_tee_stream,
                    args=(exec_proc.stdout, per_stdout, sys.stdout, label),
                    daemon=True,
                )
                t_err = threading.Thread(
                    target=_tee_stream,
                    args=(exec_proc.stderr, per_stderr, sys.stderr, label),
                    daemon=True,
                )
                t_out.start()
                t_err.start()

                try:
                    exec_proc.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    exec_proc.kill()
                    exec_proc.wait()
                    t_out.join(timeout=2)
                    t_err.join(timeout=2)
                    stdout_buf.extend(per_stdout)
                    stderr_buf.extend(per_stderr)
                    return RunResult(
                        output_path=output_path,
                        conversion_time_seconds=time.monotonic() - start,
                        success=False,
                        error=f"Task timed out after {timeout_seconds}s",
                        model=f"{self.name}-container",
                    )

                t_out.join(timeout=2)
                t_err.join(timeout=2)

                stdout_buf.extend(per_stdout)
                stderr_buf.extend(per_stderr)

                if exec_proc.returncode == 0:
                    break

                per_combined = "".join(per_stdout) + "\n" + "".join(per_stderr)
                if (
                    not _is_transient_error(per_combined)
                    or attempt >= _MAX_TRANSIENT_ATTEMPTS
                ):
                    break

                marker = (
                    f"\n[harness] transient upstream error detected "
                    f"(attempt {attempt}/{_MAX_TRANSIENT_ATTEMPTS}), "
                    f"retrying in {_TRANSIENT_RETRY_SLEEP}s...\n"
                )
                with _STREAM_LOCK:
                    try:
                        sys.stderr.write(label + marker)
                        sys.stderr.flush()
                    except (OSError, ValueError):
                        pass
                stderr_buf.append(marker)

                time.sleep(_TRANSIENT_RETRY_SLEEP)

                # Reset workspace + re-copy input so the next attempt starts
                # from a clean slate (the previous attempt may have written a
                # partial output file before the connection dropped).
                subprocess.run(
                    ["docker", "exec", cid, "sh", "-c",
                     f"rm -f {_CONTAINER_INPUT} && rm -rf {_CONTAINER_OUTPUT_DIR} "
                     f"&& mkdir -p {_CONTAINER_OUTPUT_DIR}"],
                    capture_output=True, text=True, timeout=10,
                )
                if not is_generate:
                    subprocess.run(
                        ["docker", "cp", str(input_path), f"{cid}:{_CONTAINER_INPUT}"],
                        capture_output=True, text=True, timeout=30,
                    )

            elapsed = time.monotonic() - start

        except Exception as exc:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=f"docker exec failed: {exc}",
                model=f"{self.name}-container",
            )

        # Extract output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cp_out = subprocess.run(
            ["docker", "cp", f"{cid}:{_CONTAINER_OUTPUT}", str(output_path)],
            capture_output=True, text=True, timeout=30,
        )

        has_output = cp_out.returncode == 0 and output_path.is_file()
        success = exec_proc.returncode == 0 and has_output

        out_text = ""
        if has_output:
            try:
                out_text = output_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                print(f"  Warning: could not read output: {exc}", file=sys.stderr)
                success = False

        raw_stdout = "".join(stdout_buf)
        raw_stderr = "".join(stderr_buf)
        raw_log = (raw_stdout + "\n" + raw_stderr).strip()

        input_tokens = estimate_tokens(container_prompt)
        output_tokens = estimate_tokens(out_text)
        cost = round(estimate_cost(input_tokens, output_tokens), 6)

        error = None
        if not success:
            if exec_proc.returncode != 0:
                err_source = raw_stderr or raw_stdout
                err_snippet = err_source.strip()[:500] if err_source else f"exit code {exec_proc.returncode}"
                error = f"Tool exited {exec_proc.returncode}: {err_snippet}"
            elif not has_output:
                cp_snippet = (cp_out.stderr or cp_out.stdout or "").strip()[:500]
                error = f"Tool exited 0 but no output file was written ({cp_snippet or 'no details'})"

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

    # ------------------------------------------------------------------
    # Ephemeral mode: one container per task (original behavior)
    # ------------------------------------------------------------------

    def _run_ephemeral(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        timeout_seconds: int,
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
                    err_source = raw_stderr or raw_stdout
                    err_snippet = err_source.strip()[:500] if err_source else f"exit code {start_proc.returncode}"
                    error = f"Container exited {start_proc.returncode}: {err_snippet}"
                elif not has_output:
                    cp_snippet = (cp_out_proc.stderr or cp_out_proc.stdout or "").strip()
                    cp_snippet = cp_snippet[:500] if cp_snippet else "no docker cp output"
                    error = f"Container exited 0 but no output file was written ({cp_snippet})"
                elif has_output and not out_text:
                    error = "Output file exists but could not be read"

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
                    pass

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

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

    def _rewrite_prompt(self, prompt: str, input_path: Path | None, output_path: Path) -> str:
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
