"""Evaluation of AI-generated Kyverno Chainsaw test suites.

A generated suite is expected to include:
    chainsaw-test.yaml  - canonical Chainsaw entry manifest
    policy.yaml         - source policy copy placed by the benchmark harness

Evaluation layers:
    1. Schema: file existence + YAML parse checks
    2. Functional: `chainsaw test` exits zero for the generated scenarios
    3. Coverage: generated scenario count vs reference scenario count
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _count_scenarios(doc: object) -> int:
    """Count scenarios in a loose, format-tolerant way."""
    if isinstance(doc, dict):
        scenarios = doc.get("scenarios")
        if isinstance(scenarios, list):
            return len(scenarios)
        tests = doc.get("tests")
        if isinstance(tests, list):
            return len(tests)
        return 1
    if isinstance(doc, list):
        return len(doc)
    return 0


def evaluate_chainsawgen(
    *,
    generated_dir: Path,
    source_policy: Path,
    reference_dir: Path | None,
    timeout_sec: int = 120,
) -> dict:
    """Evaluate an AI-generated Chainsaw test suite.

    Returns result keys aligned with existing report fields and a dedicated
    `chainsaw_*` namespace, similar to test-generation benchmarking.
    """
    errors: list[str] = []
    test_manifest = generated_dir / "chainsaw-test.yaml"

    if not test_manifest.exists():
        return _failure_result(["Missing required file: chainsaw-test.yaml"])

    if not source_policy.exists():
        return _failure_result([f"Missing source policy in output dir: {source_policy}"])

    if yaml is None:
        return _failure_result(["PyYAML not installed; cannot validate chainsaw-test.yaml"])

    schema_pass = True
    parsed_doc: object = None
    manifest_text = ""
    try:
        manifest_text = test_manifest.read_text(encoding="utf-8")
        # Accept either a single Test document or a multi-doc YAML containing
        # multiple Test documents (a valid Chainsaw layout). For schema-level
        # introspection we keep `parsed_doc` as the first non-empty document;
        # `_count_scenarios` already understands multi-doc lists.
        all_docs = [d for d in yaml.safe_load_all(manifest_text) if d]
        if not all_docs:
            errors.append("chainsaw-test.yaml is empty")
            schema_pass = False
        else:
            parsed_doc = all_docs if len(all_docs) > 1 else all_docs[0]
    except Exception as exc:
        errors.append(f"Failed to parse chainsaw-test.yaml: {exc}")
        schema_pass = False

    if not schema_pass:
        return _failure_result(errors)

    lower_manifest = manifest_text.lower()
    has_pass_and_fail = "pass" in lower_manifest and "fail" in lower_manifest

    chainsaw_bin = shutil.which("chainsaw")
    test_pass = False
    test_skipped = True
    if chainsaw_bin:
        test_skipped = False
        test_pass, functional_errors = _run_functional_validation(
            chainsaw_bin=chainsaw_bin,
            generated_dir=generated_dir,
            timeout_sec=timeout_sec,
        )
        errors.extend(functional_errors)
    else:
        errors.append("chainsaw CLI not found; functional validation skipped")

    generated_scenarios = _count_scenarios(parsed_doc)
    reference_scenarios = 0
    if reference_dir and yaml is not None:
        reference_manifest = reference_dir / "chainsaw-test.yaml"
        if reference_manifest.exists():
            try:
                reference_doc = yaml.safe_load(reference_manifest.read_text(encoding="utf-8"))
                reference_scenarios = _count_scenarios(reference_doc)
            except Exception:
                pass

    coverage_score = (
        min(generated_scenarios / reference_scenarios, 1.0) if reference_scenarios > 0 else 0.0
    )

    composite = schema_pass and (test_pass if not test_skipped else False) and has_pass_and_fail

    return {
        "chainsaw_schema_pass": schema_pass,
        "chainsaw_test_pass": test_pass if not test_skipped else None,
        "chainsaw_test_skipped": test_skipped,
        "chainsaw_coverage_score": round(coverage_score, 4),
        "chainsaw_reference_scenarios": reference_scenarios,
        "chainsaw_generated_scenarios": generated_scenarios,
        "chainsaw_has_pass_and_fail": has_pass_and_fail,
        "chainsaw_composite_pass": composite,
        "chainsaw_errors": errors,
        # Mirrors for existing report rendering
        "schema_pass": schema_pass,
        "semantic_pass": test_pass if not test_skipped else None,
        "semantic_skipped": test_skipped,
        "semantic_errors": errors,
    }


def _failure_result(errors: list[str]) -> dict:
    return {
        "chainsaw_schema_pass": False,
        "chainsaw_test_pass": None,
        "chainsaw_test_skipped": True,
        "chainsaw_coverage_score": 0.0,
        "chainsaw_reference_scenarios": 0,
        "chainsaw_generated_scenarios": 0,
        "chainsaw_has_pass_and_fail": False,
        "chainsaw_composite_pass": False,
        "chainsaw_errors": errors,
        # Mirrors
        "schema_pass": False,
        "semantic_pass": None,
        "semantic_skipped": True,
        "semantic_errors": [],
    }


def _run_functional_validation(
    *,
    chainsaw_bin: str,
    generated_dir: Path,
    timeout_sec: int,
) -> tuple[bool, list[str]]:
    print(f"\n[chainsaw] Preflight: checking cluster for kyverno-managed webhooks...", flush=True)
    preflight_errors = _detect_cluster_conflicts()
    if preflight_errors:
        print(f"[chainsaw] Preflight FAILED:\n{preflight_errors[0]}", flush=True)
        return False, preflight_errors
    print(f"[chainsaw] Preflight OK", flush=True)

    pass_dir = generated_dir / "pass"
    fail_dir = generated_dir / "fail"

    # Running `chainsaw test` from the parent directory causes Chainsaw to
    # discover pass/ and fail/ suites together, which executes them in one run
    # and can make cluster-scoped Helm installs interfere with each other.
    # When both scenario directories exist, validate them serially instead.
    if pass_dir.is_dir() and fail_dir.is_dir():
        print(f"[chainsaw] Found pass/ and fail/ scenarios — running serially", flush=True)
        return _run_serial_scenarios(
            chainsaw_bin=chainsaw_bin,
            generated_dir=generated_dir,
            scenario_dirs=[pass_dir, fail_dir],
            timeout_sec=timeout_sec,
        )

    print(f"[chainsaw] Single-suite mode (no pass/ or fail/ subdirs)", flush=True)
    # If chainsaw-test.yaml contains multiple Test docs we MUST force serial
    # execution; otherwise concurrent helm installs into the same cluster
    # collide on shared CRDs ("ownership metadata" errors).
    extra_args: list[str] = []
    test_manifest = generated_dir / "chainsaw-test.yaml"
    if test_manifest.exists() and yaml is not None:
        try:
            docs = [
                d for d in yaml.safe_load_all(test_manifest.read_text(encoding="utf-8")) if d
            ]
            if len(docs) > 1:
                print(
                    f"[chainsaw] Multi-doc Test manifest detected ({len(docs)} docs) — forcing --parallel 1",
                    flush=True,
                )
                extra_args = ["--parallel", "1"]
        except Exception:
            pass
    # Pre-clean known benchmark state so a previous interrupted run can't
    # leave kyverno releases/CRDs that collide with this attempt.
    print(f"[chainsaw] Pre-cleaning known benchmark state...", flush=True)
    _cleanup_known_benchmark_state()
    _delete_kyverno_managed_webhooks()
    print(f"[chainsaw] Pre-clean done", flush=True)
    return _run_single_chainsaw_test(
        chainsaw_bin=chainsaw_bin,
        test_dir=generated_dir,
        cwd=generated_dir,
        timeout_sec=timeout_sec,
        label=str(generated_dir),
        extra_args=extra_args,
    )


def _detect_cluster_conflicts() -> list[str]:
    kubectl_bin = shutil.which("kubectl")
    if not kubectl_bin or yaml is None:
        return []

    # Query for webhooks managed by Kyverno
    try:
        proc = subprocess.run(
            [
                kubectl_bin,
                "get",
                "validatingwebhookconfigurations,mutatingwebhookconfigurations",
                "-l",
                "webhook.kyverno.io/managed-by=kyverno",
                "-o",
                "yaml",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    try:
        data = yaml.safe_load(proc.stdout) or {}
    except Exception:
        return []

    # Collect names and kind of webhooks to delete
    webhooks_to_delete: list[tuple[str, str]] = []  # (name, kind)
    for item in data.get("items", []):
        name = str(item.get("metadata", {}).get("name", ""))
        kind = str(item.get("kind", ""))
        if name and kind:
            webhooks_to_delete.append((name, kind))

    if not webhooks_to_delete:
        return []

    # Attempt to delete each webhook
    for name, kind in webhooks_to_delete:
        _run_best_effort(
            [kubectl_bin, "delete", kind.lower(), name, "--ignore-not-found=true"],
            timeout_sec=30,
        )

    # Verify all webhooks are gone
    try:
        proc = subprocess.run(
            [
                kubectl_bin,
                "get",
                "validatingwebhookconfigurations,mutatingwebhookconfigurations",
                "-l",
                "webhook.kyverno.io/managed-by=kyverno",
                "-o",
                "yaml",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    try:
        remaining_data = yaml.safe_load(proc.stdout) or {}
    except Exception:
        return []

    remaining = remaining_data.get("items", [])
    if remaining:
        conflicts = [str(item.get("metadata", {}).get("name", "")) for item in remaining]
        joined = "\n".join(f"- {name}" for name in conflicts[:10])
        if len(conflicts) > 10:
            joined += f"\n- ... and {len(conflicts) - 10} more"
        return [
            "Failed to delete existing Kyverno webhook configurations:\n"
            f"{joined}"
        ]

    return []


def _run_serial_scenarios(
    *,
    chainsaw_bin: str,
    generated_dir: Path,
    scenario_dirs: list[Path],
    timeout_sec: int,
) -> tuple[bool, list[str]]:
    remaining_timeout = max(timeout_sec, len(scenario_dirs))
    errors: list[str] = []

    print(f"[chainsaw] Cleaning up known benchmark state (kyverno-pass/kyverno-fail releases + webhooks)...", flush=True)
    _cleanup_known_benchmark_state()
    _delete_kyverno_managed_webhooks()
    print(f"[chainsaw] Cleanup done", flush=True)

    for index, scenario_dir in enumerate(scenario_dirs):
        suites_left = len(scenario_dirs) - index
        scenario_timeout = max(1, remaining_timeout // suites_left)

        if index > 0:
            print(f"[chainsaw] Inter-scenario cleanup before {scenario_dir.name}...", flush=True)
            _cleanup_known_benchmark_state()
            _delete_kyverno_managed_webhooks()
            print(f"[chainsaw] Inter-scenario cleanup done", flush=True)

        scenario_pass, scenario_errors = _run_single_chainsaw_test(
            chainsaw_bin=chainsaw_bin,
            test_dir=scenario_dir,
            cwd=generated_dir,
            timeout_sec=scenario_timeout,
            label=scenario_dir.name,
        )
        if not scenario_pass:
            errors.extend(scenario_errors)
            return False, errors
        remaining_timeout -= scenario_timeout

    return True, errors


def _delete_kyverno_managed_webhooks() -> None:
    kubectl_bin = shutil.which("kubectl")
    if not kubectl_bin:
        return
    _run_best_effort(
        [
            kubectl_bin, "delete",
            "validatingwebhookconfigurations,mutatingwebhookconfigurations",
            "-l", "webhook.kyverno.io/managed-by=kyverno",
            "--ignore-not-found=true",
        ],
        timeout_sec=30,
    )
    # Delete Kyverno CRDs to avoid "ownership metadata" conflicts on subsequent
    # Helm installs into a different release name. CRDs are cluster-scoped and
    # not removed by `helm uninstall` by default.
    try:
        proc = subprocess.run(
            [kubectl_bin, "get", "crds", "-o", "name"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return
    if proc.returncode != 0:
        return
    crd_names = [
        line.strip().removeprefix("customresourcedefinition.apiextensions.k8s.io/")
        for line in proc.stdout.splitlines()
        if line.strip().endswith((".kyverno.io", ".wgpolicyk8s.io"))
    ]
    if not crd_names:
        return
    _run_best_effort(
        [kubectl_bin, "delete", "crd", *crd_names, "--ignore-not-found=true", "--wait=true", "--timeout=60s"],
        timeout_sec=90,
    )


def _cleanup_known_benchmark_state() -> None:
    helm_bin = shutil.which("helm")
    kubectl_bin = shutil.which("kubectl")

    if helm_bin:
        for namespace, release in (("kyverno-pass", "kyverno-pass"), ("kyverno-fail", "kyverno-fail")):
            _run_best_effort(
                [helm_bin, "uninstall", release, "-n", namespace, "--ignore-not-found", "--no-hooks"],
                timeout_sec=60,
            )
            _wait_for_release_gone(
                helm_bin=helm_bin,
                release=release,
                namespace=namespace,
                timeout_sec=120,
            )

    if kubectl_bin:
        _run_best_effort(
            [kubectl_bin, "delete", "clusterpolicy", "require-resource-limits", "--ignore-not-found=true"],
            timeout_sec=30,
        )
        for namespace in ("kyverno-pass", "kyverno-fail"):
            _run_best_effort([kubectl_bin, "delete", "pods", "--all", "-n", namespace], timeout_sec=60)
            _wait_for_pods_gone(kubectl_bin=kubectl_bin, namespace=namespace, timeout_sec=90)


def _run_best_effort(command: list[str], *, timeout_sec: int) -> None:
    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def _wait_for_pods_gone(*, kubectl_bin: str, namespace: str, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                [kubectl_bin, "get", "pods", "-n", namespace, "--no-headers"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return

        if proc.returncode != 0:
            return

        if not proc.stdout.strip():
            return

        time.sleep(3)


def _wait_for_release_gone(*, helm_bin: str, release: str, namespace: str, timeout_sec: int) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                [helm_bin, "status", release, "-n", namespace],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return

        if proc.returncode != 0:
            return

        time.sleep(3)


def _run_single_chainsaw_test(
    *,
    chainsaw_bin: str,
    test_dir: Path,
    cwd: Path,
    timeout_sec: int,
    label: str,
    extra_args: list[str] | None = None,
) -> tuple[bool, list[str]]:
    import sys

    test_dir = test_dir.resolve()
    cwd = cwd.resolve()

    cmd = [chainsaw_bin, "test", *(extra_args or []), str(test_dir)]
    header = f"\n{'=' * 70}\n[chainsaw] Running: {label}\n[chainsaw] Dir:     {test_dir}\n[chainsaw] Cmd:     {' '.join(cmd)}\n[chainsaw] Timeout: {timeout_sec}s\n{'=' * 70}"
    print(header, flush=True)

    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        return False, [f"failed to start chainsaw for {label}: {exc}"]

    deadline = time.time() + timeout_sec
    timed_out = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            captured.append(line)
            if time.time() > deadline:
                timed_out = True
                proc.kill()
                break
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        timed_out = True

    if timed_out:
        return False, [f"chainsaw test timed out after {timeout_sec}s for {label}"]

    if proc.returncode == 0:
        print(f"[chainsaw] {label}: PASS", flush=True)
        return True, []

    out = "".join(captured).strip()
    if not out:
        out = f"chainsaw test exited non-zero with no output for {label}"
    elif label:
        out = f"[{label}]\n{out}"
    print(f"[chainsaw] {label}: FAIL (exit {proc.returncode})", flush=True)
    return False, [out]