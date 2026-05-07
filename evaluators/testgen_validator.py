"""Evaluation of AI-generated Kyverno CLI test suites.

A test suite is a directory containing:
  kyverno-test.yaml  — Test manifest (apiVersion: cli.kyverno.io/v1alpha1)
  resources.yaml     — Resource manifests referenced by the test
  policy.yaml        — Copy of the source policy (placed by the harness)

Three evaluation layers:
  1. Schema: files exist, YAML parses, required fields present
  2. Functional: kyverno test exits zero
  3. Coverage: generated vs oracle tuple counts, has_pass_and_fail
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from .semantic_validator import run_kyverno_test

_EXPECTED_API_VERSION = "cli.kyverno.io/v1alpha1"
_EXPECTED_KIND = "Test"
_REQUIRED_FIELDS = ("policies", "resources", "results")


def _count_tuples(results: list[dict]) -> int:
    """Count individual (resource_name, result) pairs across all result entries."""
    total = 0
    for entry in results:
        resources = entry.get("resources")
        total += len(resources) if resources else 1
    return total


def _has_pass_and_fail(results: list[dict]) -> bool:
    outcomes = {str(r.get("result", "")).lower() for r in results}
    return "pass" in outcomes and "fail" in outcomes


def _is_non_validate_policy(source_policy: Path) -> bool:
    """Return True if every rule in the policy uses mutate or generate (not validate).

    Mutate and generate policies produce only pass+skip results — there is no
    fail case — so the has_pass_and_fail composite gate should not apply.
    """
    if yaml is None or not source_policy.exists():
        return False
    try:
        doc = yaml.safe_load(source_policy.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return False
        rules = (doc.get("spec") or {}).get("rules") or []
        if not rules:
            return False
        return all("mutate" in r or "generate" in r for r in rules)
    except Exception:
        pass
    return False


def _extract_not_found_resources(kt_errors: list[str]) -> set[str]:
    """Parse kyverno test output and return resource names that got 'Not found'."""
    import re
    not_found: set[str] = set()
    for err in kt_errors:
        for line in err.splitlines():
            if "Not found" in line:
                # Lines look like: │ ... │ apps/v1/Deployment/default/my-resource │ Fail │ Not found │
                parts = [p.strip() for p in line.split("│") if p.strip()]
                if len(parts) >= 4:
                    resource_path = parts[3] if len(parts) >= 5 else parts[2]
                    name = resource_path.rsplit("/", 1)[-1]
                    not_found.add(name)
    return not_found


def _load_policy_meta(source_policy: Path) -> tuple[str | None, str]:
    """Return (metadata.name, kind) from a policy YAML, or (None, '') on failure."""
    if yaml is None or not source_policy.exists():
        return None, ""
    try:
        doc = yaml.safe_load(source_policy.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return None, ""
        return (doc.get("metadata") or {}).get("name"), doc.get("kind", "")
    except Exception:
        return None, ""


def evaluate_testgen(
    *,
    generated_dir: Path,
    source_policy: Path,
    oracle_dir: Path | None,
    timeout_sec: int = 60,
) -> dict:
    """Evaluate an AI-generated Kyverno CLI test suite.

    Returns a dict with keys:
      schema_pass, semantic_pass, semantic_skipped     — mirrors for existing reports
      testgen_schema_pass, testgen_kyverno_test_pass,
      testgen_kyverno_test_skipped, testgen_coverage_score,
      testgen_oracle_tuples, testgen_generated_tuples,
      testgen_has_pass_and_fail, testgen_composite_pass, testgen_errors
    """
    errors: list[str] = []

    # --- 1. Schema: file existence + YAML structure ---
    test_manifest_path = generated_dir / "kyverno-test.yaml"
    resources_path = generated_dir / "resources.yaml"

    missing = [f.name for f in (test_manifest_path, resources_path) if not f.exists()]
    if missing:
        errors.append(f"Missing required files: {', '.join(missing)}")
        return _failure_result(errors)

    if yaml is None:
        errors.append("PyYAML not installed; cannot validate test manifest")
        return _failure_result(errors)

    schema_pass = True
    parsed_manifest: dict | None = None

    try:
        parsed_manifest = yaml.safe_load(test_manifest_path.read_text(encoding="utf-8"))
        if not isinstance(parsed_manifest, dict):
            errors.append("kyverno-test.yaml is not a YAML mapping")
            schema_pass = False
        else:
            actual_api = parsed_manifest.get("apiVersion")
            if actual_api != _EXPECTED_API_VERSION:
                errors.append(
                    f"kyverno-test.yaml apiVersion must be {_EXPECTED_API_VERSION!r},"
                    f" got {actual_api!r}"
                )
                schema_pass = False
            actual_kind = parsed_manifest.get("kind")
            if actual_kind != _EXPECTED_KIND:
                errors.append(
                    f"kyverno-test.yaml kind must be {_EXPECTED_KIND!r},"
                    f" got {actual_kind!r}"
                )
                schema_pass = False
            for field in _REQUIRED_FIELDS:
                if not parsed_manifest.get(field):
                    errors.append(f"kyverno-test.yaml missing required field: {field!r}")
                    schema_pass = False
    except Exception as exc:
        errors.append(f"Failed to parse kyverno-test.yaml: {exc}")
        schema_pass = False

    resource_names: set[str] = set()
    try:
        docs = list(yaml.safe_load_all(resources_path.read_text(encoding="utf-8")))
        for doc in docs:
            if isinstance(doc, dict):
                meta = doc.get("metadata") or {}
                name = meta.get("name", "")
                ns = meta.get("namespace", "")
                if name:
                    resource_names.add(name)
                    if ns:
                        resource_names.add(f"{ns}/{name}")
    except Exception as exc:
        errors.append(f"Failed to parse resources.yaml: {exc}")
        schema_pass = False

    if not schema_pass:
        return _failure_result(errors)

    # --- 1b. Name consistency: every name in results must exist in resources.yaml ---
    if parsed_manifest and resource_names:
        referenced: list[str] = []
        for entry in (parsed_manifest.get("results") or []):
            referenced.extend(entry.get("resources") or [])
        missing_names = [r for r in referenced if r not in resource_names]
        if missing_names:
            errors.append(
                f"Resource name mismatch: {missing_names} referenced in kyverno-test.yaml "
                f"but not found in resources.yaml. "
                f"Available resource names: {sorted(resource_names)}"
            )
            schema_pass = False

    if not schema_pass:
        return _failure_result(errors)

    # --- 2. Functional: kyverno test ---
    policy_name, policy_kind = _load_policy_meta(source_policy)
    kt_passed, kt_errors, kt_skipped = run_kyverno_test(
        generated_dir,
        output_policy_name=policy_name,
        output_policy_kind=policy_kind,
        policy_under_test=source_policy,
        timeout_sec=timeout_sec,
    )

    # For generate/mutate policies, the Kyverno CLI emits no result for
    # non-matching resources ("Not found"). Rather than ignoring failures,
    # strip those entries from the manifest and re-run — so real mismatches
    # on matching resources are still caught.
    if not kt_passed and not kt_skipped and _is_non_validate_policy(source_policy):
        not_found = _extract_not_found_resources(kt_errors)
        if not_found and parsed_manifest:
            import shutil as _shutil
            import tempfile as _tempfile
            cleaned_manifest = dict(parsed_manifest)
            cleaned_manifest["results"] = [
                r for r in (cleaned_manifest.get("results") or [])
                if not any(res in not_found for res in (r.get("resources") or []))
            ]
            if cleaned_manifest["results"]:
                cleaned_dir = Path(_tempfile.mkdtemp(prefix="kyverno_testgen_clean_"))
                try:
                    for f in generated_dir.iterdir():
                        if f.is_file():
                            _shutil.copy(f, cleaned_dir / f.name)
                    (cleaned_dir / "kyverno-test.yaml").write_text(
                        yaml.dump(cleaned_manifest, default_flow_style=False, sort_keys=False),
                        encoding="utf-8",
                    )
                    kt_passed, kt_errors, kt_skipped = run_kyverno_test(
                        cleaned_dir,
                        output_policy_name=policy_name,
                        output_policy_kind=policy_kind,
                        policy_under_test=source_policy,
                        timeout_sec=timeout_sec,
                    )
                finally:
                    _shutil.rmtree(cleaned_dir, ignore_errors=True)

    # --- 3. Coverage ---
    generated_results: list[dict] = (parsed_manifest or {}).get("results") or []
    generated_tuples = _count_tuples(generated_results)
    has_p_and_f = _has_pass_and_fail(generated_results)

    oracle_tuples = 0
    if oracle_dir and yaml is not None:
        oracle_manifest = oracle_dir / "kyverno-test.yaml"
        if oracle_manifest.exists():
            try:
                oracle_doc = yaml.safe_load(oracle_manifest.read_text(encoding="utf-8"))
                if isinstance(oracle_doc, dict):
                    oracle_tuples = _count_tuples(oracle_doc.get("results") or [])
            except Exception:
                pass

    coverage_score = (
        min(generated_tuples / oracle_tuples, 1.0) if oracle_tuples > 0 else 0.0
    )

    # Composite requires structural validity, a runnable suite, and scenario diversity.
    # Coverage score is reported but not gated — the oracle represents one author's
    # choices, not a minimum bar.
    # GeneratingPolicy tests correctly produce only result: pass entries (no fail),
    # so the has_pass_and_fail gate is relaxed for them.
    non_validate = _is_non_validate_policy(source_policy)
    composite = schema_pass and (kt_passed if not kt_skipped else False) and (has_p_and_f or non_validate)

    return {
        "testgen_schema_pass": schema_pass,
        "testgen_kyverno_test_pass": kt_passed if not kt_skipped else None,
        "testgen_kyverno_test_skipped": kt_skipped,
        "testgen_coverage_score": round(coverage_score, 4),
        "testgen_oracle_tuples": oracle_tuples,
        "testgen_generated_tuples": generated_tuples,
        "testgen_has_pass_and_fail": has_p_and_f,
        "testgen_composite_pass": composite,
        "testgen_errors": errors + kt_errors,
        # Mirrors for existing report rendering
        "schema_pass": schema_pass,
        "semantic_pass": kt_passed if not kt_skipped else None,
        "semantic_skipped": kt_skipped,
        "semantic_errors": kt_errors,
    }


def _failure_result(errors: list[str]) -> dict:
    return {
        "testgen_schema_pass": False,
        "testgen_kyverno_test_pass": None,
        "testgen_kyverno_test_skipped": True,
        "testgen_coverage_score": 0.0,
        "testgen_oracle_tuples": 0,
        "testgen_generated_tuples": 0,
        "testgen_has_pass_and_fail": False,
        "testgen_composite_pass": False,
        "testgen_errors": errors,
        # Mirrors
        "schema_pass": False,
        "semantic_pass": None,
        "semantic_skipped": True,
        "semantic_errors": [],
    }
