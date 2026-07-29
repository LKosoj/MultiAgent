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


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from streamlit_app.text_to_sql_client import (  # noqa: E402
    TERMINAL_RUN_STATUSES,
    TextToSqlApiClient,
    TextToSqlRunRequest,
)


EMPTY_PREDICTION = "SELECT 1 WHERE 0"
TOKEN_ENV = "TEXT2SQL_BENCHMARK_TOKEN"


@dataclass(frozen=True)
class BenchmarkCase:
    ordinal: int
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
    payload = json.loads(task_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{task_path}: expected a list")

    cases: list[BenchmarkCase] = []
    for ordinal, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"{task_path}: item {ordinal} is not an object")
        database_id = str(row["db_id"])
        database_path = (
            dataset_root
            / "dev_databases"
            / database_id
            / f"{database_id}.sqlite"
        ).resolve()
        _require_database(database_path, database_id)
        cases.append(
            BenchmarkCase(
                ordinal=ordinal,
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
        database_path = (sqlite_root / f"{database_id}.sqlite").resolve()
        _require_database(database_path, database_id)
        document_name = str(row.get("external_knowledge") or "").strip()
        external_knowledge = ""
        if document_name:
            document_path = (documents_root / document_name).resolve()
            if not document_path.is_relative_to(documents_root.resolve()):
                raise ValueError(f"external knowledge escapes document root: {case_id}")
            external_knowledge = document_path.read_text(encoding="utf-8")
        cases.append(
            BenchmarkCase(
                ordinal=len(cases),
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
    if not path.is_file() or path.stat().st_size < 1024:
        raise ValueError(f"SQLite database is missing or invalid: {database_id}: {path}")


def _select_cases(
    cases: Sequence[BenchmarkCase],
    *,
    limit: int | None,
    case_ids: set[str],
) -> list[BenchmarkCase]:
    selected = [case for case in cases if not case_ids or case.case_id in case_ids]
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


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _load_completed(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"{path}: observation without case_id")
        if case_id in completed:
            raise ValueError(f"{path}: duplicate observation for {case_id}")
        completed[case_id] = row
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
    prompt = case.prompt()
    run_id: str | None = None
    terminal_status: str | None = None
    try:
        client = _client(base_url, token)
        handle = client.start(
            TextToSqlRunRequest(
                query=prompt,
                connection_ref=connection_ref,
                idempotency_key=_idempotency_key(
                    benchmark_name,
                    case.case_id,
                    prompt,
                    connection_ref,
                ),
                max_rows=max_rows,
                safety_level="strict",
                include_explanation=True,
                validate_schema=True,
                dry_run_only=False,
                use_schema_suggestions=True,
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
        if terminal_status != "finished":
            raise RuntimeError(
                f"workflow ended with status={terminal_status}: {status_error or ''}"
            )
        result = client.get_result(handle.run_id)
        outcome = asdict(result)
        outcome.pop("final_output", None)
        outcome.pop("raw", None)
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
        "case_id": case.case_id,
        "database_id": case.database_id,
        "difficulty": case.difficulty,
        "question": case.question,
        "external_knowledge_sha256": (
            hashlib.sha256(case.external_knowledge.encode("utf-8")).hexdigest()
            if case.external_knowledge
            else None
        ),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "run_id": run_id,
        "workflow_status": terminal_status,
        "observation_status": observation_status,
        "runner_error": runner_error,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "outcome": outcome,
    }


def run_benchmark(args: argparse.Namespace) -> int:
    token = os.getenv(TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is required")
    cases = _load_cases(args)
    cases = _select_cases(
        cases,
        limit=args.limit,
        case_ids=set(args.case_id),
    )
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = output_dir / "observations.jsonl"
    completed = _load_completed(observations_path)
    pending = [case for case in cases if case.case_id not in completed]

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


def _write_manifest(
    *,
    args: argparse.Namespace,
    cases: Sequence[BenchmarkCase],
    output_dir: Path,
    principal: Mapping[str, Any],
    completed_before: int,
) -> None:
    source_paths = _source_paths(args)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": args.dataset,
        "case_count": len(cases),
        "completed_before": completed_before,
        "repo_revision": _git_revision(),
        "base_url": args.base_url,
        "workers": args.workers,
        "case_timeout": args.case_timeout,
        "max_rows": args.max_rows,
        "model_route": "model_code",
        "model_id": "llmgateway/qwen3.5",
        "model_temperature": 0.7,
        "successful_sql_memory_enabled": os.getenv(
            "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED",
            "1",
        ),
        "principal": {
            "subject": principal.get("subject"),
            "tenant_id": principal.get("tenant_id"),
            "roles": principal.get("roles"),
        },
        "sources": [
            {
                "path": str(path),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for path in source_paths
        ],
    }
    target = output_dir / "manifest.json"
    target.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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
        observation = observations.get(case.case_id, {})
        outcome = observation.get("outcome")
        sql = outcome.get("sql") if isinstance(outcome, dict) else None
        sql_by_id[case.case_id] = sql.strip() if isinstance(sql, str) and sql.strip() else EMPTY_PREDICTION

    if dataset == "bird":
        predictions = {
            str(case.ordinal): (
                f"{sql_by_id[case.case_id]}\t----- bird -----\t{case.database_id}"
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
            sql_by_id[case.case_id] + "\n",
            encoding="utf-8",
        )


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("bird", "spider"), required=True)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--sqlite-root", type=Path)
    parser.add_argument("--database-map", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--workers", type=_positive_int, default=4)
    parser.add_argument("--case-timeout", type=float, default=600.0)
    parser.add_argument("--max-rows", type=_positive_int, default=100)
    parser.add_argument("--limit", type=_positive_int)
    parser.add_argument("--case-id", action="append", default=[])
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
