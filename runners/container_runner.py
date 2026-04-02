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

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .base import (
    RunResult,
    ToolRunner,
    estimate_cost,
    estimate_tokens,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Container paths (what the agent sees inside the container)
_CONTAINER_INPUT = "/workspace/policy.yaml"
_CONTAINER_OUTPUT = "/workspace/output/converted.yaml"

# Default image names (built by docker/build.sh)
_IMAGES = {
    "nctl": "benchmark-nctl",
    "claude": "benchmark-claude",
    "cursor": "benchmark-cursor",
    "codex": "benchmark-codex",
}

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ContainerRunner(ToolRunner):
    """Run a benchmark tool inside an isolated Docker container."""

    def __init__(self, tool_name: str):
        self.name = tool_name
        self._image = _IMAGES.get(tool_name, f"benchmark-{tool_name}")
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

        # --- 1. Create ephemeral workspace ---
        workspace = tempfile.mkdtemp(prefix="benchmark_")
        ws_output_dir = Path(workspace) / "output"
        ws_output_dir.mkdir()

        try:
            return self._run_in_container(
                input_path, output_path, prompt,
                workspace, ws_output_dir,
                timeout_seconds, config,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _run_in_container(
        self,
        input_path: Path,
        output_path: Path,
        prompt: str,
        workspace: str,
        ws_output_dir: Path,
        timeout_seconds: int,
        config: dict,
    ) -> RunResult:
        # --- 2. Copy input policy into workspace ---
        # For generation tasks, benchmark.py passes output_path as input_path
        # (via ``input_path or output_path``), so the equality check detects that.
        is_generate = not input_path or not input_path.is_file() or input_path == output_path
        if not is_generate:
            shutil.copy2(input_path, Path(workspace) / "policy.yaml")

        # --- 3. Rewrite prompt paths for container ---
        container_prompt = self._rewrite_prompt(prompt, input_path, output_path)

        # --- 4. Build docker command ---
        # --network host: no-op on macOS Docker Desktop (bridge still provides
        # outbound internet), but gives direct host network on Linux.
        cmd = [
            "docker", "run", "--rm",
            "--network", "host",
            "-v", f"{workspace}:/workspace",
        ]

        if self._env_file.is_file():
            cmd += ["--env-file", str(self._env_file)]

        for key, val in config.get("container_env", {}).items():
            if not _ENV_KEY_RE.match(key):
                raise ValueError(f"Invalid env var name in container_env: {key!r}")
            cmd += ["-e", f"{key}={val}"]

        cmd += [self._image, container_prompt]

        # --- 5. Execute ---
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            elapsed = time.monotonic() - start
        except subprocess.TimeoutExpired:
            return RunResult(
                output_path=output_path,
                conversion_time_seconds=time.monotonic() - start,
                success=False,
                error=f"Container timed out after {timeout_seconds}s",
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

        container_output = ws_output_dir / "converted.yaml"
        has_output = container_output.is_file()
        success = proc.returncode == 0 and has_output

        out_text = ""
        if has_output:
            try:
                out_text = container_output.read_text(encoding="utf-8")
            except Exception:
                pass
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(container_output, output_path)

        raw_stdout = proc.stdout or ""
        raw_stderr = proc.stderr or ""
        raw_log = (raw_stdout + "\n" + raw_stderr).strip()

        input_tokens = estimate_tokens(container_prompt)
        output_tokens = estimate_tokens(out_text)
        cost = round(estimate_cost(input_tokens, output_tokens), 6)

        error = None
        if not success:
            if proc.returncode != 0:
                # Truncate stderr for the error message
                err_snippet = raw_stderr[:500] if raw_stderr else f"exit code {proc.returncode}"
                error = f"Container exited {proc.returncode}: {err_snippet}"
            elif not container_output.is_file():
                error = "Container exited 0 but no output file was written"

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

    @staticmethod
    def _rewrite_prompt(prompt: str, input_path: Path | None, output_path: Path) -> str:
        """Replace host-absolute paths in the prompt with container paths."""
        rewritten = prompt
        if input_path and str(input_path) in rewritten:
            rewritten = rewritten.replace(str(input_path), _CONTAINER_INPUT)
        rewritten = rewritten.replace(str(output_path), _CONTAINER_OUTPUT)
        return rewritten
