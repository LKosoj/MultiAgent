from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "verify_text2sql_public_benchmark_baseline.py"
)
_SPEC = importlib.util.spec_from_file_location("benchmark_baseline_verifier", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
verifier = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verifier)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _symlink_to(link: Path, target: Path | str) -> None:
    try:
        link.symlink_to(target)
    except NotImplementedError:
        pytest.skip("platform does not support symlinks")
    except OSError as exc:
        if exc.errno in {errno.ENOSYS, errno.EOPNOTSUPP}:
            pytest.skip("filesystem does not support symlinks")
        raise


def _evidence_entry(root: Path, path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(payload),
        "bytes": len(payload),
    }


def _observation(
    ordinal: int,
    *,
    benchmark: str,
    workflow_status: str,
    outcome_status: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "benchmark": benchmark,
        "case_key": f"{benchmark}:{ordinal}",
        "case_id": f"case-{ordinal}",
        "ordinal": ordinal,
        "observation_status": "completed",
        "workflow_status": workflow_status,
        "outcome": {"status": outcome_status},
    }


def _bundle(root: Path, bundle_id: str, benchmark: str) -> dict[str, object]:
    bundle_root = root / bundle_id
    revision = "a" * 40
    source_digest = {"sha256": "b" * 64, "size_bytes": 42}
    source_manifest_entry = {"path": "/historical/source.json", **source_digest}
    config_digest = {
        "path": "workflow_pipelines/text_to_sql_pipeline.yaml",
        "sha256": "c" * 64,
        "size_bytes": 84,
    }
    manifest = {
        "base_url": "http://127.0.0.1:8810",
        "schema_version": 1,
        "benchmark": benchmark,
        "case_count": 2,
        "completed_before": 2,
        "created_at": "2026-07-30T00:00:00+00:00",
        "max_rows": 100,
        "model_configuration": {
            "note": "historical fixture",
            "reported_by_runtime": False,
        },
        "principal": {
            "roles": ["admin"],
            "subject": "text2sql-benchmark",
            "tenant_id": "text2sql-benchmark",
        },
        "repo_revision": revision,
        "pipeline_revision": revision,
        "workers": 1,
        "case_timeout": 900,
        "successful_sql_memory_enabled": "0",
        "sources": [source_manifest_entry],
        "configuration_sources": [config_digest],
    }
    _write_json(bundle_root / "manifest.json", manifest)
    rows = [
        _observation(0, benchmark=benchmark, workflow_status="finished", outcome_status="succeeded"),
        _observation(1, benchmark=benchmark, workflow_status="errored", outcome_status="failed"),
    ]
    observations = bundle_root / "observations.jsonl"
    observations.parent.mkdir(parents=True, exist_ok=True)
    observations.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    (bundle_root / "official_eval.log").write_text("official\n", encoding="utf-8")
    _write_json(bundle_root / "analysis" / "scores.json", {"score": 1})
    (bundle_root / "analysis" / "diagnostics.jsonl").write_text("{}\n", encoding="utf-8")
    _write_json(bundle_root / "analysis" / "summary.json", {"cases": 2})
    runtime = bundle_root / "runtime_evidence"
    runtime.mkdir(parents=True)
    (runtime / "worker.log").write_text("worker\n", encoding="utf-8")
    runtime_count, runtime_digest = verifier._runtime_tree_digest(runtime, bundle_id)
    evidence_paths = {
        "manifest": bundle_root / "manifest.json",
        "observations": observations,
        "official_evaluator_log": bundle_root / "official_eval.log",
        "official_scores": bundle_root / "analysis" / "scores.json",
        "diagnostics": bundle_root / "analysis" / "diagnostics.jsonl",
        "summary": bundle_root / "analysis" / "summary.json",
    }
    return {
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "case_count": 2,
        "evidence": {
            name: _evidence_entry(root, path)
            for name, path in evidence_paths.items()
        },
        "runtime_tree": {
            "path": runtime.relative_to(root).as_posix(),
            "sha256": runtime_digest,
            "file_count": runtime_count,
        },
        "manifest_contract": {
            "repo_revision": revision,
            "pipeline_revision": revision,
            "source_digests": [source_digest],
            "configuration_digests": [config_digest],
        },
        "observations_contract": {
            "schema_version": 1,
            "observation_status_counts": {"completed": 2},
            "workflow_status_counts": {"errored": 1, "finished": 1},
            "outcome_status_counts": {"failed": 1, "succeeded": 1},
        },
    }


