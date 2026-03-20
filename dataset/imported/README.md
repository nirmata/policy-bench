# Imported Kyverno policy corpus

Policy YAML and Kyverno CLI test fixtures under `kyverno-policies/` and `kyverno-tests/` are **not committed** by default. They are produced from the official [**kyverno/policies**](https://github.com/kyverno/policies) repository using a pinned revision.

## One-time setup

```bash
pip install pyyaml
python3 scripts/sync_kyverno_policies.py
```

This downloads the source archive, extracts it under `dataset/.cache/`, and copies files into:

- `dataset/imported/kyverno-policies/<benchmark-id>.yaml`
- `dataset/imported/kyverno-tests/<benchmark-id>/` (where upstream ships `.kyverno-test`)

Metadata from the last sync: `upstream-meta.json` (ref / counts).

The curated list of upstream paths and benchmark IDs lives in `dataset/kyverno-upstream-manifest.yaml`.

## Offline / CI

Run the sync step in your pipeline before `python3 benchmark.py`, or vendor the contents of `dataset/imported/` and commit them if you want zero network access.
