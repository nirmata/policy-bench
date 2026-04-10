#!/usr/bin/env python3
"""Download Kyverno policies from kyverno/policies and install into dataset/imported/.

Uses a GitHub source archive tarball (no git required). Pins are read from
dataset/kyverno-upstream-manifest.yaml.

Usage:
  python3 scripts/sync_kyverno_policies.py
  python3 scripts/sync_kyverno_policies.py --ref abc1234   # override manifest ref
  python3 scripts/sync_kyverno_policies.py --dry-run

After sync, run the benchmark as usual; ClusterPolicy inputs resolve under
dataset/imported/kyverno-policies/.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Error: PyYAML required. pip install pyyaml", file=sys.stderr)
    sys.exit(1)


REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "dataset" / "kyverno-upstream-manifest.yaml"


def _load_manifest() -> dict:
    if not MANIFEST.exists():
        print(f"Error: manifest not found: {MANIFEST}", file=sys.stderr)
        sys.exit(1)
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def _download_extract(*, repo: str, ref: str, cache_dir: Path) -> Path:
    """Return path to extracted root folder (e.g. policies-<ref>)."""
    # "https://github.com/kyverno/policies.git" -> kyverno/policies
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = repo.rstrip("/").split("/")
    org_repo = "/".join(parts[-2:])  # kyverno/policies

    url = f"https://github.com/{org_repo}/archive/{ref}.tar.gz"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / f"{ref}.tar.gz"

    print(f"  Fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "policy-bench-sync"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        tarball.write_bytes(resp.read())

    extract_to = cache_dir / f"extract-{ref}"
    if extract_to.exists():
        shutil.rmtree(extract_to)
    extract_to.mkdir(parents=True)

    with tarfile.open(tarball, "r:gz") as tar:
        try:
            tar.extractall(extract_to, filter="data")  # type: ignore[call-arg]
        except TypeError:
            tar.extractall(extract_to)

    # First (and only) top-level directory inside archive
    children = list(extract_to.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        print(f"Error: unexpected archive layout under {extract_to}", file=sys.stderr)
        sys.exit(1)
    root = children[0]
    print(f"  Extracted to {root}")
    return root


def _copy_test_dir(*, src: Path, dest: Path) -> None:
    """Copy Kyverno CLI test assets (.kyverno-test → dest)."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dest / item.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0].strip())
    parser.add_argument("--ref", help="Override manifest ref (branch, tag, or full SHA)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions only")
    args = parser.parse_args()

    manifest = _load_manifest()
    repo = manifest.get("repository", "https://github.com/kyverno/policies.git")
    ref = args.ref or manifest.get("ref")
    if not ref:
        print("Error: manifest missing 'ref' and none passed via --ref", file=sys.stderr)
        return 1

    dest_policies = REPO_ROOT / manifest.get("dest_policies", "dataset/imported/kyverno-policies")
    dest_tests = REPO_ROOT / manifest.get("dest_tests", "dataset/imported/kyverno-tests")
    cache_dir = REPO_ROOT / manifest.get("cache_dir", "dataset/.cache/kyverno-policies-archive")
    policies_cfg: list = manifest.get("policies") or []

    if args.dry_run:
        print(f"Would sync {len(policies_cfg)} policies from {repo} @ {ref}")
        for p in policies_cfg:
            print(f"  - {p['id']}: {p['upstream_path']}")
        return 0

    src_root = _download_extract(repo=repo, ref=ref, cache_dir=Path(cache_dir))

    dest_policies.mkdir(parents=True, exist_ok=True)

    meta = {
        "repository": repo,
        "ref": ref,
        "upstream_root": str(src_root.name),
        "policy_count": len(policies_cfg),
        "tests_synced": 0,
    }

    for entry in policies_cfg:
        pid = entry["id"]
        rel = entry["upstream_path"]
        src_file = src_root / rel
        if not src_file.is_file():
            print(f"Error: missing upstream file: {src_file}", file=sys.stderr)
            return 1
        out = dest_policies / f"{pid}.yaml"
        shutil.copy2(src_file, out)
        print(f"  policy  {pid}  <=  {rel}")

        sync_test = entry.get("sync_test", False)
        if sync_test:
            test_src = src_file.parent / ".kyverno-test"
            if not test_src.is_dir():
                print(
                    f"Warning: sync_test true but no .kyverno-test for {pid} ({test_src})",
                    file=sys.stderr,
                )
                continue
            test_dest = dest_tests / pid
            if test_dest.exists():
                shutil.rmtree(test_dest)
            _copy_test_dir(src=test_src, dest=test_dest)
            meta["tests_synced"] += 1
            print(f"  test    {pid}  <=  {Path(rel).parent}/.kyverno-test")

    meta_path = REPO_ROOT / "dataset" / "imported" / "upstream-meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nDone. Wrote {meta_path.relative_to(REPO_ROOT)}")
    print(f"Policies: {dest_policies.relative_to(REPO_ROOT)} ({len(policies_cfg)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