@pytest.fixture
def frozen_baseline(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    registry = {
        "schema_version": 1,
        "baseline_id": "fixture-baseline",
        "report_revision": "a" * 40,
        "protocol": {
            "workers": 1,
            "case_timeout_seconds": 900,
            "sql_memory_disabled": True,
            "runtime_identity": {"status": "unavailable_from_gateway"},
        },
        "bundles": [
            _bundle(artifact_root, "bird-after-fix", "bird"),
            _bundle(artifact_root, "spider-after-fix", "spider"),
        ],
    }
    baseline = tmp_path / "baseline.json"
    _write_json(baseline, registry)
    return baseline, artifact_root, registry


def _rewrite_registry(path: Path, registry: dict[str, object]) -> None:
    _write_json(path, registry)


def _refresh_evidence(root: Path, registry: dict[str, object], name: str) -> None:
    bundle = registry["bundles"][0]
    entry = bundle["evidence"][name]
    path = root / entry["path"]
    bundle["evidence"][name] = _evidence_entry(root, path)


def test_verifies_complete_historical_baseline(frozen_baseline):
    baseline, artifact_root, _registry = frozen_baseline

    assert verifier.verify_baseline(baseline, artifact_root) is None
    assert verifier.main(["--baseline", str(baseline), "--artifact-root", str(artifact_root)]) == 0


def test_rejects_missing_evidence(frozen_baseline):
    baseline, artifact_root, _registry = frozen_baseline
    (artifact_root / "bird-after-fix" / "analysis" / "summary.json").unlink()

    with pytest.raises(verifier.BaselineVerificationError, match="missing artifact"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_missing_or_unexpected_registry_evidence(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    evidence = registry["bundles"][0]["evidence"]
    del evidence["summary"]
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="missing or extra evidence entries"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_unexpected_registry_evidence(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    evidence = registry["bundles"][0]["evidence"]
    evidence["unexpected"] = evidence["summary"]
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="missing or extra evidence entries"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_unexpected_manifest_fields(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    manifest_path = artifact_root / "bird-after-fix" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = True
    _write_json(manifest_path, manifest)
    _refresh_evidence(artifact_root, registry, "manifest")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="manifest: unexpected fields"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_corrupt_evidence_even_when_registry_digest_is_updated(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    manifest = artifact_root / "bird-after-fix" / "manifest.json"
    manifest.write_text("{", encoding="utf-8")
    _refresh_evidence(artifact_root, registry, "manifest")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="invalid JSON"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_extra_manifest_digest_fields(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    manifest_path = artifact_root / "bird-after-fix" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0]["unexpected"] = True
    _write_json(manifest_path, manifest)
    _refresh_evidence(artifact_root, registry, "manifest")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="unexpected digest fields"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_extra_runtime_artifact(frozen_baseline):
    baseline, artifact_root, _registry = frozen_baseline
    (artifact_root / "bird-after-fix" / "runtime_evidence" / "unexpected.log").write_text(
        "unexpected\n", encoding="utf-8"
    )

    with pytest.raises(verifier.BaselineVerificationError, match="runtime file count mismatch"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_runtime_tree_content_mutation(frozen_baseline):
    baseline, artifact_root, _registry = frozen_baseline
    (artifact_root / "bird-after-fix" / "runtime_evidence" / "worker.log").write_text(
        "modified\n",
        encoding="utf-8",
    )

    with pytest.raises(verifier.BaselineVerificationError, match="runtime tree digest mismatch"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_escaping_registry_path(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    registry["bundles"][0]["evidence"]["manifest"]["path"] = "../escape.json"
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="non-escaping relative path"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_symlinked_registry_artifact(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    summary = artifact_root / "bird-after-fix" / "analysis" / "summary.json"
    replacement = summary.with_name("summary-target.json")
    summary.rename(replacement)
    _symlink_to(summary, replacement.name)
    _refresh_evidence(artifact_root, registry, "summary")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="symlinks are not allowed"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_nested_runtime_tree_symlink(frozen_baseline):
    baseline, artifact_root, _registry = frozen_baseline
    runtime = artifact_root / "bird-after-fix" / "runtime_evidence"
    nested = runtime / "nested"
    nested.mkdir()
    _symlink_to(nested / "worker-link.log", runtime / "worker.log")

    with pytest.raises(verifier.BaselineVerificationError, match="symlinks are not allowed"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_truncated_observations(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    observations = artifact_root / "bird-after-fix" / "observations.jsonl"
    observations.write_bytes(observations.read_bytes().rstrip(b"\n"))
    _refresh_evidence(artifact_root, registry, "observations")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="truncated JSONL"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_duplicate_case_evidence(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    observations = artifact_root / "bird-after-fix" / "observations.jsonl"
    first, _second = observations.read_text(encoding="utf-8").splitlines()
    observations.write_text(f"{first}\n{first}\n", encoding="utf-8")
    _refresh_evidence(artifact_root, registry, "observations")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="duplicate case_key"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_partial_case_evidence(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    observations = artifact_root / "bird-after-fix" / "observations.jsonl"
    first = observations.read_text(encoding="utf-8").splitlines()[0]
    observations.write_text(f"{first}\n", encoding="utf-8")
    _refresh_evidence(artifact_root, registry, "observations")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="partial case evidence"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_outcome_without_status(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    observations = artifact_root / "bird-after-fix" / "observations.jsonl"
    rows = [json.loads(line) for line in observations.read_text(encoding="utf-8").splitlines()]
    rows[0]["outcome"] = {}
    observations.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    _refresh_evidence(artifact_root, registry, "observations")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match=r"outcome\.status: expected"):
        verifier.verify_baseline(baseline, artifact_root)


def test_rejects_status_count_mismatch(frozen_baseline):
    baseline, artifact_root, registry = frozen_baseline
    observations = artifact_root / "bird-after-fix" / "observations.jsonl"
    rows = [json.loads(line) for line in observations.read_text(encoding="utf-8").splitlines()]
    rows[0]["workflow_status"] = "errored"
    observations.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    _refresh_evidence(artifact_root, registry, "observations")
    _rewrite_registry(baseline, registry)

    with pytest.raises(verifier.BaselineVerificationError, match="workflow_status counts mismatch"):
        verifier.verify_baseline(baseline, artifact_root)
