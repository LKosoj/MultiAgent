"""Pinned inputs and identity validation for public benchmark releases."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .official_evaluator_contracts import (
    BIRD_EVALUATOR_DATA_PATHS,
    BIRD_EVALUATOR_SOURCE_PATHS,
    EVALUATOR_CALL_SURFACES,
    EVALUATOR_IDENTITY_FIELDS,
    IDENTITY_ARTIFACT_SHA256,
    LEGACY_EVALUATOR_IDENTITY_FIELDS,
    SPIDER_EVALUATOR_DATA_PATHS,
    SPIDER_EVALUATOR_SOURCE_PATHS,
    file_records_digest,
    spider_gold_inventory,
    spider_local_references,
    validate_image_identity,
)
from .sandbox import SandboxError, resolve_safe_regular_file
CANONICAL_RELEASE_DATASET_ORDER = ("bird", "spider")
_SCHEMA_CACHE_KINDS = frozenset(
    {
        "schema_table",
        "schema_ready",
        "schema_probe_fact",
        "schema_semantic_fact",
        "successful_sql_example",
    }
)


class ReleaseCase(Protocol):
    ordinal: int
    case_key: str
    case_id: str
    database_id: str
    database_path: Path
    question: str
    external_knowledge: str
    schema_description_path: Path | None

    def prompt(self) -> str: ...


@dataclass(frozen=True)
class FrozenReleaseInputs:
    """Cases and database digests produced by the same lock validation pass."""

    cases_by_benchmark: Mapping[str, tuple[ReleaseCase, ...]]
    case_manifests: Mapping[str, Mapping[str, object]]
    database_digests_by_benchmark: Mapping[str, Mapping[str, str]]

    def cases_for(self, benchmark: str) -> tuple[ReleaseCase, ...]:
        try:
            return self.cases_by_benchmark[benchmark]
        except KeyError as exc:
            raise SandboxError(f"release binding has no cases for {benchmark}") from exc


FileDigestCache = dict[Path, tuple[tuple[int, int, int, int], str]]


def json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def schema_memory_source_identity(source: Path | None) -> dict[str, str]:
    """Bind the previous release's non-empty schema-memory tree."""

    if source is None:
        raise SandboxError("canonical release schema-memory source is required")
    root = Path(source).resolve()
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("schema-memory source is missing or unsafe")
    records = _transferable_schema_records(root)
    if not records:
        raise SandboxError("schema-memory source is empty")
    return {"root": str(root), "digest": json_digest(records)}


def filter_schema_memory_copy(root: Path) -> None:
    """Remove non-schema records from a release-local schema-memory copy."""

    database_roots = _schema_memory_database_roots(root)
    schema_snapshots = _schema_snapshot_paths(root)
    for database_root in database_roots:
        for path in database_root.iterdir():
            if path.name in {"smolagents_memory.db", "chromadb"}:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        database = database_root / "smolagents_memory.db"
        if database.is_file():
            _filter_schema_memory_sqlite(database)
        chromadb_root = database_root / "chromadb"
        if chromadb_root.is_dir() and any(chromadb_root.iterdir()):
            _filter_schema_memory_chroma(chromadb_root)
    protected = set(database_roots) | set(schema_snapshots)
    for database_root in database_roots:
        protected.update(database_root.parents)
    for schema_snapshot in schema_snapshots:
        protected.update(schema_snapshot.parents)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path in protected or any(database_root in path.parents for database_root in database_roots):
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _schema_memory_database_roots(root: Path) -> tuple[Path, ...]:
    roots = {
        path.parent for path in root.rglob("smolagents_memory.db") if path.is_file()
    }
    roots.update(
        path.parent for path in root.rglob("chromadb") if path.is_dir()
    )
    return tuple(sorted(roots, key=lambda item: item.as_posix()))


def _schema_snapshot_paths(root: Path) -> tuple[Path, ...]:
    snapshots: list[Path] = []
    for path in root.rglob("schema-v1-*.json"):
        if path.is_symlink():
            raise SandboxError("schema snapshot source is unsafe")
        if path.is_file() and path.parent.name == "sqlrag":
            snapshots.append(path)
    return tuple(sorted(snapshots, key=lambda item: item.as_posix()))


