"""Semantic validation via the Kyverno CLI test command."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# New policy types don't have named rules — kyverno test shows "Excluded"
# if a rule field is present but doesn't match.
_NEW_POLICY_KINDS = {
    "ValidatingPolicy",
    "MutatingPolicy",
    "GeneratingPolicy",
    "DeletingPolicy",
    "NamespacedDeletingPolicy",
    "ImageValidatingPolicy",
}


def run_kyverno_test(
    test_dir: Path,
    *,
    output_policy_name: str | None = None,
    output_policy_kind: str | None = None,
    policy_under_test: Path | None = None,
    timeout_sec: int = 60,
) -> tuple[bool, list[str], bool]:
    """Run ``kyverno test <test_dir>``.

    Returns (passed, errors, skipped).

    If *policy_under_test* is set (the converted/generated policy file), it
    replaces the ``policies`` entries in the test manifest so the CLI evaluates
    the benchmark output, not the bundled source policy.

    If *output_policy_name* is set, patches ``results[].policy`` to match the
    converted policy's ``metadata.name``.

    If *output_policy_kind* is a new policy type (ValidatingPolicy, etc.),
    strips the ``rule`` field from results entries — new types don't have
    named rules and kyverno test marks them "Excluded" if present.
    """
    if not shutil.which("kyverno"):
        return False, [], True

    test_dir = test_dir.resolve()
    if not test_dir.is_dir():
        return False, [f"Kyverno test dir not found: {test_dir}"], False

    run_dir = test_dir
    cleanup_dir: Path | None = None

    if yaml and (policy_under_test is not None or output_policy_name):
        try:
            cleanup_dir = Path(tempfile.mkdtemp(prefix="kyverno_test_"))
            # Copy all resource/test YAML assets from the suite (resource.yaml, resources.yaml, values.yaml, etc.)
            for f in test_dir.iterdir():
                if f.suffix not in (".yaml", ".yml") or f.name.startswith("."):
                    continue
                if f.name == "kyverno-test.yaml":
                    continue
                shutil.copy(f, cleanup_dir / f.name)

            test_file = test_dir / "kyverno-test.yaml"
            if not test_file.exists():
                for f in sorted(test_dir.iterdir()):
                    if f.suffix in (".yaml", ".yml") and f.name not in (
                        "resources.yaml",
                        "resource.yaml",
                    ):
                        test_file = f
                        break

            if test_file.exists():
                doc = yaml.safe_load(test_file.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    if policy_under_test is not None:
                        doc["policies"] = [str(policy_under_test.resolve())]
                    if "results" in doc:
                        is_new_kind = output_policy_kind in _NEW_POLICY_KINDS
                        seen: dict[tuple, dict] = {}
                        for r in doc["results"]:
                            if not isinstance(r, dict):
                                continue
                            if output_policy_name and "policy" in r:
                                r["policy"] = output_policy_name
                            if is_new_kind:
                                r.pop("rule", None)
                                # Merge duplicates by resource -- new policy
                                # types evaluate all validations as a group,
                                # so ANY fail = overall fail.
                                res = r.get("resources") or [None]
                                key = (r.get("kind"), r.get("policy"), res[0])
                                if key in seen:
                                    if r.get("result") == "fail":
                                        seen[key]["result"] = "fail"
                                else:
                                    seen[key] = r
                        if is_new_kind:
                            doc["results"] = list(seen.values())
                (cleanup_dir / "kyverno-test.yaml").write_text(
                    yaml.dump(doc, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )
                run_dir = cleanup_dir
        except Exception:
            if cleanup_dir and cleanup_dir.exists():
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            cleanup_dir = None

    try:
        proc = subprocess.run(
            ["kyverno", "test", str(run_dir)],
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        if proc.returncode == 0:
            return True, [], False

        raw = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        out = _strip_ansi(raw)

        if out and (
            "unknown field" in out or "Invalid value" in out
        ) and "failed to load" in out.lower():
            return (
                False,
                [
                    "Kyverno CLI 'test' command does not yet support "
                    "ValidatingPolicy 1.16+ schema (e.g. spec.admission, "
                    "spec.assertions). Use --skip-kyverno-test for now."
                ],
                True,
            )

        if not out:
            out = "kyverno test exited non-zero (no output)"
        return False, [out], False
    finally:
        if cleanup_dir and cleanup_dir.exists():
            shutil.rmtree(cleanup_dir, ignore_errors=True)
