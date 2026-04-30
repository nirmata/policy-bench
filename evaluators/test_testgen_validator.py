"""Unit tests for testgen_validator.evaluate_testgen()."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluators.testgen_validator import (
    _count_tuples,
    _has_pass_and_fail,
    evaluate_testgen,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_MANIFEST = textwrap.dedent("""\
    apiVersion: cli.kyverno.io/v1alpha1
    kind: Test
    metadata:
      name: require-labels
    policies:
    - policy.yaml
    resources:
    - resources.yaml
    results:
    - kind: Pod
      policy: require-labels
      resources:
      - goodpod
      result: pass
      rule: check-for-labels
    - kind: Pod
      policy: require-labels
      resources:
      - badpod
      result: fail
      rule: check-for-labels
""")

_VALID_RESOURCES = textwrap.dedent("""\
    apiVersion: v1
    kind: Pod
    metadata:
      name: goodpod
""")

_SOURCE_POLICY = textwrap.dedent("""\
    apiVersion: kyverno.io/v1
    kind: ClusterPolicy
    metadata:
      name: require-labels
""")


def _write_suite(tmp_path: Path, manifest: str = _VALID_MANIFEST, resources: str = _VALID_RESOURCES) -> Path:
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "kyverno-test.yaml").write_text(manifest)
    (suite_dir / "resources.yaml").write_text(resources)
    return suite_dir


def _write_source(tmp_path: Path, content: str = _SOURCE_POLICY) -> Path:
    p = tmp_path / "policy.yaml"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# _count_tuples
# ---------------------------------------------------------------------------

def test_count_tuples_multi_resource():
    results = [
        {"resources": ["a", "b"], "result": "fail"},
        {"resources": ["c"], "result": "pass"},
    ]
    assert _count_tuples(results) == 3


def test_count_tuples_no_resources_field():
    # An entry with no resources list counts as 1.
    assert _count_tuples([{"result": "pass"}]) == 1


def test_count_tuples_empty():
    assert _count_tuples([]) == 0


# ---------------------------------------------------------------------------
# _has_pass_and_fail
# ---------------------------------------------------------------------------

def test_has_pass_and_fail_true():
    results = [{"result": "pass"}, {"result": "fail"}]
    assert _has_pass_and_fail(results) is True


def test_has_pass_and_fail_pass_only():
    assert _has_pass_and_fail([{"result": "pass"}]) is False


def test_has_pass_and_fail_skip_doesnt_count():
    results = [{"result": "pass"}, {"result": "skip"}]
    assert _has_pass_and_fail(results) is False


# ---------------------------------------------------------------------------
# evaluate_testgen: missing files
# ---------------------------------------------------------------------------

def test_missing_kyverno_test_yaml(tmp_path):
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "resources.yaml").write_text(_VALID_RESOURCES)
    source = _write_source(tmp_path)
    result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_schema_pass"] is False
    assert result["testgen_composite_pass"] is False
    assert any("kyverno-test.yaml" in e for e in result["testgen_errors"])


def test_missing_resources_yaml(tmp_path):
    suite_dir = tmp_path / "suite"
    suite_dir.mkdir()
    (suite_dir / "kyverno-test.yaml").write_text(_VALID_MANIFEST)
    source = _write_source(tmp_path)
    result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_schema_pass"] is False
    assert any("resources.yaml" in e for e in result["testgen_errors"])


# ---------------------------------------------------------------------------
# evaluate_testgen: schema failures
# ---------------------------------------------------------------------------

def test_wrong_api_version(tmp_path):
    bad_manifest = _VALID_MANIFEST.replace(
        "cli.kyverno.io/v1alpha1", "cli.kyverno.io/v1beta1"
    )
    suite_dir = _write_suite(tmp_path, manifest=bad_manifest)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_schema_pass"] is False
    assert result["testgen_composite_pass"] is False


def test_missing_results_field(tmp_path):
    bad_manifest = textwrap.dedent("""\
        apiVersion: cli.kyverno.io/v1alpha1
        kind: Test
        metadata:
          name: x
        policies:
        - policy.yaml
        resources:
        - resources.yaml
    """)
    suite_dir = _write_suite(tmp_path, manifest=bad_manifest)
    source = _write_source(tmp_path)
    result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_schema_pass"] is False
    assert any("results" in e for e in result["testgen_errors"])


# ---------------------------------------------------------------------------
# evaluate_testgen: functional failures
# ---------------------------------------------------------------------------

def test_kyverno_test_fails(tmp_path):
    suite_dir = _write_suite(tmp_path)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (False, ["assertion failed"], False)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_schema_pass"] is True
    assert result["testgen_kyverno_test_pass"] is False
    assert result["testgen_composite_pass"] is False
    assert "assertion failed" in result["testgen_errors"]


def test_kyverno_test_skipped(tmp_path):
    suite_dir = _write_suite(tmp_path)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (False, [], True)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_kyverno_test_skipped"] is True
    assert result["testgen_kyverno_test_pass"] is None
    # Skipped kyverno test → composite fails (no evidence it ran correctly)
    assert result["testgen_composite_pass"] is False


# ---------------------------------------------------------------------------
# evaluate_testgen: coverage computation
# ---------------------------------------------------------------------------

def test_coverage_score_with_oracle(tmp_path):
    suite_dir = _write_suite(tmp_path)
    source = _write_source(tmp_path)

    # Oracle has 4 tuples (2 entries × 2 resources each)
    oracle_dir = tmp_path / "oracle"
    oracle_dir.mkdir()
    oracle_manifest = textwrap.dedent("""\
        apiVersion: cli.kyverno.io/v1alpha1
        kind: Test
        metadata:
          name: require-labels
        policies:
        - policy.yaml
        resources:
        - resources.yaml
        results:
        - kind: Pod
          policy: require-labels
          resources: [a, b]
          result: pass
          rule: check-for-labels
        - kind: Pod
          policy: require-labels
          resources: [c, d]
          result: fail
          rule: check-for-labels
    """)
    (oracle_dir / "kyverno-test.yaml").write_text(oracle_manifest)

    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(
            generated_dir=suite_dir,
            source_policy=source,
            oracle_dir=oracle_dir,
        )

    # Generated suite has 2 tuples (1 resource each), oracle has 4 → 0.5
    assert result["testgen_oracle_tuples"] == 4
    assert result["testgen_generated_tuples"] == 2
    assert result["testgen_coverage_score"] == 0.5


def test_coverage_score_capped_at_1(tmp_path):
    """Generated suite covering MORE than the oracle still scores 1.0."""
    # Build a generated suite with 3 tuples
    big_manifest = textwrap.dedent("""\
        apiVersion: cli.kyverno.io/v1alpha1
        kind: Test
        metadata:
          name: require-labels
        policies:
        - policy.yaml
        resources:
        - resources.yaml
        results:
        - kind: Pod
          policy: require-labels
          resources: [a, b, c]
          result: pass
          rule: check-for-labels
        - kind: Pod
          policy: require-labels
          resources: [d]
          result: fail
          rule: check-for-labels
    """)
    suite_dir = _write_suite(tmp_path, manifest=big_manifest)
    source = _write_source(tmp_path)

    oracle_dir = tmp_path / "oracle"
    oracle_dir.mkdir()
    (oracle_dir / "kyverno-test.yaml").write_text(textwrap.dedent("""\
        apiVersion: cli.kyverno.io/v1alpha1
        kind: Test
        metadata:
          name: x
        policies: [p]
        resources: [r]
        results:
        - resources: [x]
          result: pass
        - resources: [y]
          result: fail
    """))

    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(
            generated_dir=suite_dir, source_policy=source, oracle_dir=oracle_dir
        )

    assert result["testgen_coverage_score"] == 1.0


def test_no_oracle_coverage_zero(tmp_path):
    suite_dir = _write_suite(tmp_path)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_coverage_score"] == 0.0
    assert result["testgen_oracle_tuples"] == 0


# ---------------------------------------------------------------------------
# evaluate_testgen: composite formula
# ---------------------------------------------------------------------------

def test_composite_pass_all_three_signals(tmp_path):
    suite_dir = _write_suite(tmp_path)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    # Valid schema + kyverno test passes + has pass and fail → composite True
    assert result["testgen_schema_pass"] is True
    assert result["testgen_kyverno_test_pass"] is True
    assert result["testgen_has_pass_and_fail"] is True
    assert result["testgen_composite_pass"] is True


def test_composite_fails_without_pass_and_fail(tmp_path):
    """A suite with only passing cases should not earn composite_pass."""
    pass_only_manifest = textwrap.dedent("""\
        apiVersion: cli.kyverno.io/v1alpha1
        kind: Test
        metadata:
          name: x
        policies:
        - policy.yaml
        resources:
        - resources.yaml
        results:
        - kind: Pod
          policy: x
          resources: [goodpod]
          result: pass
          rule: some-rule
    """)
    suite_dir = _write_suite(tmp_path, manifest=pass_only_manifest)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    assert result["testgen_has_pass_and_fail"] is False
    assert result["testgen_composite_pass"] is False


# ---------------------------------------------------------------------------
# evaluate_testgen: mirror keys for existing reports
# ---------------------------------------------------------------------------

def test_mirror_keys_present(tmp_path):
    suite_dir = _write_suite(tmp_path)
    source = _write_source(tmp_path)
    with patch("evaluators.testgen_validator.run_kyverno_test") as mock_kt:
        mock_kt.return_value = (True, [], False)
        result = evaluate_testgen(generated_dir=suite_dir, source_policy=source, oracle_dir=None)
    for key in ("schema_pass", "semantic_pass", "semantic_skipped", "semantic_errors"):
        assert key in result, f"mirror key {key!r} missing from result"