def _filter_schema_memory_sqlite(database: Path) -> None:
    try:
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "agent_memory" in tables:
                for rowid, raw_data in connection.execute(
                    "SELECT rowid, data FROM agent_memory"
                ):
                    if _cache_kind(raw_data) not in _SCHEMA_CACHE_KINDS:
                        connection.execute("DELETE FROM agent_memory WHERE rowid = ?", (rowid,))
            for table in tables - {"agent_memory"}:
                quoted_table = '"' + table.replace('"', '""') + '"'
                connection.execute(f"DELETE FROM {quoted_table}")
    except sqlite3.Error as exc:
        raise SandboxError("schema-memory SQLite filtering failed") from exc


def _filter_schema_memory_chroma(chromadb_root: Path) -> None:
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(chromadb_root))
        for collection in client.list_collections():
            result = collection.get(include=["metadatas"])
            ids = result.get("ids", [])
            metadatas = result.get("metadatas", [])
            remove = [
                identifier
                for identifier, metadata in zip(ids, metadatas)
                if not isinstance(identifier, str)
                or not isinstance(metadata, dict)
                or metadata.get("cache_kind") not in _SCHEMA_CACHE_KINDS
            ]
            if remove:
                collection.delete(ids=remove)
    except Exception as exc:
        raise SandboxError("schema-memory Chroma filtering failed") from exc


def _transferable_schema_records(root: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for snapshot in _schema_snapshot_paths(root):
        records.append(
            {
                "store": "schema_snapshot",
                "path": snapshot.relative_to(root).as_posix(),
                "sha256": sha256_file(snapshot),
            }
        )
    for database_root in _schema_memory_database_roots(root):
        relative_root = database_root.relative_to(root).as_posix()
        database = database_root / "smolagents_memory.db"
        if database.is_file():
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_memory'"
                    ).fetchone()
                    rows = (
                        connection.execute(
                            "SELECT session_id, agent_name, step, data FROM agent_memory"
                        ).fetchall()
                        if exists is not None
                        else []
                    )
            except sqlite3.Error as exc:
                raise SandboxError("schema-memory SQLite source is invalid") from exc
            for session_id, agent_name, step, raw_data in rows:
                if _cache_kind(raw_data) in _SCHEMA_CACHE_KINDS:
                    records.append(
                        {
                            "store": "sqlite",
                            "database_root": relative_root,
                            "session_id": session_id,
                            "agent_name": agent_name,
                            "step": step,
                            "data": raw_data,
                        }
                    )
        chromadb_root = database_root / "chromadb"
        if chromadb_root.is_dir() and any(chromadb_root.iterdir()):
            records.extend(_transferable_schema_chroma_records(chromadb_root, relative_root))
    return sorted(
        records,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _transferable_schema_chroma_records(
    chromadb_root: Path, database_root: str
) -> list[dict[str, object]]:
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(chromadb_root))
        records: list[dict[str, object]] = []
        for collection in client.list_collections():
            result = collection.get(include=["metadatas", "documents", "embeddings"])
            for identifier, metadata, document, embedding in zip(
                result.get("ids", []),
                result.get("metadatas", []),
                result.get("documents", []),
                result.get("embeddings", []),
            ):
                if isinstance(metadata, dict) and metadata.get("cache_kind") in _SCHEMA_CACHE_KINDS:
                    if hasattr(embedding, "tolist"):
                        embedding = embedding.tolist()
                    if not isinstance(embedding, list) or any(
                        not isinstance(value, (int, float)) for value in embedding
                    ):
                        raise SandboxError("schema-memory Chroma embedding is invalid")
                    records.append(
                        {
                            "store": "chroma",
                            "database_root": database_root,
                            "collection": collection.name,
                            "id": identifier,
                            "metadata": metadata,
                            "document": document,
                            "embedding": embedding,
                        }
                    )
        return records
    except Exception as exc:
        raise SandboxError("schema-memory Chroma source is invalid") from exc


def _cache_kind(raw_data: object) -> str | None:
    try:
        payload = json.loads(raw_data)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload.get("cache_kind") if isinstance(payload, dict) else None


def _cached_sha256_file(
    path: Path,
    cache: FileDigestCache | None,
    *,
    digest_file: Callable[[Path], str] | None = None,
) -> str:
    safe_path = resolve_safe_regular_file(path, label="release input")
    calculate_digest = digest_file or sha256_file
    if cache is None:
        return calculate_digest(safe_path)
    stat = safe_path.stat()
    fingerprint = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    cached = cache.get(safe_path)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    digest = calculate_digest(safe_path)
    cache[safe_path] = (fingerprint, digest)
    return digest


