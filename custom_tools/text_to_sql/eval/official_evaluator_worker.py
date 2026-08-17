"""Container worker that invokes pinned official evaluator source unchanged."""

from __future__ import annotations

import argparse
import ast
import json
import multiprocessing
import os
from pathlib import Path
import runpy
import subprocess
import sys
from typing import Mapping, Sequence

try:
    from .official_evaluator_contracts import (
        BIRD_CASE_COUNT,
        BIRD_DIFFICULTY_JSONL_SHA256,
        RAW_FREEZE_SHA256,
        SPIDER_CASE_COUNT,
        load_bird_task_jsonl,
        sha256_bytes,
        validate_bird_predictions,
    )
except ImportError:  # Executed as a mounted standalone worker.
    from official_evaluator_contracts import (  # type: ignore[no-redef]
        BIRD_CASE_COUNT,
        BIRD_DIFFICULTY_JSONL_SHA256,
        RAW_FREEZE_SHA256,
        SPIDER_CASE_COUNT,
        load_bird_task_jsonl,
        sha256_bytes,
        validate_bird_predictions,
    )


def validate_bird_results(rows: object) -> list[dict[str, int]]:
    if not isinstance(rows, list) or len(rows) != BIRD_CASE_COUNT:
        raise ValueError("BIRD result must contain exactly 500 rows")
    normalized: list[dict[str, int]] = []
    for index, row in enumerate(rows):
        score = row.get("res") if isinstance(row, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or row.get("sql_idx") != index
            or not isinstance(score, int)
            or isinstance(score, bool)
            or score not in {0, 1}
        ):
            raise ValueError("BIRD result rows must be ordered binary scores")
        normalized.append({"sql_idx": index, "res": int(row["res"])})
    return normalized


def validate_spider_results(
    rows: object,
    expected_keys: Sequence[str],
) -> list[dict[str, object]]:
    if len(expected_keys) != SPIDER_CASE_COUNT or len(set(expected_keys)) != SPIDER_CASE_COUNT:
        raise ValueError("Spider expected keys must contain exactly 135 IDs")
    if not isinstance(rows, list) or len(rows) != SPIDER_CASE_COUNT:
        raise ValueError("Spider result keys must contain exactly 135 rows")
    normalized: list[dict[str, object]] = []
    for row in rows:
        score = row.get("score") if isinstance(row, Mapping) else None
        if (
            not isinstance(row, Mapping)
            or not isinstance(score, int)
            or isinstance(score, bool)
            or score not in {0, 1}
        ):
            raise ValueError("Spider result rows must contain binary scores")
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str):
            raise ValueError("Spider result row has no instance ID")
        normalized.append(dict(row))
    if [row["instance_id"] for row in normalized] != sorted(expected_keys):
        raise ValueError("Spider result keys do not match the exact selected set")
    return normalized


def _printed_spider_scores(log_path: Path, expected_keys: Sequence[str]) -> dict[str, int]:
    expected = set(expected_keys)
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            value = ast.literal_eval(line.strip())
        except (SyntaxError, ValueError):
            continue
        if isinstance(value, dict) and set(value) == expected and set(value.values()) <= {0, 1}:
            return {str(key): int(score) for key, score in value.items()}
    raise ValueError("Spider official printed score mapping is missing")


def run_spider_source(
    entrypoint: Path,
    *,
    result_dir: Path,
    gold_dir: Path,
    temp_dir: Path,
    work_dir: Path,
    expected_keys: Sequence[str],
) -> list[dict[str, object]]:
    result_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    previous_cwd = Path.cwd()
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    tee = None
    try:
        os.chdir(work_dir)
        namespace = runpy.run_path(str(entrypoint), run_name="official_spider_evaluator")
        tee = sys.stdout
        args = argparse.Namespace(
            mode="sql",
            result_dir=str(result_dir),
            gold_dir=str(gold_dir),
            is_sql_debug=False,
            max_workers=20,
            timeout=60,
            expected_keys=list(expected_keys),
        )
        rows = namespace["evaluate_spider2sql"](args, temp_dir)
    finally:
        if tee is None and sys.stdout is not previous_stdout:
            tee = sys.stdout
        if tee is not None:
            tee.flush()
            close = getattr(tee, "close", None)
            if callable(close):
                close()
        sys.stdout, sys.stderr = previous_stdout, previous_stderr
        os.chdir(previous_cwd)
    normalized = validate_spider_results(rows, expected_keys)
    printed = _printed_spider_scores(work_dir / "log.txt", expected_keys)
    returned = {str(row["instance_id"]): int(row["score"]) for row in normalized}
    if returned != printed:
        raise ValueError("Spider returned and printed scores differ")
    return normalized


