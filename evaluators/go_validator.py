"""Call the Go validate-policy binary for schema + CEL validation.

Falls back to None (caller uses Python checks) if the binary is missing,
crashes, or returns unparseable output.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    except subprocess.TimeoutExpired:
        print(f"  Warning: Go validator timed out for {policy_path}, falling back to Python validator", file=sys.stderr)
        return None
    except (FileNotFoundError, OSError):
        return None

    if proc.returncode == 2:
        print(f"  Warning: Go validator exited with code 2 for {policy_path}, falling back to Python validator", file=sys.stderr)
        return None

    try:
        result = json.loads(proc.stdout)
        result["validator_used"] = "go"
        return result
    except (json.JSONDecodeError, ValueError):
        print(f"  Warning: Go validator returned unparseable output for {policy_path}, falling back to Python validator", file=sys.stderr)
        return None