def schema_description_sidecar_identity(
    case: ReleaseCase,
    *,
    digest_cache: FileDigestCache | None = None,
) -> dict[str, object] | None:
    """Return the closed, per-database curated-description input set."""

    raw_root = getattr(case, "schema_description_path", None)
    if raw_root is None:
        return None
    database_parent = resolve_safe_regular_file(
        case.database_path, label="database"
    ).parent.resolve()
    root = Path(raw_root)
    if root != database_parent / "database_description":
        raise SandboxError("schema description sidecar is outside its database")
    if root.is_symlink() or not root.is_dir():
        raise SandboxError("schema description sidecar is missing or unsafe")

    records: list[dict[str, str]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".csv":
            raise SandboxError("schema description sidecar has an invalid entry")
        records.append(
            {
                "path": path.name,
                "sha256": _cached_sha256_file(path, digest_cache),
            }
        )
    if not records:
        raise SandboxError("schema description sidecar is empty")
    return {
        "path": "database_description",
        "files": records,
        "digest": json_digest(records),
    }


def file_identity(
    path: Path,
    *,
    digest_cache: FileDigestCache | None = None,
) -> dict[str, object]:
    safe_path = resolve_safe_regular_file(path, label="release input")
    return {
        "size_bytes": safe_path.stat().st_size,
        "sha256": _cached_sha256_file(safe_path, digest_cache),
    }


def release_dataset_policy(
    policy: Mapping[str, object],
    benchmark: str,
) -> Mapping[str, object]:
    datasets = policy.get("datasets")
    if not isinstance(datasets, Mapping):
        raise SandboxError("release policy datasets are invalid")
    dataset = datasets.get(benchmark)
    if not isinstance(dataset, Mapping):
        raise SandboxError(f"release policy has no {benchmark} dataset")
    return dataset


def evaluator_identity(
    dataset_policy: Mapping[str, object],
    *,
    dataset_root: Path,
) -> dict[str, object]:
    """Return the pinned evaluator identity and verify its executable file.

    The evaluator is an external post-run program.  Keeping this validation in
    the release lock prevents an operator from scoring the same predictions
    with a different evaluator after generation has started.
    """
    raw = dataset_policy.get("evaluator")
    if not isinstance(raw, Mapping) or set(raw) not in {
        LEGACY_EVALUATOR_IDENTITY_FIELDS,
        EVALUATOR_IDENTITY_FIELDS,
    }:
        raise SandboxError("release policy evaluator identity is invalid")
    identity = {name: raw[name] for name in sorted(raw)}
    if not all(isinstance(identity[name], str) and identity[name] for name in identity):
        raise SandboxError("release policy evaluator identity is invalid")
    entrypoint = Path(str(identity["entrypoint"]))
    if entrypoint.is_absolute() or ".." in entrypoint.parts:
        raise SandboxError("release policy evaluator entrypoint is unsafe")
    try:
        evaluator_path = resolve_safe_regular_file(
            dataset_root / entrypoint,
            label="official evaluator",
        )
    except OSError as exc:
        raise SandboxError("official evaluator is missing or unsafe") from exc
    if not evaluator_path.is_relative_to(dataset_root.resolve()):
        raise SandboxError("official evaluator escapes the frozen dataset root")
    expected_sha256 = str(identity["sha256"])
    if len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        raise SandboxError("release policy evaluator sha256 is invalid")
    if sha256_file(evaluator_path) != expected_sha256:
        raise SandboxError("official evaluator does not match frozen policy")
    return identity


def build_release_plan(policy: Mapping[str, object]) -> list[dict[str, object]]:
    seeds = policy.get("repeat_seeds")
    if (
        not isinstance(seeds, list)
        or len(seeds) != 3
        or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds)
        or len(set(seeds)) != 3
    ):
        raise ValueError(
            "canonical release requires exactly three distinct integer seeds"
        )
    return [
        {
            "benchmark": benchmark,
            "repeat_ordinal": repeat_ordinal,
            "seed": seed,
        }
        for benchmark in CANONICAL_RELEASE_DATASET_ORDER
        for repeat_ordinal, seed in enumerate(seeds, start=1)
    ]