def run_bird_source(entrypoint: Path, argv: Sequence[str]) -> list[dict[str, int]]:
    if __name__ != "__main__":
        raise RuntimeError("BIRD official source requires the real worker __main__")
    if multiprocessing.get_context().get_start_method() != "fork":
        raise RuntimeError("BIRD official source requires multiprocessing fork")
    previous_argv = sys.argv
    previous_cwd = Path.cwd()
    evaluation_dir = entrypoint.parent
    sys.path.insert(0, str(evaluation_dir))
    try:
        os.chdir(evaluation_dir)
        sys.argv = [str(entrypoint), *argv]
        source = entrypoint.read_bytes()
        exec(compile(source, str(entrypoint), "exec"), globals(), globals())
        return validate_bird_results(globals().get("exec_result"))
    finally:
        sys.argv = previous_argv
        os.chdir(previous_cwd)
        sys.path.remove(str(evaluation_dir))


def runtime_freeze_sha256() -> str:
    result = subprocess.run(
        ["/opt/evaluator-venv/bin/pip", "freeze"],
        check=True,
        capture_output=True,
    )
    digest = sha256_bytes(result.stdout)
    if digest != RAW_FREEZE_SHA256:
        raise RuntimeError("official evaluator runtime freeze mismatch")
    return digest


def _write_json(path: Path, value: object) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"official evaluator output already exists: {path}")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _bird(args: argparse.Namespace) -> list[dict[str, int]]:
    validate_bird_predictions(args.predictions)
    difficulty = load_bird_task_jsonl(args.task)
    if sha256_bytes(difficulty) != BIRD_DIFFICULTY_JSONL_SHA256:
        raise ValueError("BIRD canonical difficulty JSONL digest mismatch")
    args.difficulty_jsonl.write_bytes(difficulty)
    return run_bird_source(
        args.entrypoint,
        [
            "--db_root_path", str(args.database_root) + "/",
            "--predicted_sql_path", str(args.predictions),
            "--ground_truth_path", str(args.gold_sql),
            "--num_cpus", "16",
            "--output_log_path", str(args.source_log),
            "--diff_json_path", str(args.difficulty_jsonl),
            "--meta_time_out", "30.0",
            "--sql_dialect", "SQLite",
        ],
    )


def _spider(args: argparse.Namespace) -> list[dict[str, object]]:
    keys = json.loads(args.expected_keys.read_text(encoding="utf-8"))
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValueError("Spider expected key file is invalid")
    return run_spider_source(
        args.entrypoint,
        result_dir=args.predictions,
        gold_dir=args.gold_dir,
        temp_dir=args.temp_dir,
        work_dir=args.work_dir,
        expected_keys=keys,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("benchmark", choices=("bird", "spider"))
    parser.add_argument("--entrypoint", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--raw-results", type=Path, required=True)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--database-root", type=Path)
    parser.add_argument("--gold-sql", type=Path)
    parser.add_argument("--difficulty-jsonl", type=Path)
    parser.add_argument("--source-log", type=Path)
    parser.add_argument("--gold-dir", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--expected-keys", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    before = runtime_freeze_sha256()
    rows = _bird(args) if args.benchmark == "bird" else _spider(args)
    after = runtime_freeze_sha256()
    if before != after:
        raise RuntimeError("official evaluator runtime changed during evaluation")
    _write_json(args.raw_results, {"freeze_before": before, "freeze_after": after, "results": rows})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
