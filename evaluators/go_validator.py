"""Call the Go validate-policy binary for schema + CEL validation.

Falls back to None (caller uses Python checks) if the binary is missing,
crashes, or returns unparseable output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BINARY = REPO_ROOT / "validate-policy"


def validate_with_go(policy_path: Path, timeout: int = 30) -> dict | None:
    """Run the Go validator. Returns parsed JSON dict or None if unavailable."""
    if not BINARY.is_file():
        return None

    cmd = [str(BINARY), "--policy", str(policy_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    if proc.returncode == 2:
        return None

    try:
        return json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