def canonical_runtime_environment(
    settings: Mapping[str, object],
    *,
    llm_models_profile: str | None = None,
) -> dict[str, str]:
    allowed_settings = {"model_api_base"}
    unknown = sorted(set(settings) - allowed_settings)
    if unknown:
        raise ValueError(
            "unknown canonical environment setting(s): " + ", ".join(unknown)
        )
    model_api_base = str(settings.get("model_api_base") or "").strip()
    parsed = urlsplit(model_api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("canonical model API base must be an http(s) URL")
    if llm_models_profile is None or (
        isinstance(llm_models_profile, str) and not llm_models_profile.strip()
    ):
        # No profile pinned explicitly (an empty/blank string is treated the
        # same as not passing one at all): write the loader's own default
        # ("default") rather than leaving the operator's ambient
        # TEXT_TO_SQL_LLM_MODELS_PROFILE in effect, so the release
        # environment (and its digest) stays reproducible regardless of the
        # shell that launches the benchmark.
        resolved_llm_models_profile = "default"
    elif isinstance(llm_models_profile, str):
        # Fail fast on a typo'd/unknown profile name here, rather than
        # silently pinning it into the release environment and only
        # discovering it does not exist once something deep in the
        # benchmark run tries to load it.
        from custom_tools.text_to_sql.llm_models_config import get_active_profile

        try:
            get_active_profile(llm_models_profile)
        except KeyError as error:
            raise ValueError(
                f"unknown llm models profile: {llm_models_profile}"
            ) from error
        resolved_llm_models_profile = llm_models_profile
    else:
        raise ValueError("llm_models_profile must be non-empty text or None")
    environment = {
        "OPENAI_API_BASE_DB": model_api_base,
        "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS": "/benchmark-input",
        "TEXT_TO_SQL_ALLOWED_DB_SCHEMES": "sqlite",
        "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "0",
        "TEXT_TO_SQL_CODE_LABEL_CASCADE_HINT": "shadow",
        "TEXT_TO_SQL_CLARIFYING_QUESTIONS": "0",
        "TEXT_TO_SQL_LLM_MODELS_PROFILE": resolved_llm_models_profile,
    }
    return environment


def verified_git_provenance(
    root: Path,
    *,
    origin: str,
    revision: str,
) -> dict[str, object]:
    safe_root = root.resolve(strict=True)

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(safe_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    try:
        actual_origin = git("remote", "get-url", "origin")
        actual_revision = git("rev-parse", "HEAD")
        inside = git("rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SandboxError(f"cannot prove Git provenance for {safe_root}") from exc
    normalized_actual = _normalize_git_origin(actual_origin)
    normalized_expected = _normalize_git_origin(origin)
    if normalized_actual != normalized_expected or actual_revision != revision:
        raise SandboxError(f"Git provenance mismatch for {safe_root}")
    if inside != "true":
        raise SandboxError(f"Git provenance root is not a worktree: {safe_root}")
    return {
        "origin": actual_origin,
        "revision": actual_revision,
        "worktree": str(safe_root),
    }


def _normalize_git_origin(origin: str) -> str:
    """Compare equivalent HTTPS and SCP-style GitHub remotes consistently."""

    value = origin.strip()
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"https://{host}/{path}"
    return value.removesuffix(".git").rstrip("/")


def stable_case_manifest(
    benchmark: str,
    cases: Sequence[ReleaseCase],
    *,
    digest_cache: FileDigestCache | None = None,
    digest_file: Callable[[Path], str] | None = None,
) -> dict[str, object]:
    cache = digest_cache if digest_cache is not None else {}
    rows = [
        {
            "ordinal": case.ordinal,
            "case_key": case.case_key,
            "case_id": case.case_id,
            "database_id": case.database_id,
            "question_sha256": hashlib.sha256(
                case.question.encode("utf-8")
            ).hexdigest(),
            "external_knowledge_sha256": hashlib.sha256(
                case.external_knowledge.encode("utf-8")
            ).hexdigest(),
            "prompt_sha256": hashlib.sha256(case.prompt().encode("utf-8")).hexdigest(),
            "database_sha256": _cached_sha256_file(
                case.database_path,
                cache,
                digest_file=digest_file,
            ),
            "schema_description_sidecar": schema_description_sidecar_identity(
                case,
                digest_cache=cache,
            ),
        }
        for case in sorted(cases, key=lambda item: (item.ordinal, item.case_key))
    ]
    return {
        "benchmark": benchmark,
        "case_count": len(rows),
        "cases_digest": json_digest(rows),
        "cases": rows,
    }


def _assert_policy_file(
    path: Path,
    expected: Mapping[str, object],
    *,
    label: str,
) -> None:
    actual = file_identity(path)
    if actual["size_bytes"] != expected.get("size_bytes") or actual[
        "sha256"
    ] != expected.get("sha256"):
        raise SandboxError(f"{label} does not match frozen policy")


def _release_input_record(
    *,
    benchmark: str,
    kind: str,
    root: str,
    relative_path: str,
    path: Path,
    digest_cache: FileDigestCache | None = None,
) -> dict[str, object]:
    return {
        "benchmark": benchmark,
        "kind": kind,
        "root": root,
        "path": relative_path,
        **file_identity(path, digest_cache=digest_cache),
    }


def _database_input_records(
    args: argparse.Namespace,
    *,
    bird_cases: Sequence[ReleaseCase],
    spider_cases: Sequence[ReleaseCase],
    digest_cache: FileDigestCache,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for benchmark, cases, root_name, database_root in (
        ("bird", bird_cases, "bird_root", args.bird_root),
        ("spider", spider_cases, "spider_sqlite_root", args.spider_sqlite_root),
    ):
        for case in cases:
            key = (benchmark, str(case.database_path))
            if key in seen:
                continue
            seen.add(key)
            records.append(
                _release_input_record(
                    benchmark=benchmark,
                    kind="database",
                    root=root_name,
                    relative_path=case.database_path.relative_to(
                        Path(database_root).resolve()
                    ).as_posix(),
                    path=case.database_path,
                    digest_cache=digest_cache,
                )
            )
    return records


def _spider_document_input_records(
    args: argparse.Namespace,
    *,
    spider_task: Path,
    digest_cache: FileDigestCache,
) -> list[dict[str, object]]:
    rows: list[dict[str, Any]] = []
    with spider_task.open(encoding="utf-8") as handle:
        for raw_line in handle:
            if raw_line.strip():
                value = json.loads(raw_line)
                if not isinstance(value, dict):
                    raise SandboxError("Spider task row is invalid")
                rows.append(value)
    document_names = sorted(
        {
            str(row.get("external_knowledge") or "").strip()
            for row in rows
            if str(row.get("instance_id") or "").startswith("local")
            and str(row.get("external_knowledge") or "").strip()
        }
    )
    documents_root = args.spider_root / "resource" / "documents"
    records: list[dict[str, object]] = []
    for document_name in document_names:
        document_path = resolve_safe_regular_file(
            documents_root / document_name,
            label="Spider document",
        )
        if not document_path.is_relative_to(documents_root.resolve()):
            raise SandboxError("Spider document escapes the frozen dataset root")
        records.append(
            _release_input_record(
                benchmark="spider",
                kind="document",
                root="spider_root",
                relative_path=document_path.relative_to(
                    args.spider_root.resolve()
                ).as_posix(),
                path=document_path,
                digest_cache=digest_cache,
            )
        )
    return records


def _official_evaluator_input_records(
    args: argparse.Namespace,
    *,
    bird_task: Path,
    spider_task: Path,
    digest_cache: FileDigestCache,
) -> list[dict[str, object]]:
    identity_path = Path(args.official_evaluator_image_identity)
    validate_image_identity(identity_path)
    records = [
        _release_input_record(
            benchmark="shared",
            kind="evaluator_runtime",
            root="official_evaluator_image_identity",
            relative_path=".",
            path=identity_path,
            digest_cache=digest_cache,
        )
    ]
    for benchmark, root, source_paths, data_paths in (
        (
            "bird",
            args.bird_root,
            BIRD_EVALUATOR_SOURCE_PATHS,
            BIRD_EVALUATOR_DATA_PATHS,
        ),
        (
            "spider",
            args.spider_root,
            SPIDER_EVALUATOR_SOURCE_PATHS,
            SPIDER_EVALUATOR_DATA_PATHS,
        ),
    ):
        root_name = f"{benchmark}_root"
        for kind, paths in (
            ("evaluator_source", source_paths),
            ("evaluator_data", data_paths),
        ):
            records.extend(
                _release_input_record(
                    benchmark=benchmark,
                    kind=kind,
                    root=root_name,
                    relative_path=relative_path,
                    path=root / relative_path,
                    digest_cache=digest_cache,
                )
                for relative_path in paths
            )
    references = spider_local_references(
        spider_task,
        args.spider_database_map,
    )
    gold_root = args.spider_root / "evaluation_suite" / "gold"
    records.extend(
        _release_input_record(
            benchmark="spider",
            kind="official_gold",
            root="spider_root",
            relative_path=path.relative_to(args.spider_root.resolve()).as_posix(),
            path=path,
            digest_cache=digest_cache,
        )
        for _instance_id, path in spider_gold_inventory(references, gold_root)
    )
    return records


def _validate_evaluator_closures(
    evaluator_identities: Mapping[str, Mapping[str, object]],
    inputs: Sequence[Mapping[str, object]],
) -> None:
    data_kinds = frozenset(
        {"task", "database", "database_map", "evaluator_data", "official_gold"}
    )
    for benchmark in CANONICAL_RELEASE_DATASET_ORDER:
        identity = evaluator_identities[benchmark]
        if set(identity) != EVALUATOR_IDENTITY_FIELDS:
            raise SandboxError("release policy evaluator closure is incomplete")
        source = [
            row for row in inputs
            if row.get("benchmark") == benchmark
            and row.get("kind") == "evaluator_source"
        ]
        data = [
            row for row in inputs
            if row.get("benchmark") == benchmark and row.get("kind") in data_kinds
        ]
        expected = {
            "call_surface": EVALUATOR_CALL_SURFACES[benchmark],
            "source_closure_sha256": file_records_digest(source),
            "data_closure_sha256": file_records_digest(data),
            "runtime_identity_sha256": IDENTITY_ARTIFACT_SHA256,
        }
        if any(identity.get(name) != value for name, value in expected.items()):
            raise SandboxError(f"{benchmark} official evaluator closure mismatch")


def _release_file_inventory(
    args: argparse.Namespace,
    *,
    policy_schema: int,
    bird_task_policy: Mapping[str, object],
    spider_task_policy: Mapping[str, object],
    bird_task: Path,
    spider_task: Path,
    bird_cases: Sequence[ReleaseCase],
    spider_cases: Sequence[ReleaseCase],
    evaluator_identities: Mapping[str, Mapping[str, object]],
    digest_cache: FileDigestCache,
) -> list[dict[str, object]]:
    inputs = [
        _release_input_record(
            benchmark="bird",
            kind="task",
            root="bird_root",
            relative_path=str(bird_task_policy["path"]),
            path=bird_task,
            digest_cache=digest_cache,
        ),
        _release_input_record(
            benchmark="spider",
            kind="task",
            root="spider_root",
            relative_path=str(spider_task_policy["path"]),
            path=spider_task,
            digest_cache=digest_cache,
        ),
        _release_input_record(
            benchmark="spider",
            kind="database_map",
            root="spider_database_map",
            relative_path=".",
            path=args.spider_database_map,
            digest_cache=digest_cache,
        ),
    ]
    inputs.extend(
        _database_input_records(
            args,
            bird_cases=bird_cases,
            spider_cases=spider_cases,
            digest_cache=digest_cache,
        )
    )
    inputs.extend(
        _spider_document_input_records(
            args,
            spider_task=spider_task,
            digest_cache=digest_cache,
        )
    )
    if policy_schema == 2:
        inputs.extend(
            _official_evaluator_input_records(
                args,
                bird_task=bird_task,
                spider_task=spider_task,
                digest_cache=digest_cache,
            )
        )
        _validate_evaluator_closures(evaluator_identities, inputs)
    return inputs


def _build_release_lock_and_binding(
    args: argparse.Namespace,
    *,
    policy: Mapping[str, object],
    provenance: Mapping[str, object],
    evaluator_identities: Mapping[str, object],
    inputs: Sequence[Mapping[str, object]],
    bird_cases: tuple[ReleaseCase, ...],
    spider_cases: tuple[ReleaseCase, ...],
    digest_cache: FileDigestCache,
    schema_memory_source: Mapping[str, str],
) -> tuple[dict[str, object], FrozenReleaseInputs]:
    canonical_environment = canonical_runtime_environment(
        {"model_api_base": args.model_api_base},
        llm_models_profile=getattr(args, "llm_models_profile", None),
    )
    model_backend_id = str(args.model_backend_id or "").strip()
    if not model_backend_id:
        raise SandboxError("canonical model backend identity is required")
    model_identity = {
        "backend_release_id": model_backend_id,
        "routes": policy.get("model_routes", {}),
    }
    case_manifests = {
        "bird": stable_case_manifest("bird", bird_cases, digest_cache=digest_cache),
        "spider": stable_case_manifest(
            "spider", spider_cases, digest_cache=digest_cache
        ),
    }
    lock: dict[str, object] = {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_input_lock",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "policy_digest": json_digest(policy),
        "release_plan": build_release_plan(policy),
        "provenance": dict(provenance),
        "canonical_environment": canonical_environment,
        "canonical_environment_digest": json_digest(canonical_environment),
        "model_identity": model_identity,
        "model_identity_digest": json_digest(model_identity),
        "evaluator_identities": dict(evaluator_identities),
        "inputs": sorted(
            (dict(item) for item in inputs),
            key=lambda item: (
                str(item["benchmark"]),
                str(item["kind"]),
                str(item["root"]),
                str(item["path"]),
            ),
        ),
        "case_manifests": case_manifests,
        "schema_memory_source": dict(schema_memory_source),
    }
    lock["lock_digest"] = json_digest(lock)
    database_digests = {
        benchmark: MappingProxyType(
            {
                str(row["case_key"]): str(row["database_sha256"])
                for row in manifest["cases"]
            }
        )
        for benchmark, manifest in case_manifests.items()
    }
    binding = FrozenReleaseInputs(
        cases_by_benchmark=MappingProxyType(
            {"bird": bird_cases, "spider": spider_cases}
        ),
        case_manifests=MappingProxyType(case_manifests),
        database_digests_by_benchmark=MappingProxyType(database_digests),
    )
    return lock, binding


def inspect_release_inputs(
    args: argparse.Namespace,
    *,
    policy: Mapping[str, object],
    load_bird_cases: Callable[[Path], Sequence[ReleaseCase]],
    load_spider_cases: Callable[[Path, Path, Path], Sequence[ReleaseCase]],
    prove_git: Callable[..., Mapping[str, object]] = verified_git_provenance,
    schema_memory_source: Path | None = None,
) -> tuple[dict[str, object], FrozenReleaseInputs]:
    digest_cache: FileDigestCache = {}
    source_identity = schema_memory_source_identity(
        schema_memory_source
        if schema_memory_source is not None
        else getattr(args, "schema_memory_source", None)
    )
    policy_schema = policy.get("schema_version")
    if (
        not isinstance(policy_schema, int)
        or isinstance(policy_schema, bool)
        or policy_schema not in {1, 2}
    ):
        raise SandboxError("release policy schema version is invalid")
    if policy_schema == 2 and getattr(
        args, "official_evaluator_image_identity", None
    ) is None:
        raise SandboxError("official evaluator image identity is required")
    bird_policy = release_dataset_policy(policy, "bird")
    spider_policy = release_dataset_policy(policy, "spider")
    evaluator_identities = {
        "bird": evaluator_identity(bird_policy, dataset_root=args.bird_root),
        "spider": evaluator_identity(spider_policy, dataset_root=args.spider_root),
    }
    bird_task_policy = bird_policy.get("task_file")
    spider_task_policy = spider_policy.get("task_file")
    spider_map_policy = spider_policy.get("database_map")
    if not all(
        isinstance(item, Mapping)
        for item in (bird_task_policy, spider_task_policy, spider_map_policy)
    ):
        raise SandboxError("release policy input identities are invalid")
    bird_task = args.bird_root / str(bird_task_policy["path"])
    spider_task = args.spider_root / str(spider_task_policy["path"])
    _assert_policy_file(bird_task, bird_task_policy, label="BIRD task file")
    _assert_policy_file(spider_task, spider_task_policy, label="Spider task file")
    _assert_policy_file(
        args.spider_database_map,
        spider_map_policy,
        label="Spider database map",
    )
    provenance = {
        "bird": prove_git(
            args.bird_root,
            origin=str(bird_policy["origin"]),
            revision=str(bird_policy["revision"]),
        ),
        "spider": prove_git(
            args.spider_root,
            origin=str(spider_policy["origin"]),
            revision=str(spider_policy["revision"]),
        ),
        "spider_sqlite": prove_git(
            args.spider_sqlite_root,
            origin=str(spider_policy["origin"]),
            revision=str(spider_policy["revision"]),
        ),
    }
    bird_cases = tuple(load_bird_cases(args.bird_root))
    spider_cases = tuple(
        load_spider_cases(
            args.spider_root,
            args.spider_sqlite_root,
            args.spider_database_map,
        )
    )
    for benchmark, cases, dataset_policy in (
        ("bird", bird_cases, bird_policy),
        ("spider", spider_cases, spider_policy),
    ):
        if len(cases) != dataset_policy.get("case_count"):
            raise SandboxError(
                f"canonical {benchmark} case count must be "
                f"{dataset_policy.get('case_count')}, got {len(cases)}"
            )
    inputs = _release_file_inventory(
        args,
        policy_schema=policy_schema,
        bird_task_policy=bird_task_policy,
        spider_task_policy=spider_task_policy,
        bird_task=bird_task,
        spider_task=spider_task,
        bird_cases=bird_cases,
        spider_cases=spider_cases,
        evaluator_identities=evaluator_identities,
        digest_cache=digest_cache,
    )
    return _build_release_lock_and_binding(
        args,
        policy=policy,
        provenance=provenance,
        evaluator_identities=evaluator_identities,
        inputs=inputs,
        bird_cases=bird_cases,
        spider_cases=spider_cases,
        digest_cache=digest_cache,
        schema_memory_source=source_identity,
    )


def release_input_path(
    args: argparse.Namespace,
    record: Mapping[str, object],
) -> Path:
    root_name = record.get("root")
    relative_path = record.get("path")
    if not isinstance(root_name, str) or not isinstance(relative_path, str):
        raise SandboxError("release input record path is invalid")
    roots = {
        "bird_root": args.bird_root,
        "spider_root": args.spider_root,
        "spider_sqlite_root": args.spider_sqlite_root,
        "spider_database_map": args.spider_database_map,
        "official_evaluator_image_identity": getattr(
            args, "official_evaluator_image_identity", None
        ),
    }
    if root_name not in roots:
        raise SandboxError(f"release input record has unknown root: {root_name}")
    if roots[root_name] is None:
        raise SandboxError(f"release input root is unavailable: {root_name}")
    root = Path(roots[root_name])
    return root if relative_path == "." else root / relative_path


def validate_release_input_lock(
    args: argparse.Namespace,
    lock: Mapping[str, object],
    *,
    policy: Mapping[str, object],
    load_bird_cases: Callable[[Path], Sequence[ReleaseCase]],
    load_spider_cases: Callable[[Path, Path, Path], Sequence[ReleaseCase]],
    prove_git: Callable[..., Mapping[str, object]] = verified_git_provenance,
) -> FrozenReleaseInputs:
    if lock.get("record_kind") != "text2sql_public_benchmark_input_lock":
        raise SandboxError("release input lock kind is invalid")
    if lock.get("policy_digest") != json_digest(policy):
        raise SandboxError("release input lock policy digest mismatch")
    if lock.get("release_plan") != build_release_plan(policy):
        raise SandboxError("release plan does not match canonical order")
    expected_environment = canonical_runtime_environment(
        {"model_api_base": args.model_api_base},
        llm_models_profile=getattr(args, "llm_models_profile", None),
    )
    if lock.get("canonical_environment") != expected_environment or lock.get(
        "canonical_environment_digest"
    ) != json_digest(expected_environment):
        raise SandboxError("release environment identity mismatch")
    model_identity = lock.get("model_identity")
    if (
        not isinstance(model_identity, Mapping)
        or model_identity.get("backend_release_id")
        != str(args.model_backend_id or "").strip()
    ):
        raise SandboxError("release model identity mismatch")
    if lock.get("model_identity_digest") != json_digest(model_identity):
        raise SandboxError("release model identity digest mismatch")
    inputs = lock.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise SandboxError("release input lock has no input inventory")
    for raw_record in inputs:
        if not isinstance(raw_record, Mapping):
            raise SandboxError("release input record is invalid")
        try:
            actual = file_identity(release_input_path(args, raw_record))
        except (OSError, SandboxError) as exc:
            raise SandboxError("release input changed after lock creation") from exc
        if actual.get("size_bytes") != raw_record.get("size_bytes") or actual.get(
            "sha256"
        ) != raw_record.get("sha256"):
            raise SandboxError("release input changed after lock creation")
    source_identity = lock.get("schema_memory_source")
    if (
        not isinstance(source_identity, Mapping)
        or set(source_identity) != {"root", "digest"}
        or not isinstance(source_identity.get("root"), str)
        or not isinstance(source_identity.get("digest"), str)
    ):
        raise SandboxError("release schema-memory source identity is invalid")
    expected, binding = inspect_release_inputs(
        args,
        policy=policy,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        prove_git=prove_git,
        schema_memory_source=Path(source_identity["root"]),
    )
    comparable_fields = (
        "policy_digest",
        "release_plan",
        "provenance",
        "canonical_environment",
        "canonical_environment_digest",
        "model_identity",
        "model_identity_digest",
        "evaluator_identities",
        "inputs",
        "case_manifests",
        "schema_memory_source",
    )
    if any(lock.get(field) != expected.get(field) for field in comparable_fields):
        raise SandboxError("release input lock does not match current frozen inputs")
    lock_without_digest = dict(lock)
    stored_digest = lock_without_digest.pop("lock_digest", None)
    if stored_digest != json_digest(lock_without_digest):
        raise SandboxError("release input lock digest mismatch")
    return binding
