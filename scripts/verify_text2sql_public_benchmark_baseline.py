#!/usr/bin/env python3
"""Verify frozen historical Text-to-SQL public benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Iterable, Mapping


class BaselineVerificationError(ValueError):
    """Historical benchmark evidence does not match its frozen registry."""


_EVIDENCE_KEYS = frozenset({
    "manifest",
    "observations",
    "official_evaluator_log",
    "official_scores",
    "diagnostics",
    "summary",
})
_BUNDLE_KEYS = frozenset({
    "bundle_id",
    "benchmark",
    "case_count",
    "evidence",
    "runtime_tree",
    "manifest_contract",
    "observations_contract",
})
_MANIFEST_KEYS = frozenset({
    "base_url",
    "benchmark",
    "case_count",
    "case_timeout",
    "completed_before",
    "configuration_sources",
    "created_at",
    "max_rows",
    "model_configuration",
    "pipeline_revision",
    "principal",
    "repo_revision",
    "schema_version",
    "sources",
    "successful_sql_memory_enabled",
    "workers",
})


def _fail(message: str) -> None:
    raise BaselineVerificationError(message)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"{label}: invalid JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label}: expected a JSON object")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label}: expected an object")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label}: expected a non-empty string")
    return value


def _require_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        _fail(f"{label}: expected a non-negative integer")
    return value


def _require_sha256(value: Any, label: str) -> str:
    text = _require_text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        _fail(f"{label}: expected a lowercase SHA-256 digest")
    return text


def _relative_path(value: Any, label: str) -> PurePosixPath:
    text = _require_text(value, label)
    if "\\" in text:
        _fail(f"{label}: backslashes are not allowed")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        _fail(f"{label}: must be a non-escaping relative path")
    return path


def _resolve_artifact(root: Path, relative: PurePosixPath, label: str) -> Path:
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                _fail(f"{label}: symlinks are not allowed")
        except OSError as exc:
            _fail(f"{label}: cannot inspect artifact path: {exc}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        _fail(f"{label}: missing artifact: {exc}")
    if not resolved.is_relative_to(root):
        _fail(f"{label}: artifact escapes artifact root")
    return resolved


def _verify_evidence_file(root: Path, entry: Any, label: str) -> Path:
    mapping = _require_mapping(entry, label)
    if set(mapping) != {"path", "sha256", "bytes"}:
        _fail(f"{label}: unexpected evidence fields")
    relative = _relative_path(mapping["path"], f"{label}.path")
    expected_digest = _require_sha256(mapping["sha256"], f"{label}.sha256")
    expected_bytes = _require_int(mapping["bytes"], f"{label}.bytes")
    path = _resolve_artifact(root, relative, label)
    if not path.is_file():
        _fail(f"{label}: expected a regular file")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"{label}: cannot read artifact: {exc}")
    if len(payload) != expected_bytes:
        _fail(f"{label}: byte count mismatch")
    if _sha256_bytes(payload) != expected_digest:
        _fail(f"{label}: digest mismatch")
    return path


def _runtime_tree_digest(root: Path, label: str) -> tuple[int, str]:
    files: list[Path] = []

    def walk(directory: Path) -> None:
        for child in sorted(directory.iterdir(), key=lambda value: value.name):
            if child.is_symlink():
                _fail(f"{label}: symlinks are not allowed")
            if child.is_dir():
                walk(child)
            elif child.is_file():
                files.append(child)
            else:
                _fail(f"{label}: unsupported runtime artifact type")

    walk(root)
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        try:
            payload = path.read_bytes()
        except OSError as exc:
            _fail(f"{label}: cannot read runtime artifact: {exc}")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
    return len(files), digest.hexdigest()


def _verify_runtime_tree(root: Path, entry: Any, label: str) -> None:
    mapping = _require_mapping(entry, label)
    if set(mapping) != {"path", "sha256", "file_count"}:
        _fail(f"{label}: unexpected runtime tree fields")
    path = _resolve_artifact(root, _relative_path(mapping["path"], f"{label}.path"), label)
    if not path.is_dir():
        _fail(f"{label}: expected a directory")
    expected_count = _require_int(mapping["file_count"], f"{label}.file_count")
    expected_digest = _require_sha256(mapping["sha256"], f"{label}.sha256")
    count, digest = _runtime_tree_digest(path, label)
    if count != expected_count:
        _fail(f"{label}: runtime file count mismatch")
    if digest != expected_digest:
        _fail(f"{label}: runtime tree digest mismatch")


def _digest_entries(value: Any, label: str, *, allow_path: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _fail(f"{label}: expected a non-empty list")
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        entry = _require_mapping(raw, f"{label}[{index}]")
        expected_keys = {"sha256", "size_bytes"}
        if allow_path:
            expected_keys.add("path")
        if set(entry) != expected_keys:
            _fail(f"{label}[{index}]: unexpected digest fields")
        normalized = {
            "sha256": _require_sha256(entry["sha256"], f"{label}[{index}].sha256"),
            "size_bytes": _require_int(entry["size_bytes"], f"{label}[{index}].size_bytes"),
        }
        if allow_path:
            normalized["path"] = _relative_path(entry["path"], f"{label}[{index}].path").as_posix()
        entries.append(normalized)
    return entries


def _manifest_digest_entries(value: Any, label: str, *, allow_path: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _fail(f"{label}: expected a list")
    entries: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        entry = _require_mapping(raw, f"{label}[{index}]")
        if set(entry) != {"path", "sha256", "size_bytes"}:
            _fail(f"{label}[{index}]: unexpected digest fields")
        normalized = {
            "sha256": _require_sha256(entry["sha256"], f"{label}[{index}].sha256"),
            "size_bytes": _require_int(entry["size_bytes"], f"{label}[{index}].size_bytes"),
        }
        if allow_path:
            normalized["path"] = _relative_path(entry["path"], f"{label}[{index}].path").as_posix()
        else:
            _require_text(entry["path"], f"{label}[{index}].path")
        entries.append(normalized)
    return entries


def _verify_manifest(manifest: Mapping[str, Any], bundle: Mapping[str, Any], report_revision: str, protocol: Mapping[str, Any], label: str) -> None:
    contract = _require_mapping(bundle["manifest_contract"], f"{label}.manifest_contract")
    if set(contract) != {"repo_revision", "pipeline_revision", "source_digests", "configuration_digests"}:
        _fail(f"{label}.manifest_contract: unexpected fields")
    if set(manifest) != _MANIFEST_KEYS:
        _fail(f"{label}.manifest: unexpected fields")
    benchmark = _require_text(bundle["benchmark"], f"{label}.benchmark")
    case_count = _require_int(bundle["case_count"], f"{label}.case_count")
    if manifest.get("schema_version") != 1:
        _fail(f"{label}.manifest: unsupported schema version")
    if manifest.get("benchmark") != benchmark:
        _fail(f"{label}.manifest: benchmark mismatch")
    if manifest.get("case_count") != case_count or manifest.get("completed_before") != case_count:
        _fail(f"{label}.manifest: incomplete case accounting")
    if manifest.get("repo_revision") != report_revision or manifest.get("pipeline_revision") != report_revision:
        _fail(f"{label}.manifest: report revision mismatch")
    if contract["repo_revision"] != report_revision or contract["pipeline_revision"] != report_revision:
        _fail(f"{label}.manifest_contract: report revision mismatch")
    if manifest.get("workers") != protocol["workers"]:
        _fail(f"{label}.manifest: workers mismatch")
    if manifest.get("case_timeout") != protocol["case_timeout_seconds"]:
        _fail(f"{label}.manifest: timeout mismatch")
    if manifest.get("successful_sql_memory_enabled") != "0":
        _fail(f"{label}.manifest: SQL memory must be disabled")
    expected_sources = _digest_entries(contract["source_digests"], f"{label}.source_digests", allow_path=False)
    actual_sources = _manifest_digest_entries(manifest.get("sources"), f"{label}.manifest.sources", allow_path=False)
    if actual_sources != expected_sources:
        _fail(f"{label}.manifest: source digests mismatch")
    expected_configs = _digest_entries(contract["configuration_digests"], f"{label}.configuration_digests", allow_path=True)
    actual_configs = _manifest_digest_entries(manifest.get("configuration_sources"), f"{label}.manifest.configuration_sources", allow_path=True)
    if actual_configs != expected_configs:
        _fail(f"{label}.manifest: configuration digests mismatch")


def _status_counts(rows: Iterable[Mapping[str, Any]], field: str, label: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        if value is None and field == "outcome_status":
            status = "null"
        else:
            status = _require_text(value, f"{label}.{field}")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _expected_status_counts(value: Any, label: str) -> dict[str, int]:
    mapping = _require_mapping(value, label)
    result: dict[str, int] = {}
    for key, count in mapping.items():
        result[_require_text(key, f"{label}.key")] = _require_int(count, f"{label}.{key}")
    return dict(sorted(result.items()))


def _verify_observations(path: Path, bundle: Mapping[str, Any], label: str) -> None:
    contract = _require_mapping(bundle["observations_contract"], f"{label}.observations_contract")
    if set(contract) != {"schema_version", "observation_status_counts", "workflow_status_counts", "outcome_status_counts"}:
        _fail(f"{label}.observations_contract: unexpected fields")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"{label}.observations: cannot read: {exc}")
    if not payload.endswith(b"\n"):
        _fail(f"{label}.observations: truncated JSONL evidence")
    rows: list[Mapping[str, Any]] = []
    seen_keys: set[str] = set()
    seen_ordinals: set[int] = set()
    benchmark = _require_text(bundle["benchmark"], f"{label}.benchmark")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        _fail(f"{label}.observations: invalid UTF-8: {exc}")
    for line_number, line in enumerate(lines, start=1):
        if not line:
            _fail(f"{label}.observations:{line_number}: blank line")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            _fail(f"{label}.observations:{line_number}: invalid JSON: {exc}")
        if not isinstance(row, dict):
            _fail(f"{label}.observations:{line_number}: expected an object")
        if row.get("schema_version") != contract["schema_version"]:
            _fail(f"{label}.observations:{line_number}: schema version mismatch")
        if row.get("benchmark") != benchmark:
            _fail(f"{label}.observations:{line_number}: benchmark mismatch")
        case_key = _require_text(row.get("case_key"), f"{label}.observations:{line_number}.case_key")
        ordinal = row.get("ordinal")
        if type(ordinal) is not int or ordinal < 0:
            _fail(f"{label}.observations:{line_number}: invalid ordinal")
        if case_key in seen_keys:
            _fail(f"{label}.observations: duplicate case_key {case_key}")
        if ordinal in seen_ordinals:
            _fail(f"{label}.observations: duplicate ordinal {ordinal}")
        seen_keys.add(case_key)
        seen_ordinals.add(ordinal)
        outcome = row.get("outcome")
        if outcome is not None and not isinstance(outcome, dict):
            _fail(f"{label}.observations:{line_number}: invalid outcome")
        outcome_status = (
            None
            if outcome is None
            else _require_text(
                outcome.get("status"),
                f"{label}.observations:{line_number}.outcome.status",
            )
        )
        normalized = dict(row)
        normalized["outcome_status"] = outcome_status
        rows.append(normalized)
    case_count = _require_int(bundle["case_count"], f"{label}.case_count")
    if len(rows) != case_count or seen_ordinals != set(range(case_count)):
        _fail(f"{label}.observations: partial case evidence")
    for field in ("observation_status", "workflow_status", "outcome_status"):
        actual = _status_counts(rows, field, label)
        expected = _expected_status_counts(contract[f"{field}_counts"], f"{label}.{field}_counts")
        if actual != expected:
            _fail(f"{label}.observations: {field} counts mismatch")


def verify_baseline(baseline_path: Path, artifact_root: Path) -> None:
    baseline = _read_json(baseline_path, "baseline")
    if set(baseline) != {"schema_version", "baseline_id", "report_revision", "protocol", "bundles"}:
        _fail("baseline: unexpected fields")
    if baseline.get("schema_version") != 1:
        _fail("baseline: unsupported schema version")
    _require_text(baseline.get("baseline_id"), "baseline.baseline_id")
    report_revision = _require_text(baseline.get("report_revision"), "baseline.report_revision")
    protocol = _require_mapping(baseline.get("protocol"), "baseline.protocol")
    if set(protocol) != {"workers", "case_timeout_seconds", "sql_memory_disabled", "runtime_identity"}:
        _fail("baseline.protocol: unexpected fields")
    if protocol.get("workers") != 1 or protocol.get("case_timeout_seconds") != 900:
        _fail("baseline.protocol: unexpected execution limits")
    if protocol.get("sql_memory_disabled") is not True:
        _fail("baseline.protocol: SQL memory must be disabled")
    identity = _require_mapping(protocol.get("runtime_identity"), "baseline.protocol.runtime_identity")
    if identity != {"status": "unavailable_from_gateway"}:
        _fail("baseline.protocol.runtime_identity: expected unavailable_from_gateway")
    bundles = baseline.get("bundles")
    if not isinstance(bundles, list) or len(bundles) != 2:
        _fail("baseline.bundles: expected exactly BIRD and Spider bundles")
    root = artifact_root.resolve()
    if not root.is_dir():
        _fail("artifact root is missing")
    seen_bundle_ids: set[str] = set()
    seen_benchmarks: set[str] = set()
    for index, raw_bundle in enumerate(bundles):
        label = f"baseline.bundles[{index}]"
        bundle = _require_mapping(raw_bundle, label)
        if set(bundle) != _BUNDLE_KEYS:
            _fail(f"{label}: unexpected fields")
        bundle_id = _require_text(bundle.get("bundle_id"), f"{label}.bundle_id")
        benchmark = _require_text(bundle.get("benchmark"), f"{label}.benchmark")
        if bundle_id in seen_bundle_ids or benchmark in seen_benchmarks:
            _fail(f"{label}: duplicate bundle")
        seen_bundle_ids.add(bundle_id)
        seen_benchmarks.add(benchmark)
        evidence = _require_mapping(bundle.get("evidence"), f"{label}.evidence")
        if set(evidence) != _EVIDENCE_KEYS:
            _fail(f"{label}.evidence: missing or extra evidence entries")
        paths: set[PurePosixPath] = set()
        verified: dict[str, Path] = {}
        for evidence_name in sorted(_EVIDENCE_KEYS):
            entry = _require_mapping(evidence[evidence_name], f"{label}.evidence.{evidence_name}")
            relative = _relative_path(entry.get("path"), f"{label}.evidence.{evidence_name}.path")
            if relative in paths:
                _fail(f"{label}.evidence: duplicate artifact path")
            paths.add(relative)
            verified[evidence_name] = _verify_evidence_file(root, entry, f"{label}.evidence.{evidence_name}")
        _verify_runtime_tree(root, bundle.get("runtime_tree"), f"{label}.runtime_tree")
        _verify_manifest(_read_json(verified["manifest"], f"{label}.manifest"), bundle, report_revision, protocol, label)
        _verify_observations(verified["observations"], bundle, label)
    if seen_benchmarks != {"bird", "spider"}:
        _fail("baseline.bundles: expected bird and spider exactly once")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        verify_baseline(args.baseline, args.artifact_root)
    except BaselineVerificationError as exc:
        print(f"baseline verification failed: {exc}", file=sys.stderr)
        return 1
    print("historical Text-to-SQL benchmark baseline verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
