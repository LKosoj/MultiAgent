#!/usr/bin/env python3
"""Run public SQLite Text-to-SQL cases through the authenticated AG-UI API.

The runner deliberately keeps benchmark gold SQL out of API requests. It writes
one append-only observation per completed case and can resume from that file.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Iterable, Mapping, Sequence
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from streamlit_app.text_to_sql_client import (  # noqa: E402
    TERMINAL_RUN_STATUSES,
    TextToSqlApiClient,
    TextToSqlRunRequest,
)
from custom_tools.text_to_sql.eval.sandbox import (  # noqa: E402
    BwrapSandboxSpec,
    CANONICAL_RUNTIME_SOURCE_PATHS,
    SandboxCaseRunner,
    SandboxError,
    SourceSnapshot,
    canonical_case_order,
    create_source_snapshot,
    empty_history_receipt,
    resolve_safe_regular_file,
    source_snapshot_manifest,
    validate_canonical_workers,
    validate_execution_mode,
)
from custom_tools.text_to_sql.eval import public_benchmark_release as release_support  # noqa: E402
from scripts import text2sql_benchmark_reporting as benchmark_reporting  # noqa: E402


__all__ = [
    "BwrapSandboxSpec",
    "CANONICAL_RUNTIME_SOURCE_PATHS",
    "SandboxCaseRunner",
    "SourceSnapshot",
    "create_source_snapshot",
    "empty_history_receipt",
    "source_snapshot_manifest",
    "validate_canonical_workers",
    "uuid",
]


EMPTY_PREDICTION = "/* TEXT2SQL_NO_PREDICTION */ INVALID SQL"
TOKEN_ENV = "TEXT2SQL_BENCHMARK_TOKEN"
CONFIGURATION_PATHS = (
    Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
    Path("config/pii/categories.yaml"),
    Path("config/text_to_sql/adaptive.yaml"),
    Path("config/text_to_sql/column_aliases.yaml"),
    Path("config/text_to_sql/joins.yaml"),
    Path("config/text_to_sql/llm_models.yaml"),
    Path("config/text_to_sql/main_table_scoring.yaml"),
    Path("config/text_to_sql/nlu_morphemes.yaml"),
    Path("config/text_to_sql/public_benchmark_release_policy.json"),
    Path("config/text_to_sql/safety.yaml"),
    Path("config/text_to_sql/significance.yaml"),
    Path("config/text_to_sql/similarity_thresholds.yaml"),
    Path("config/text_to_sql/state_schema.yaml"),
    Path("config/text_to_sql/type_categories.yaml"),
    Path("agent_profiles/schema_research_agent.yaml"),
    Path("agent_profiles/sql_solver_agent.yaml"),
)
CANONICAL_CASE_COUNTS = {"bird": 500, "spider": 135}
CANONICAL_RELEASE_DATASET_ORDER = release_support.CANONICAL_RELEASE_DATASET_ORDER
RELEASE_POLICY_PATH = REPO_ROOT / "config/text_to_sql/public_benchmark_release_policy.json"
EARLY_STOP_AWAITING_DECISION = 2
_SCHEMA_ABSTENTION_REASONS = frozenset(
    {
        "SCHEMA_CLARIFICATION_REQUIRED",
        "SCHEMA_CONTEXT_BUDGET_EXCEEDED",
        "SCHEMA_GROUNDING_FAILED",
        "SCHEMA_LINKING_ERROR",
        "SCHEMA_NOT_AVAILABLE",
        "SCHEMA_UNRESOLVED",
    }
)
_SEMANTIC_EVIDENCE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "terminal_source",
        "root_mechanism",
        "error_class",
        "violated_typed_requirement",
        "pipeline_component",
        "state_sha256",
    }
)
_STAGNATION_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "record_kind",
        "terminal_source",
        "terminal_reason_code",
        "rejection_signatures",
        "state_sha256",
    }
)
_SEMANTIC_REQUIREMENTS = frozenset(
    {
        "required_metric",
        "required_dimension",
        "required_filter",
        "required_ordering",
        "required_limit",
        "required_time",
        "required_formula",
    }
)
_SEMANTIC_EVIDENCE_ROUTES = {
    ("research", "ambiguous"): (
        "RESEARCH_AMBIGUOUS",
        "ambiguous_requirement",
        "adaptive_schema_research",
    ),
    ("research", "unsupported"): (
        "RESEARCH_UNSUPPORTED",
        "unsupported_requirement",
        "adaptive_schema_research",
    ),
    ("solver", "missing_evidence"): (
        "SCHEMA_CLARIFICATION_REQUIRED",
        "missing_evidence",
        "adaptive_sql_solver",
    ),
}


def _validated_semantic_evidence_receipt(
    value: object,
    *,
    outcome_status: object,
    reason_code: object,
) -> dict[str, object] | None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _SEMANTIC_EVIDENCE_RECEIPT_FIELDS
    ):
        return None
    if (
        type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("record_kind") != "text2sql_adaptive_early_stop_evidence"
        or outcome_status != "abstained"
    ):
        return None
    terminal_source = value.get("terminal_source")
    root_mechanism = value.get("root_mechanism")
    if type(terminal_source) is not str or type(root_mechanism) is not str:
        return None
    route = _SEMANTIC_EVIDENCE_ROUTES.get((terminal_source, root_mechanism))
    if route is None or (
        reason_code,
        value.get("error_class"),
        value.get("pipeline_component"),
    ) != route:
        return None
    requirement = value.get("violated_typed_requirement")
    if type(requirement) is not str or requirement not in _SEMANTIC_REQUIREMENTS:
        return None
    state_sha256 = value.get("state_sha256")
    if (
        type(state_sha256) is not str
        or len(state_sha256) != 71
        or not state_sha256.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in state_sha256[7:])
    ):
        return None
    return dict(value)


def _extract_semantic_evidence_receipt(
    final_output: object,
    *,
    outcome_status: object,
    reason_code: object,
) -> dict[str, object] | None:
    if not isinstance(final_output, Mapping):
        return None
    outputs = final_output.get("outputs")
    if not isinstance(outputs, Mapping):
        return None
    return _validated_semantic_evidence_receipt(
        outputs.get("early_stop_semantic_evidence"),
        outcome_status=outcome_status,
        reason_code=reason_code,
    )


def _validated_stagnation_receipt(
    value: object,
    *,
    outcome_status: object,
    reason_code: object,
) -> dict[str, object] | None:
    if (
        not isinstance(value, Mapping)
        or set(value) != _STAGNATION_RECEIPT_FIELDS
        or value.get("schema_version") != 1
        or value.get("record_kind") != "text2sql_research_stagnation_evidence"
        or value.get("terminal_source") != "research"
        or value.get("terminal_reason_code") != "RESEARCH_STAGNATED"
        or outcome_status != "abstained"
        or reason_code != "RESEARCH_STAGNATED"
    ):
        return None
    signatures = value.get("rejection_signatures")
    if (
        not isinstance(signatures, list)
        or not signatures
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(type(part) is str and part for part in item)
            for item in signatures
        )
        or signatures
        != [list(item) for item in sorted({tuple(item) for item in signatures})]
    ):
        return None
    state_sha256 = value.get("state_sha256")
    if (
        type(state_sha256) is not str
        or len(state_sha256) != 71
        or not state_sha256.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in state_sha256[7:])
    ):
        return None
    return dict(value)


def _extract_stagnation_receipt(
    final_output: object,
    *,
    outcome_status: object,
    reason_code: object,
) -> dict[str, object] | None:
    if not isinstance(final_output, Mapping):
        return None
    outputs = final_output.get("outputs")
    if not isinstance(outputs, Mapping):
        return None
    return _validated_stagnation_receipt(
        outputs.get("early_stop_stagnation_evidence"),
        outcome_status=outcome_status,
        reason_code=reason_code,
    )


@dataclass(frozen=True)
class BenchmarkCase:
    ordinal: int
    case_key: str
    case_id: str
    database_id: str
    database_path: Path
    question: str
    external_knowledge: str
    difficulty: str | None

    def prompt(self) -> str:
        return benchmark_prompt(self.question, self.external_knowledge)


def benchmark_prompt(question: str, external_knowledge: str) -> str:
    normalized_question = question.strip()
    normalized_knowledge = external_knowledge.strip()
    if not normalized_knowledge:
        return normalized_question
    return (
        f"{normalized_question}\n\n"
        "External knowledge supplied by the benchmark:\n"
        f"{normalized_knowledge}"
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            value = json.loads(raw_line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            rows.append(value)
    return rows


def load_bird_cases(dataset_root: Path) -> list[BenchmarkCase]:
    dataset_root = dataset_root.resolve()
    task_path = dataset_root / "mini_dev_sqlite.json"
    resolve_safe_regular_file(task_path, label="BIRD task file")
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{task_path}: expected a list")

    cases: list[BenchmarkCase] = []
    for ordinal, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"{task_path}: item {ordinal} is not an object")
        database_id = str(row["db_id"])
        database_path = resolve_safe_regular_file(
            dataset_root
            / "dev_databases"
            / database_id
            / f"{database_id}.sqlite",
            label="database",
        )
        _require_database(database_path, database_id)
        cases.append(
            BenchmarkCase(
                ordinal=ordinal,
                case_key=f"bird:{ordinal}",
                case_id=str(row["question_id"]),
                database_id=database_id,
                database_path=database_path,
                question=str(row["question"]),
                external_knowledge=str(row.get("evidence") or ""),
                difficulty=str(row.get("difficulty") or "") or None,
            )
        )
    return cases


def load_spider_cases(
    dataset_root: Path,
    sqlite_root: Path,
    database_map_path: Path,
) -> list[BenchmarkCase]:
    dataset_root = dataset_root.resolve()
    sqlite_root = sqlite_root.resolve()
    task_path = dataset_root / "spider2-lite.jsonl"
    resolve_safe_regular_file(task_path, label="Spider task file")
    resolve_safe_regular_file(database_map_path, label="database map")
    documents_root = dataset_root / "resource" / "documents"
    task_rows = _read_jsonl(task_path)
    database_map = json.loads(database_map_path.read_text(encoding="utf-8"))
    if not isinstance(database_map, dict):
        raise ValueError(f"{database_map_path}: expected an object")

    cases: list[BenchmarkCase] = []
    for row in task_rows:
        case_id = str(row["instance_id"])
        if not case_id.startswith("local"):
            continue
        try:
            database_id = str(database_map[case_id])
        except KeyError as exc:
            raise ValueError(f"missing SQLite mapping for {case_id}") from exc
        database_path = resolve_safe_regular_file(
            sqlite_root / f"{database_id}.sqlite",
            label="database",
        )
        _require_database(database_path, database_id)
        document_name = str(row.get("external_knowledge") or "").strip()
        external_knowledge = ""
        if document_name:
            raw_document_path = documents_root / document_name
            document_path = resolve_safe_regular_file(
                raw_document_path,
                label="Spider document",
            )
            if not document_path.is_relative_to(documents_root.resolve()):
                raise ValueError(f"external knowledge escapes document root: {case_id}")
            external_knowledge = document_path.read_text(encoding="utf-8")
        cases.append(
            BenchmarkCase(
                ordinal=len(cases),
                case_key=case_id,
                case_id=case_id,
                database_id=database_id,
                database_path=database_path,
                question=str(row["question"]),
                external_knowledge=external_knowledge,
                difficulty=None,
            )
        )
    return cases


def _require_database(path: Path, database_id: str) -> None:
    path = resolve_safe_regular_file(path, label="database")
    if path.stat().st_size < 1024:
        raise ValueError(f"SQLite database is missing or invalid: {database_id}: {path}")


def _select_cases(
    cases: Sequence[BenchmarkCase],
    *,
    limit: int | None,
    case_ids: set[str],
    ordinal_start: int | None,
    ordinal_stop: int | None,
) -> list[BenchmarkCase]:
    selected = [
        case
        for case in cases
        if (not case_ids or case.case_id in case_ids)
        and (ordinal_start is None or case.ordinal >= ordinal_start)
        and (ordinal_stop is None or case.ordinal < ordinal_stop)
    ]
    if case_ids:
        missing = sorted(case_ids - {case.case_id for case in selected})
        if missing:
            raise ValueError("unknown case ids: " + ", ".join(missing))
    return selected[:limit] if limit is not None else selected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _files_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_release_policy() -> dict[str, object]:
    if RELEASE_POLICY_PATH.is_symlink() or not RELEASE_POLICY_PATH.is_file():
        raise SandboxError("canonical benchmark release policy is missing or unsafe")
    payload = json.loads(RELEASE_POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SandboxError("canonical benchmark release policy must be an object")
    if payload.get("record_kind") != "text2sql_public_benchmark_release_policy":
        raise SandboxError("canonical benchmark release policy kind is invalid")
    return payload


_build_release_plan = release_support.build_release_plan
_canonical_runtime_environment = release_support.canonical_runtime_environment
_release_dataset_policy = release_support.release_dataset_policy
_stable_case_manifest = release_support.stable_case_manifest


def _verified_git_provenance(
    path: Path,
    *,
    origin: str,
    revision: str,
) -> Mapping[str, object]:
    return release_support.verified_git_provenance(
        path,
        origin=origin,
        revision=revision,
    )


def _create_release_input_lock(
    args: argparse.Namespace,
    *,
    policy: Mapping[str, object],
) -> dict[str, object]:
    lock, _binding = release_support.inspect_release_inputs(
        args,
        policy=policy,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        prove_git=_verified_git_provenance,
    )
    return lock


def _validate_release_input_lock(
    args: argparse.Namespace,
    lock: Mapping[str, object],
    *,
    policy: Mapping[str, object],
) -> release_support.FrozenReleaseInputs:
    return release_support.validate_release_input_lock(
        args,
        lock,
        policy=policy,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        prove_git=_verified_git_provenance,
    )


def _configuration_sources(root: Path = REPO_ROOT) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for relative_path in CONFIGURATION_PATHS:
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"runtime configuration is missing or unsafe: {relative_path}")
        content = path.read_bytes()
        records.append(
            {
                "path": str(relative_path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        )
    return records


def _ordered_canonical_cases(
    cases: Sequence[BenchmarkCase],
    *,
    seed: int,
) -> list[BenchmarkCase]:
    by_key = {case.case_key: case for case in cases}
    if len(by_key) != len(cases):
        raise SandboxError("canonical case list contains duplicate case_key")
    return [
        by_key[case_key]
        for case_key in canonical_case_order(list(by_key), seed=seed)
    ]


def _observation_key(row: Mapping[str, Any]) -> str:
    case_key = row.get("case_key")
    if isinstance(case_key, str) and case_key:
        return case_key
    if row.get("benchmark") == "bird" and isinstance(row.get("ordinal"), int):
        return f"bird:{row['ordinal']}"
    case_id = row.get("case_id")
    if isinstance(case_id, str) and case_id:
        return case_id
    raise ValueError("observation has no stable case identity")


def _load_completed(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        try:
            case_key = _observation_key(row)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        if case_key in completed:
            raise ValueError(f"{path}: duplicate observation for {case_key}")
        completed[case_key] = row
    return completed


class ObservationWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, observation: Mapping[str, Any]) -> None:
        encoded = (
            json.dumps(
                observation,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with self._lock:
            with self.path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())


def _client(base_url: str, token: str) -> TextToSqlApiClient:
    return TextToSqlApiClient(
        base_url=base_url,
        auth_headers=lambda: {"Authorization": f"Bearer {token}"},
        poll_interval_seconds=1.0,
        max_poll_attempts=1200,
    )


def _sqlite_dsn(path: Path) -> str:
    return f"sqlite://{path.resolve()}"


def _idempotency_key(
    benchmark_name: str,
    case_id: str,
    prompt: str,
    connection_ref: str,
) -> str:
    request_identity = hashlib.sha256(
        json.dumps(
            {
                "benchmark": benchmark_name,
                "case_id": case_id,
                "prompt": prompt,
                "connection_ref": connection_ref,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"public-{benchmark_name}-{case_id}-{request_identity}"


def _register_databases(
    client: TextToSqlApiClient,
    cases: Sequence[BenchmarkCase],
    *,
    benchmark_name: str,
) -> dict[str, str]:
    refs: dict[str, str] = {}
    unique_cases = {case.database_id: case for case in cases}
    for database_id in sorted(unique_cases):
        case = unique_cases[database_id]
        connection = client.register_connection(
            display_name=f"{benchmark_name}:{database_id}",
            dsn=_sqlite_dsn(case.database_path),
            owner_subject="text2sql-benchmark",
            tenant_id="text2sql-benchmark",
            enabled_for_user=True,
        )
        refs[database_id] = connection.connection_ref
    return refs


def _free_local_port() -> int:
    return _release_bwrap_module()._free_local_port()  # type: ignore[attr-defined,no-any-return]


def _sandbox_runtime_env(args: argparse.Namespace) -> dict[str, str]:
    return _release_bwrap_module()._sandbox_runtime_env(args)  # type: ignore[attr-defined,no-any-return]


def _run_case_in_sandbox(*args: object, **kwargs: object) -> tuple[dict[str, Any], dict[str, object]]:
    return _release_bwrap_module()._run_case_in_sandbox(*args, **kwargs)  # type: ignore[attr-defined,no-any-return]


def _verify_database_digest(*args: object, **kwargs: object) -> None:
    _release_bwrap_module()._verify_database_digest(*args, **kwargs)  # type: ignore[attr-defined]


def _runtime_evidence(*args: object, **kwargs: object) -> dict[str, Any]:
    return _release_bwrap_module()._runtime_evidence(*args, **kwargs)  # type: ignore[attr-defined,no-any-return]

def _run_case(
    case: BenchmarkCase,
    *,
    benchmark_name: str,
    base_url: str,
    token: str,
    connection_ref: str,
    timeout_seconds: float,
    max_rows: int,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    request_identity = json.dumps(
        {
            "external_knowledge": case.external_knowledge,
            "question": case.question,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    run_id: str | None = None
    terminal_status: str | None = None
    try:
        client = _client(base_url, token)
        handle = client.start(
            TextToSqlRunRequest(
                query=case.question,
                connection_ref=connection_ref,
                context_documents=(case.external_knowledge,)
                if case.external_knowledge
                else (),
                idempotency_key=_idempotency_key(
                    benchmark_name,
                    case.case_key,
                    request_identity,
                    connection_ref,
                ),
                max_rows=max_rows,
                safety_level="strict",
                include_explanation=True,
                validate_schema=True,
                dry_run_only=False,
                enable_telemetry=True,
            )
        )
        run_id = handle.run_id
        deadline = time.monotonic() + timeout_seconds
        status_error: str | None = None
        while True:
            status = client.get_run(handle.run_id)
            terminal_status = status.status
            status_error = status.error
            if status.status in TERMINAL_RUN_STATUSES:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run exceeded {timeout_seconds:.0f}s")
            time.sleep(1.0)
        try:
            result = client.get_result(handle.run_id)
        except Exception as exc:
            if terminal_status != "finished":
                raise RuntimeError(
                    f"workflow ended with status={terminal_status}: "
                    f"{status_error or exc}"
                ) from exc
            raise
        outcome = asdict(result)
        semantic_evidence_receipt = _extract_semantic_evidence_receipt(
            result.final_output,
            outcome_status=result.status,
            reason_code=result.reason_code,
        )
        stagnation_receipt = _extract_stagnation_receipt(
            result.final_output,
            outcome_status=result.status,
            reason_code=result.reason_code,
        )
        outcome.pop("final_output", None)
        outcome.pop("raw", None)
        if semantic_evidence_receipt is not None:
            outcome["semantic_evidence_receipt"] = semantic_evidence_receipt
        if stagnation_receipt is not None:
            outcome["stagnation_receipt"] = stagnation_receipt
        observation_status = "completed"
        runner_error = None
    except Exception as exc:
        outcome = None
        observation_status = "runner_error"
        runner_error = f"{type(exc).__name__}: {exc}"

    finished_at = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "benchmark": benchmark_name,
        "ordinal": case.ordinal,
        "case_key": case.case_key,
        "case_id": case.case_id,
        "database_id": case.database_id,
        "difficulty": case.difficulty,
        "question": case.question,
        "external_knowledge_sha256": (
            hashlib.sha256(case.external_knowledge.encode("utf-8")).hexdigest()
            if case.external_knowledge
            else None
        ),
        "prompt_sha256": hashlib.sha256(request_identity.encode("utf-8")).hexdigest(),
        "run_id": run_id,
        "workflow_status": terminal_status,
        "observation_status": observation_status,
        "runner_error": runner_error,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "outcome": outcome,
    }


_require_release_arguments = release_support.require_release_arguments
_load_release_input_lock = release_support.load_release_input_lock
_release_bundle_identity = release_support.release_bundle_identity
_release_leg_args = release_support.release_leg_args


def _write_release_input_lock(args: argparse.Namespace) -> int:
    _require_release_arguments(args)
    target = args.create_release_lock
    policy = _load_release_policy()
    lock = _create_release_input_lock(args, policy=policy)
    lock_changed = False
    policy_path = getattr(args, "early_stop_policy", None)
    if policy_path is not None:
        if policy_path.is_symlink() or not policy_path.is_file():
            raise SandboxError("early-stop policy is missing or unsafe")
        raw_policy = json.loads(policy_path.read_text(encoding="utf-8"))
        benchmark_reporting.parse_early_stop_policy(raw_policy)
        lock["early_stop_policy"] = raw_policy
        lock_changed = True
        lock["early_stop_policy_sha256"] = (
            "sha256:" + hashlib.sha256(policy_path.read_bytes()).hexdigest()
        )
    if getattr(args, "post_repeat_evaluation_barrier", False):
        lock["post_repeat_evaluation_barrier"] = True
        lock_changed = True
    if lock_changed:
        lock_without_digest = dict(lock)
        lock_without_digest.pop("lock_digest", None)
        lock["lock_digest"] = _json_digest(lock_without_digest)
    release_support.write_release_input_lock_new(target, lock)
    print(f"release input lock written: {target}", flush=True)
    return 0


def _run_release_bundle(args: argparse.Namespace, token: str) -> int:
    return release_support.run_release_bundle(
        args,
        token,
        repo_root=REPO_ROOT,
        policy_path=RELEASE_POLICY_PATH,
        configuration_paths=CONFIGURATION_PATHS,
        load_bird_cases=load_bird_cases,
        load_spider_cases=load_spider_cases,
        run_leg=_run_bwrap_benchmark,
        prove_git=_verified_git_provenance,
    )

def run_benchmark(args: argparse.Namespace) -> int:
    if getattr(args, "create_release_lock", None) is not None:
        return _write_release_input_lock(args)
    token = os.getenv(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is required")
    if getattr(args, "release_lock", None) is not None:
        return _run_release_bundle(args, token)
    if args.output_dir is None:
        raise ValueError("diagnostic mode requires --output-dir")
    if args.execution_mode == "bwrap":
        return _run_bwrap_benchmark(args, token)
    validate_execution_mode(args.execution_mode)
    if args.dataset is None or args.dataset_root is None:
        raise ValueError("diagnostic mode requires --dataset and --dataset-root")
    cases = _load_cases(args)
    cases = _select_cases(
        cases,
        limit=args.limit,
        case_ids=set(args.case_id),
        ordinal_start=args.ordinal_start,
        ordinal_stop=args.ordinal_stop,
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = output_dir / "observations.jsonl"
    completed = _load_completed(observations_path)
    pending = [case for case in cases if case.case_key not in completed]

    client = _client(args.base_url, token)
    principal = dict(client.get_me())
    if "admin" not in principal.get("roles", []):
        raise RuntimeError("benchmark API principal must have the admin role")
    connection_refs = _register_databases(
        client,
        pending or cases,
        benchmark_name=args.dataset,
    )
    _write_manifest(
        args=args,
        cases=cases,
        output_dir=output_dir,
        principal=principal,
        completed_before=len(completed),
        manifest_profile="remote_diagnostic_v1",
    )

    writer = ObservationWriter(observations_path)
    if not pending:
        print(f"{args.dataset}: nothing to do; {len(cases)} cases already completed")
        export_predictions(args.dataset, cases, observations_path, output_dir)
        return 0

    print(
        f"{args.dataset}: running {len(pending)} cases "
        f"({len(completed)} already present), workers={args.workers}",
        flush=True,
    )
    failures = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _run_case,
                case,
                benchmark_name=args.dataset,
                base_url=args.base_url,
                token=token,
                connection_ref=connection_refs[case.database_id],
                timeout_seconds=args.case_timeout,
                max_rows=args.max_rows,
            ): case
            for case in pending
        }
        for completed_count, future in enumerate(as_completed(futures), start=1):
            observation = future.result()
            writer.append(observation)
            if observation["observation_status"] != "completed":
                failures += 1
            print(
                f"[{completed_count}/{len(pending)}] "
                f"{observation['case_id']} {observation['observation_status']} "
                f"{observation['elapsed_seconds']}s",
                flush=True,
            )

    export_predictions(args.dataset, cases, observations_path, output_dir)
    print(f"{args.dataset}: completed={len(pending) - failures} runner_errors={failures}")
    return 1 if failures else 0


def _release_bwrap_module() -> object:
    from custom_tools.text_to_sql.eval import public_benchmark_bwrap

    return public_benchmark_bwrap


def _run_bwrap_benchmark(args: argparse.Namespace, token: str) -> int:
    return _release_bwrap_module()._run_bwrap_benchmark(args, token)  # type: ignore[attr-defined,no-any-return]


def _canonical_run_scope(args: argparse.Namespace, *, case_count: int) -> str:
    return _release_bwrap_module()._canonical_run_scope(args, case_count=case_count)  # type: ignore[attr-defined,no-any-return]


def _create_canonical_output_dir(path: Path) -> Path:
    return _release_bwrap_module()._create_canonical_output_dir(path)  # type: ignore[attr-defined,no-any-return]


def _write_case_manifest(*args: object, **kwargs: object) -> tuple[str, dict[str, str]]:
    return _release_bwrap_module()._write_case_manifest(*args, **kwargs)  # type: ignore[attr-defined,no-any-return]


def _write_empty_history_evidence(*args: object, **kwargs: object) -> None:
    _release_bwrap_module()._write_empty_history_evidence(*args, **kwargs)  # type: ignore[attr-defined]


def _validate_release_resume(*args: object, **kwargs: object) -> set[tuple[str, int]]:
    return _release_bwrap_module()._validate_release_resume(*args, **kwargs)  # type: ignore[attr-defined,no-any-return]


def _write_manifest(*args: object, **kwargs: object) -> None:
    _release_bwrap_module()._write_manifest(*args, **kwargs)  # type: ignore[attr-defined]


_write_bundle_state = release_support.write_bundle_state


def _source_paths(args: argparse.Namespace) -> list[Path]:
    if args.dataset == "bird":
        return [args.dataset_root.resolve() / "mini_dev_sqlite.json"]
    return [
        args.dataset_root.resolve() / "spider2-lite.jsonl",
        args.database_map.resolve(),
    ]


def _load_cases(args: argparse.Namespace) -> list[BenchmarkCase]:
    if args.dataset == "bird":
        return load_bird_cases(args.dataset_root)
    if args.sqlite_root is None or args.database_map is None:
        raise ValueError("spider requires --sqlite-root and --database-map")
    return load_spider_cases(
        args.dataset_root,
        args.sqlite_root,
        args.database_map,
    )


def export_predictions(
    dataset: str,
    cases: Sequence[BenchmarkCase],
    observations_path: Path,
    output_dir: Path,
) -> None:
    observations = _load_completed(observations_path)
    sql_by_id: dict[str, str] = {}
    for case in cases:
        observation = observations.get(case.case_key, {})
        outcome = observation.get("outcome")
        sql = (
            outcome.get("sql")
            if isinstance(outcome, dict)
            and outcome.get("status") == "succeeded"
            and outcome.get("executed") is True
            else None
        )
        sql_by_id[case.case_key] = (
            sql.strip()
            if isinstance(sql, str) and sql.strip()
            else EMPTY_PREDICTION
        )

    if dataset == "bird":
        predictions = {
            str(case.ordinal): (
                f"{sql_by_id[case.case_key]}\t----- bird -----\t{case.database_id}"
            )
            for case in cases
        }
        (output_dir / "bird_predictions.json").write_text(
            json.dumps(predictions, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return

    predictions_dir = output_dir / "spider_predictions"
    predictions_dir.mkdir(parents=True, exist_ok=True)
    for case in cases:
        (predictions_dir / f"{case.case_id}.sql").write_text(
            sql_by_id[case.case_key] + "\n",
            encoding="utf-8",
        )


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def _nonnegative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("bird", "spider"))
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--sqlite-root", type=Path)
    parser.add_argument("--database-map", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--execution-mode", choices=("remote", "bwrap"), default="remote")
    parser.add_argument("--repeat-ordinal", type=_positive_int, default=1)
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--diagnostic-subset",
        action="store_true",
        help="Explicitly mark canonical-isolation output as a non-release subset.",
    )
    parser.add_argument("--workers", type=_positive_int, default=4)
    parser.add_argument("--case-timeout", type=float, default=14_400.0)
    parser.add_argument("--max-rows", type=_positive_int, default=100)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--ordinal-start", type=_nonnegative_int)
    parser.add_argument("--ordinal-stop", type=_positive_int)
    parser.add_argument(
        "--pipeline-revision",
        help="Revision deployed by the benchmark API server, if known.",
    )
    parser.add_argument("--sandbox-state-root", type=Path)
    parser.add_argument(
        "--schema-memory-source",
        type=Path,
        help="Schema-memory directory copied once from the previous canonical release.",
    )
    parser.add_argument("--sandbox-secret-dir", type=Path)
    parser.add_argument("--sandbox-venv-root", type=Path, default=REPO_ROOT / ".venv")
    parser.add_argument(
        "--sandbox-env",
        action="append",
        default=[],
        help="Non-secret NAME=VALUE forwarded into the sandbox.",
    )
    release_mode = parser.add_mutually_exclusive_group()
    release_mode.add_argument(
        "--create-release-lock",
        type=Path,
        help="Preflight only: write a complete immutable input lock and exit.",
    )
    release_mode.add_argument(
        "--release-lock",
        type=Path,
        help="Run the complete six-leg canonical release from an existing lock.",
    )
    parser.add_argument(
        "--early-stop-policy",
        type=Path,
        help="Optional closed policy recorded only while creating a release lock.",
    )
    parser.add_argument(
        "--post-repeat-evaluation-barrier",
        action="store_true",
        help=(
            "Pause after each completed repeat until closed evaluator artifacts "
            "and diagnostics are present; recorded only in a new release lock."
        ),
    )
    parser.add_argument("--resume-release", action="store_true")
    parser.add_argument(
        "--repair-decision",
        type=Path,
        help="Immutable reviewed decision bound to an early-stop candidate.",
    )
    parser.add_argument("--bird-root", type=Path)
    parser.add_argument("--spider-root", type=Path)
    parser.add_argument("--spider-sqlite-root", type=Path)
    parser.add_argument("--spider-database-map", type=Path)
    parser.add_argument("--official-evaluator-image-identity", type=Path)
    parser.add_argument("--model-api-base")
    parser.add_argument("--model-backend-id")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.resume_release and args.release_lock is None:
        parser.error("--resume-release requires --release-lock")
    if args.early_stop_policy is not None and args.create_release_lock is None:
        parser.error("--early-stop-policy requires --create-release-lock")
    if args.post_repeat_evaluation_barrier and args.create_release_lock is None:
        parser.error("--post-repeat-evaluation-barrier requires --create-release-lock")
    if args.repair_decision is not None and not args.resume_release:
        parser.error("--repair-decision requires --resume-release")
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
