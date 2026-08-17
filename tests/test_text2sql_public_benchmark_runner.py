from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Mapping

import pytest

import custom_tools.text_to_sql.eval.sandbox as sandbox_module
from custom_tools.text_to_sql.eval import release_inputs
from custom_tools.text_to_sql.eval import release_diagnostics
from custom_tools.text_to_sql.eval import release_bundle_execution as bundle_execution
from custom_tools.text_to_sql.eval import public_benchmark_artifacts
from custom_tools.text_to_sql.eval import public_benchmark_bwrap_execution as bwrap_execution
from custom_tools.text_to_sql.eval.public_benchmark_bwrap_execution import (
    BwrapBenchmarkExecution,
)
from custom_tools.text_to_sql.eval.release_bundle_execution import (
    ReleaseBundleExecution,
)
import scripts.text2sql_public_benchmark as benchmark_runner
from scripts.text2sql_public_benchmark import (
    CANONICAL_RELEASE_DATASET_ORDER,
    EMPTY_PREDICTION,
    _build_release_plan,
    _canonical_runtime_environment,
    _canonical_run_scope,
    _create_release_input_lock,
    _idempotency_key,
    _load_completed,
    _ordered_canonical_cases,
    _run_case,
    _validate_release_resume,
    _select_cases,
    _configuration_sources,
    _create_canonical_output_dir,
    _verify_database_digest,
    _write_case_manifest,
    _write_empty_history_evidence,
    _write_manifest,
    _write_bundle_state,
    _validate_release_input_lock,
    benchmark_prompt,
    export_predictions,
    load_bird_cases,
    load_spider_cases,
)
from streamlit_app.text_to_sql_client import TextToSqlResult
from custom_tools.text_to_sql.eval.sandbox import prepare_case_overlays


def _sqlite_file(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 2048)


def test_load_bird_cases_does_not_put_gold_sql_in_prompt(tmp_path: Path) -> None:
    root = tmp_path / "bird"
    root.mkdir()
    (root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 7,
                    "db_id": "school",
                    "question": "Count the students.",
                    "evidence": "Students are enrolled people.",
                    "difficulty": "simple",
                    "SQL": "SELECT COUNT(*) FROM students",
                }
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(root / "dev_databases" / "school" / "school.sqlite")

    cases = load_bird_cases(root)

    assert len(cases) == 1
    assert cases[0].case_key == "bird:0"
    assert cases[0].case_id == "7"
    assert "SELECT COUNT(*)" not in cases[0].prompt()
    assert cases[0].prompt() == benchmark_prompt(
        "Count the students.",
        "Students are enrolled people.",
    )


def test_load_spider_cases_filters_non_local_and_loads_documents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "spider2-lite"
    documents = root / "resource" / "documents"
    documents.mkdir(parents=True)
    (documents / "rule.md").write_text("Use the documented rule.", encoding="utf-8")
    (root / "spider2-lite.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "instance_id": "bq001",
                        "question": "Remote question",
                        "external_knowledge": "",
                    }
                ),
                json.dumps(
                    {
                        "instance_id": "local001",
                        "question": "Local question",
                        "external_knowledge": "rule.md",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sqlite_root = tmp_path / "sqlite"
    _sqlite_file(sqlite_root / "db.sqlite")
    database_map = tmp_path / "local-map.jsonl"
    database_map.write_text(json.dumps({"local001": "db"}), encoding="utf-8")

    cases = load_spider_cases(root, sqlite_root, database_map)

    assert [case.case_id for case in cases] == ["local001"]
    assert [case.case_key for case in cases] == ["local001"]
    assert "Use the documented rule." in cases[0].prompt()


def test_export_predictions_is_stable_and_uses_failing_fallback(
    tmp_path: Path,
) -> None:
    root = tmp_path / "bird"
    root.mkdir()
    (root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 0,
                    "db_id": "db",
                    "question": "First",
                    "SQL": "gold must stay unused",
                },
                {
                    "question_id": 1,
                    "db_id": "db",
                    "question": "Second",
                    "SQL": "gold must stay unused",
                },
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(root / "dev_databases" / "db" / "db.sqlite")
    cases = load_bird_cases(root)
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        json.dumps(
            {
                "benchmark": "bird",
                "ordinal": 1,
                "case_id": "1",
                "outcome": {
                    "status": "succeeded",
                    "executed": True,
                    "sql": "SELECT 2",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    export_predictions("bird", cases, observations, tmp_path)

    predictions = json.loads((tmp_path / "bird_predictions.json").read_text())
    assert list(predictions) == ["0", "1"]
    assert predictions["0"].startswith(EMPTY_PREDICTION)
    assert predictions["1"].startswith("SELECT 2")
    with sqlite3.connect(":memory:") as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(EMPTY_PREDICTION)


def test_export_predictions_ignores_unexecuted_sql(tmp_path: Path) -> None:
    root = tmp_path / "bird"
    root.mkdir()
    (root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 0,
                    "db_id": "db",
                    "question": "Count rows.",
                    "SQL": "gold must stay unused",
                }
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(root / "dev_databases" / "db" / "db.sqlite")
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        json.dumps(
            {
                "benchmark": "bird",
                "ordinal": 0,
                "case_id": "0",
                "outcome": {
                    "status": "failed",
                    "executed": False,
                    "sql": "SELECT COUNT(*) FROM items",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    export_predictions("bird", load_bird_cases(root), observations, tmp_path)

    predictions = json.loads((tmp_path / "bird_predictions.json").read_text())
    assert predictions["0"].startswith(EMPTY_PREDICTION)


def test_load_completed_uses_bird_ordinal_when_question_ids_repeat(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observations.jsonl"
    observations.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "benchmark": "bird",
                        "ordinal": 0,
                        "case_id": "duplicate",
                    }
                ),
                json.dumps(
                    {
                        "benchmark": "bird",
                        "ordinal": 1,
                        "case_id": "duplicate",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _load_completed(observations)

    assert list(completed) == ["bird:0", "bird:1"]


def test_select_cases_supports_disjoint_ordinal_ranges() -> None:
    cases = [
        benchmark_runner.BenchmarkCase(
            ordinal=ordinal,
            case_key=f"bird:{ordinal}",
            case_id=str(ordinal),
            database_id="db",
            database_path=Path("/tmp/db.sqlite"),
            question=f"Question {ordinal}",
            external_knowledge="",
            difficulty="simple",
        )
        for ordinal in range(5)
    ]

    selected = _select_cases(
        cases,
        limit=None,
        case_ids=set(),
        ordinal_start=1,
        ordinal_stop=4,
    )

    assert [case.ordinal for case in selected] == [1, 2, 3]


def test_runner_uses_seeded_canonical_case_order() -> None:
    cases = [
        benchmark_runner.BenchmarkCase(
            ordinal=ordinal,
            case_key=f"bird:{ordinal}",
            case_id=str(ordinal),
            database_id="db",
            database_path=Path("/tmp/db.sqlite"),
            question=f"Question {ordinal}",
            external_knowledge="",
            difficulty=None,
        )
        for ordinal in range(8)
    ]

    first = _ordered_canonical_cases(cases, seed=17)
    repeated = _ordered_canonical_cases(cases, seed=17)
    different = _ordered_canonical_cases(cases, seed=18)

    assert [case.case_key for case in first] == [case.case_key for case in repeated]
    assert [case.case_key for case in first] != [case.case_key for case in different]


def test_canonical_scope_requires_seed_and_explicit_subset_marker() -> None:
    base = {
        "dataset": "bird",
        "seed": None,
        "limit": None,
        "case_id": [],
        "ordinal_start": None,
        "ordinal_stop": None,
        "diagnostic_subset": False,
    }
    with pytest.raises(ValueError, match="--seed"):
        _canonical_run_scope(SimpleNamespace(**base), case_count=500)

    base.update(seed=7, limit=1)
    with pytest.raises(ValueError, match="--diagnostic-subset"):
        _canonical_run_scope(SimpleNamespace(**base), case_count=1)

    base.update(diagnostic_subset=True)
    assert _canonical_run_scope(SimpleNamespace(**base), case_count=1) == (
        "diagnostic_subset"
    )
    base.update(limit=None, diagnostic_subset=False)
    with pytest.raises(benchmark_runner.SandboxError, match="case count"):
        _canonical_run_scope(SimpleNamespace(**base), case_count=499)
    assert _canonical_run_scope(SimpleNamespace(**base), case_count=500) == (
        "diagnostic_full_dataset"
    )


def test_idempotency_key_changes_when_connection_registration_changes() -> None:
    first = _idempotency_key("bird", "7", "show rows", "conn-first")
    second = _idempotency_key("bird", "7", "show rows", "conn-second")

    assert first != second
    assert first == _idempotency_key("bird", "7", "show rows", "conn-first")


def test_run_case_fetches_terminal_outcome_after_transport_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "database" / "db.sqlite"
    _sqlite_file(database_path)
    case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="7",
        database_id="db",
        database_path=database_path,
        question="Count rows.",
        external_knowledge="",
        difficulty="simple",
    )
    result = TextToSqlResult(
        run_id="run-1",
        status="abstained",
        reason_code="SCHEMA_CLARIFICATION_REQUIRED",
        sql="",
        generated=False,
        approved=False,
        executed=False,
        dry_run=False,
        audited=False,
        rows=[],
        columns=[],
        rows_affected=0,
        error="audit failed",
        execution={},
        audit={},
        persistence={},
        final_output={
            "outputs": {
                "early_stop_semantic_evidence": {
                    "schema_version": 1,
                    "record_kind": "text2sql_adaptive_early_stop_evidence",
                    "terminal_source": "solver",
                    "root_mechanism": "missing_evidence",
                    "error_class": "missing_evidence",
                    "violated_typed_requirement": "required_filter",
                    "pipeline_component": "adaptive_sql_solver",
                    "state_sha256": "sha256:" + "a" * 64,
                }
            }
        },
    )

    class FakeClient:
        def start(self, request: object) -> SimpleNamespace:
            return SimpleNamespace(run_id="run-1")

        def get_run(self, run_id: str) -> SimpleNamespace:
            assert run_id == "run-1"
            return SimpleNamespace(status="errored", error="transport projection")

        def get_result(self, run_id: str) -> TextToSqlResult:
            assert run_id == "run-1"
            return result

    monkeypatch.setattr(
        benchmark_runner,
        "_client",
        lambda base_url, token: FakeClient(),
    )

    observation = _run_case(
        case,
        benchmark_name="bird",
        base_url="http://example.invalid",
        token="token",
        connection_ref="connection",
        timeout_seconds=1,
        max_rows=100,
    )

    assert observation["workflow_status"] == "errored"
    assert observation["observation_status"] == "completed"
    assert observation["runner_error"] is None
    assert observation["outcome"]["reason_code"] == "SCHEMA_CLARIFICATION_REQUIRED"
    assert observation["outcome"]["semantic_evidence_receipt"] == (
        result.final_output["outputs"]["early_stop_semantic_evidence"]
    )
    assert "final_output" not in observation["outcome"]
    assert "raw" not in observation["outcome"]


def test_manifest_records_verifiable_pipeline_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_root = tmp_path / "bird"
    dataset_root.mkdir()
    source = dataset_root / "mini_dev_sqlite.json"
    source.write_text("[]", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setattr(benchmark_runner, "_git_revision", lambda: "runner-revision")
    args = SimpleNamespace(
        dataset="bird",
        dataset_root=dataset_root,
        pipeline_revision="pipeline-revision",
        base_url="http://example.invalid",
        workers=4,
        case_timeout=930.0,
        max_rows=100,
    )

    _write_manifest(
        args=args,
        cases=[],
        output_dir=output_dir,
        principal={
            "subject": "benchmark",
            "tenant_id": "benchmark",
            "roles": ["admin"],
        },
        completed_before=0,
        manifest_profile="remote_diagnostic_v1",
    )

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert set(manifest) == {
        "schema_version",
        "created_at",
        "benchmark",
        "case_count",
        "completed_before",
        "repo_revision",
        "pipeline_revision",
        "base_url",
        "repeat_ordinal",
        "workers",
        "case_timeout",
        "max_rows",
        "model_configuration",
        "configuration_sources",
        "successful_sql_memory_enabled",
        "principal",
        "sources",
    }
    assert manifest["repo_revision"] == "runner-revision"
    assert manifest["pipeline_revision"] == "pipeline-revision"
    assert manifest["model_configuration"]["reported_by_runtime"] is False
    assert "model_id" not in manifest
    assert "model_temperature" not in manifest
    assert {
        item["path"] for item in manifest["configuration_sources"]
    } == {str(path) for path in benchmark_runner.CONFIGURATION_PATHS}


def test_bwrap_manifest_uses_v2_execution_policy_from_source_snapshot(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "source-snapshot"
    for relative_path in (
        Path("config/text_to_sql/adaptive.yaml"),
        Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
    ):
        destination = snapshot_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((benchmark_runner.REPO_ROOT / relative_path).read_bytes())
    snapshot = sandbox_module.SourceSnapshot(
        root=snapshot_root,
        digest="sha256:" + "a" * 64,
        files=(),
        tree_paths=(),
    )
    dataset_root = tmp_path / "bird"
    dataset_root.mkdir()
    (dataset_root / "mini_dev_sqlite.json").write_text("[]", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    args = SimpleNamespace(
        dataset="bird",
        dataset_root=dataset_root,
        pipeline_revision=None,
        base_url="http://example.invalid",
        workers=1,
        case_timeout=14_400.0,
        max_rows=100,
        execution_mode="bwrap",
        repeat_ordinal=1,
    )

    _write_manifest(
        args=args,
        cases=[],
        output_dir=output_dir,
        principal={"subject": "benchmark", "roles": ["admin"]},
        completed_before=0,
        manifest_profile="bwrap_v2",
        source_snapshot=snapshot,
    )

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["artifact_contract_version"] == 1
    assert manifest["execution_policy"] == {
        "outer_case_deadline_seconds": 14_400.0,
        "workers": 1,
        "adaptive_policy": {
            "policy_version": 2,
            "wall_clock": {"wall_clock_seconds": 14_400},
            "resource_limits": {"model_tokens": 524_288, "db_probe_ms": 14_400_000},
            "operation_counts": {
                "actions": 512,
                "model_decisions": 256,
                "db_probes": 384,
            },
            "result_volume": {"returned_rows": 5_000, "inline_bytes": 2_097_152},
            "per_action": {"sample_rows": 50},
            "model_budget": {
                "model_calls": 256,
                "input_tokens_per_call": 16_384,
                "output_tokens_per_call": 32_000,
                "total_tokens": 524_288,
            },
        },
        "workflow_retry_policy": {
            "global": {
                "max_retries": 0,
                "backoff_strategy": "exponential",
                "base_delay": 1.0,
                "max_delay": 60.0,
                "retry_on_errors": ["network_error", "rate_limit", "timeout"],
            },
            "per_step": {
                "schema_research": {"max_retries": 0},
                "sql_solving": {"max_retries": 0},
                "db_audit": {"max_retries": 0},
            },
        },
    }


def test_cli_help_and_lock_preflight_do_not_initialize_runtime_models(
    tmp_path: Path,
) -> None:
    script = Path(benchmark_runner.__file__).resolve()
    help_result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=benchmark_runner.REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    forbidden = ("MemoryManager", "модель эмбеддингов", "ChromaDB")
    assert help_result.stdout.startswith("usage:")
    assert not any(marker in help_result.stdout for marker in forbidden)
    assert help_result.stderr == ""

    lock_path = tmp_path / "release-lock.json"
    preflight_code = f"""
import subprocess
import scripts.text2sql_public_benchmark as runner

runner._load_release_policy = lambda: {{}}
runner._create_release_input_lock = lambda args, policy: {{"preflight": "ok"}}
subprocess.Popen = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Popen"))
raise SystemExit(runner.main([
    "--create-release-lock", {str(lock_path)!r},
    "--bird-root", "/bird",
    "--spider-root", "/spider",
    "--spider-sqlite-root", "/sqlite",
    "--spider-database-map", "/map.jsonl",
    "--model-api-base", "http://127.0.0.1:9999/v1",
    "--model-backend-id", "frozen-model",
]))
"""
    lock_result = subprocess.run(
        [sys.executable, "-c", preflight_code],
        cwd=benchmark_runner.REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert lock_path.is_file()
    assert json.loads(lock_path.read_text()) == {"preflight": "ok"}
    assert not any(marker in lock_result.stdout for marker in forbidden)
    assert lock_result.stderr == ""


def test_cli_default_case_timeout_is_14400_seconds() -> None:
    args = benchmark_runner.build_parser().parse_args([])

    assert args.case_timeout == 14_400.0


def test_manifest_records_frozen_runtime_memory_setting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset_root = tmp_path / "bird"
    dataset_root.mkdir()
    (dataset_root / "mini_dev_sqlite.json").write_text("[]", encoding="utf-8")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    monkeypatch.setenv("TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED", "1")
    args = SimpleNamespace(
        dataset="bird",
        dataset_root=dataset_root,
        pipeline_revision=None,
        base_url="http://example.invalid",
        workers=1,
        case_timeout=930.0,
        max_rows=100,
        sandbox_env=[],
        canonical_runtime_env={
            "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "0",
        },
    )

    _write_manifest(
        args=args,
        cases=[],
        output_dir=output_dir,
        principal={"subject": "benchmark", "roles": ["admin"]},
        completed_before=0,
        manifest_profile="remote_diagnostic_v1",
    )

    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert manifest["successful_sql_memory_enabled"] == "0"


def test_runtime_configuration_snapshot_includes_typed_profiles() -> None:
    records = _configuration_sources()
    paths = {record["path"] for record in records}

    assert {
        "config/pii/categories.yaml",
        "config/text_to_sql/adaptive.yaml",
        "agent_profiles/schema_research_agent.yaml",
        "agent_profiles/sql_solver_agent.yaml",
    } <= paths
    assert all(record["size_bytes"] > 0 for record in records)
    assert all(len(record["sha256"]) == 64 for record in records)


def test_case_manifest_separates_question_knowledge_prompt_and_database_hashes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database" / "db.sqlite"
    _sqlite_file(database)
    case = benchmark_runner.BenchmarkCase(
        ordinal=3,
        case_key="bird:3",
        case_id="question-3",
        database_id="db",
        database_path=database,
        question="Question text",
        external_knowledge="Knowledge text",
        difficulty=None,
    )

    manifest_digest, database_digests = _write_case_manifest(
        tmp_path / "case_manifest.json",
        cases=[case],
        benchmark="bird",
        repeat_ordinal=1,
        bundle_id="bundle-1",
        seed=17,
        run_scope="diagnostic_subset",
    )

    payload = json.loads((tmp_path / "case_manifest.json").read_text())
    row = payload["cases"][0]
    assert manifest_digest.startswith("sha256:")
    assert row["question_sha256"] != row["external_knowledge_sha256"]
    assert row["prompt_sha256"] not in {
        row["question_sha256"],
        row["external_knowledge_sha256"],
    }
    assert row["database_sha256"] == database_digests["bird:3"]

    database.write_bytes(database.read_bytes() + b"changed")
    with pytest.raises(benchmark_runner.SandboxError, match="database changed"):
        _verify_database_digest(case, database_digests["bird:3"])


def test_run_case_keeps_question_and_external_knowledge_as_separate_authority_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Benchmark rules must enter the service only through one context document."""

    database = tmp_path / "database" / "db.sqlite"
    _sqlite_file(database)
    requests = []

    class FakeClient:
        def start(self, request: object) -> SimpleNamespace:
            requests.append(request)
            return SimpleNamespace(run_id=f"run-{len(requests)}")

        def get_run(self, run_id: str) -> SimpleNamespace:
            return SimpleNamespace(status="finished", error=None)

        def get_result(self, run_id: str) -> TextToSqlResult:
            return TextToSqlResult(
                run_id=run_id,
                status="finished",
                sql="SELECT 1",
                explanation=None,
                row_count=1,
                truncated=False,
                result_preview=[],
                error=None,
                reason_code=None,
                execution={},
                audit={},
                persistence={},
                final_output={},
            )

    monkeypatch.setattr(benchmark_runner, "_client", lambda *_args: FakeClient())
    base = dict(
        ordinal=1,
        case_key="bird:1",
        case_id="case-1",
        database_id="db",
        database_path=database,
        question="Which orders are active?",
        difficulty=None,
    )
    first = benchmark_runner.BenchmarkCase(
        **base, external_knowledge="Use active_flag = 1."
    )
    second = benchmark_runner.BenchmarkCase(
        **base, external_knowledge="Use active_flag = true."
    )

    _run_case(
        first,
        benchmark_name="bird",
        base_url="http://example.invalid",
        token="token",
        connection_ref="connection",
        timeout_seconds=1,
        max_rows=10,
    )
    _run_case(
        second,
        benchmark_name="bird",
        base_url="http://example.invalid",
        token="token",
        connection_ref="connection",
        timeout_seconds=1,
        max_rows=10,
    )

    assert [request.query for request in requests] == [base["question"]] * 2
    assert [request.context_documents for request in requests] == [
        (first.external_knowledge,),
        (second.external_knowledge,),
    ]
    assert all(request.enable_telemetry is True for request in requests)
    assert requests[0].idempotency_key != requests[1].idempotency_key


def test_canonical_output_must_be_new_and_history_evidence_updates_atomically(
    tmp_path: Path,
) -> None:
    output = _create_canonical_output_dir(tmp_path / "new-output")
    sentinel = output / "historical-artifact.json"
    sentinel.write_text("preserve\n", encoding="utf-8")
    with pytest.raises(benchmark_runner.SandboxError, match="must be new"):
        _create_canonical_output_dir(output)
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"

    args = SimpleNamespace(dataset="bird", repeat_ordinal=2, seed=17)
    evidence = output / "empty_history_evidence.json"
    first = {"case_key": "bird:0", "verification_status": "verified_empty"}
    unavailable = {
        "case_key": "bird:1",
        "verification_status": "unavailable",
        "preexisting_history_items": None,
    }
    _write_empty_history_evidence(
        evidence,
        receipts=[first],
        args=args,
        bundle_id="bundle-1",
        snapshot_digest="sha256:" + "a" * 64,
        configuration_digest="sha256:" + "b" * 64,
        run_scope="diagnostic_subset",
    )
    assert json.loads(evidence.read_text())["receipts"] == [first]

    _write_empty_history_evidence(
        evidence,
        receipts=[first, unavailable],
        args=args,
        bundle_id="bundle-1",
        snapshot_digest="sha256:" + "a" * 64,
        configuration_digest="sha256:" + "b" * 64,
        run_scope="diagnostic_subset",
    )
    assert json.loads(evidence.read_text())["receipts"] == [first, unavailable]
    assert not list(output.glob(".empty_history_evidence.json.*.tmp"))


def test_empty_history_receipt_is_persisted_before_sandbox_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = benchmark_runner.create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    database = tmp_path / "database" / "db.sqlite"
    _sqlite_file(database)
    case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="0",
        database_id="db",
        database_path=database,
        question="Question",
        external_knowledge="",
        difficulty=None,
    )
    args = SimpleNamespace(
        dataset="bird",
        repeat_ordinal=1,
        sandbox_venv_root=tmp_path / "venv",
        sandbox_secret_dir=tmp_path / "secrets",
        sandbox_env=[],
        seed=17,
        case_timeout=1.0,
        max_rows=1,
    )
    events: list[str] = []
    receipts: list[Mapping[str, object]] = []

    class FakeRunner:
        def run(self, spec, start_case, *, expected_snapshot, before_start):
            prepare_case_overlays(spec)
            before_start()
            events.append("process")
            raise RuntimeError("stop before API")

    def persist(receipt: Mapping[str, object]) -> None:
        receipts.append(receipt)
        events.append("persisted")

    monkeypatch.setattr(benchmark_runner, "SandboxCaseRunner", FakeRunner)
    expected_database_digest = hashlib.sha256(database.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="stop before API"):
        benchmark_runner._run_case_in_sandbox(
            case,
            args=args,
            token="token",
            snapshot=snapshot,
            state_root=tmp_path / "state",
            bundle_id="bundle-1",
            configuration_digest="sha256:" + "a" * 64,
            expected_database_digest=expected_database_digest,
            persist_receipt=persist,
            run_scope="diagnostic_subset",
        )

    assert events == ["persisted", "process"]
    assert receipts[0]["verification_status"] == "verified_empty"


def test_database_is_rechecked_after_empty_history_receipt_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    snapshot = benchmark_runner.create_source_snapshot(
        source,
        tmp_path / "snapshot",
        allowed_paths=(Path("backend"),),
    )
    database = tmp_path / "database" / "db.sqlite"
    _sqlite_file(database)
    expected_database_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="0",
        database_id="db",
        database_path=database,
        question="Question",
        external_knowledge="",
        difficulty=None,
    )
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(mode=0o700)
    secret_dir.chmod(0o700)
    for name in (
        "ag_ui_auth_token_map",
        "openai_api_key",
        "openai_api_key_db",
        "hf_token",
    ):
        path = secret_dir / name
        path.write_text("test\n", encoding="utf-8")
        path.chmod(0o600)
    venv_root = tmp_path / "venv"
    venv_root.mkdir()
    args = SimpleNamespace(
        dataset="bird",
        repeat_ordinal=1,
        sandbox_venv_root=venv_root,
        sandbox_secret_dir=secret_dir,
        sandbox_env=[],
        seed=17,
        case_timeout=1.0,
        max_rows=1,
    )
    spawned = False

    def start(_command: list[str]) -> object:
        nonlocal spawned
        spawned = True
        return object()

    real_runner = sandbox_module.SandboxCaseRunner(
        start=start,
        wait_for_health=lambda _port: False,
        stop=lambda _process: None,
        verify_reaped=lambda _process: True,
    )

    class TamperingRunner:
        def run(self, spec, start_case, *, expected_snapshot, before_start):
            def tamper_after_receipt() -> None:
                before_start()
                database.write_bytes(b"tampered after receipt")

            return real_runner.run(
                spec,
                start_case,
                expected_snapshot=expected_snapshot,
                before_start=tamper_after_receipt,
            )

    monkeypatch.setattr(sandbox_module, "ensure_bwrap_available", lambda: "bwrap")
    monkeypatch.setattr(
        benchmark_runner,
        "empty_history_receipt",
        lambda **_kwargs: {
            "state_namespace": "bird:1:bird:0",
            "preexisting_history_items": 0,
        },
    )
    monkeypatch.setattr(benchmark_runner, "SandboxCaseRunner", TamperingRunner)

    with pytest.raises(
        benchmark_runner.SandboxError,
        match="database content does not match frozen manifest",
    ):
        benchmark_runner._run_case_in_sandbox(
            case,
            args=args,
            token="token",
            snapshot=snapshot,
            state_root=tmp_path / "state",
            bundle_id="bundle-1",
            configuration_digest="sha256:" + "a" * 64,
            expected_database_digest=expected_database_digest,
            persist_receipt=lambda _receipt: None,
            run_scope="full_release",
        )

    assert spawned is False


def test_no_early_stop_policy_preserves_the_complete_w703_artifact_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No policy means a complete ordinary run, never a partial diagnostic run."""
    from custom_tools.text_to_sql.eval import public_benchmark_artifacts

    monkeypatch.chdir(tmp_path)
    dataset_root = Path("bird")
    dataset_root.mkdir()
    (dataset_root / "mini_dev_sqlite.json").write_text("[]", encoding="utf-8")
    database = tmp_path / "databases" / "db.sqlite"
    _sqlite_file(database)
    cases = [
        benchmark_runner.BenchmarkCase(
            ordinal=index,
            case_key=f"bird:{index}",
            case_id=str(index),
            database_id="db",
            database_path=database,
            question=f"Question {index}",
            external_knowledge="",
            difficulty=None,
        )
        for index in range(2)
    ]
    source = tmp_path / "source"
    (source / "backend").mkdir(parents=True)
    (source / "backend" / "app.py").write_text("APP = 1\n", encoding="utf-8")
    for relative_path in (
        Path("config/text_to_sql/adaptive.yaml"),
        Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
    ):
        destination = source / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((benchmark_runner.REPO_ROOT / relative_path).read_bytes())
    source_digest = hashlib.sha256((source / "backend" / "app.py").read_bytes()).hexdigest()

    monkeypatch.setattr(
        benchmark_runner,
        "_load_cases",
        lambda _args: list(cases),
    )
    monkeypatch.setattr(
        public_benchmark_artifacts,
        "_source_paths",
        lambda _args: [dataset_root / "mini_dev_sqlite.json"],
    )
    monkeypatch.setattr(
        benchmark_runner,
        "create_source_snapshot",
        lambda _root, destination, **_kwargs: sandbox_module.create_source_snapshot(
            source,
            destination,
            allowed_paths=(
                Path("backend"),
                Path("config/text_to_sql/adaptive.yaml"),
                Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
            ),
        ),
    )
    monkeypatch.setattr(
        benchmark_runner,
        "_configuration_sources",
        lambda _root: [{"path": "backend/app.py", "sha256": source_digest, "size_bytes": 8}],
    )
    monkeypatch.setattr(benchmark_runner, "_git_revision", lambda: "frozen-revision")

    class FrozenDatetime:
        @staticmethod
        def now(_timezone: object) -> object:
            return __import__("datetime").datetime(2026, 8, 3, tzinfo=__import__("datetime").timezone.utc)

    monkeypatch.setattr(benchmark_runner, "datetime", FrozenDatetime)
    monkeypatch.setattr(
        benchmark_runner.uuid,
        "uuid4",
        lambda: SimpleNamespace(hex="fixed-bundle"),
    )

    def fake_case(
        case: benchmark_runner.BenchmarkCase,
        *,
        persist_receipt: object,
        **_kwargs: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        receipt = {
            "case_key": case.case_key,
            "state_namespace": f"bird:1:{case.case_key}",
            "verification_status": "verified_empty",
            "preexisting_history_items": 0,
        }
        persist_receipt(receipt)  # type: ignore[operator]
        return (
            {
                "benchmark": "bird",
                "ordinal": case.ordinal,
                "case_key": case.case_key,
                "case_id": case.case_id,
                "database_id": case.database_id,
                "observation_status": "completed",
                "elapsed_seconds": 1.0,
                "outcome": {
                    "status": "succeeded",
                    "reason_code": "OK",
                    "executed": True,
                    "sql": f"SELECT {case.ordinal}",
                },
            },
            receipt,
        )

    monkeypatch.setattr(benchmark_runner, "_run_case_in_sandbox", fake_case)
    monkeypatch.setattr(
        benchmark_runner.benchmark_reporting,
        "find_early_stop_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("policy-free run evaluated early stop")),
    )

    def run(output: Path, state_root: Path) -> None:
        args = SimpleNamespace(
            dataset="bird",
            dataset_root=dataset_root,
            sqlite_root=None,
            database_map=None,
            output_dir=output,
            sandbox_state_root=state_root,
            sandbox_secret_dir=tmp_path / "secrets",
            sandbox_venv_root=tmp_path / "venv",
            sandbox_env=[],
            workers=1,
            repeat_ordinal=1,
            seed=17,
            diagnostic_subset=True,
            limit=None,
            case_id=[],
            ordinal_start=None,
            ordinal_stop=None,
            pipeline_revision=None,
            base_url="http://unused.invalid",
            case_timeout=1.0,
            max_rows=1,
            execution_mode="bwrap",
            early_stop_policy=None,
        )
        assert benchmark_runner._run_bwrap_benchmark(args, "token") == 0

    first, second = tmp_path / "first", tmp_path / "second"
    run(first, tmp_path / "state-first")
    run(second, tmp_path / "state-second")

    baseline = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "text2sql_public_benchmark_w703_contract.json"
        ).read_text(encoding="utf-8")
    )
    assert baseline["schema_version"] == 1
    assert baseline["record_kind"] == (
        "text2sql_public_benchmark_w703_byte_baseline"
    )
    assert baseline["normalization"] == {
        "artifact_handshake.json": [
            "artifacts.manifest.json=recomputed_from_normalized_manifest"
        ],
        "manifest.json": ["state_root"],
    }
    expected_files = set(baseline["files"])
    assert {path.name for path in first.iterdir() if path.is_file()} == expected_files
    assert {path.name for path in second.iterdir() if path.is_file()} == expected_files
    assert not any(
        (first / name).exists()
        for name in ("early_stop_candidate.json", "diagnostic_summary.json", "repair_decision.json")
    )

    def normalized_fingerprints(root: Path) -> dict[str, dict[str, object]]:
        content = {name: (root / name).read_bytes() for name in expected_files}
        manifest = json.loads(content["manifest.json"])
        manifest["state_root"] = "__W7_03_STATE_ROOT__"
        normalized_manifest = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        content["manifest.json"] = normalized_manifest
        handshake = json.loads(content["artifact_handshake.json"])
        handshake["artifacts"]["manifest.json"] = (
            "sha256:" + hashlib.sha256(normalized_manifest).hexdigest()
        )
        content["artifact_handshake.json"] = (
            json.dumps(handshake, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        return {
            name: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
            for name, payload in sorted(content.items())
        }

    assert normalized_fingerprints(first) == normalized_fingerprints(second)


def test_runtime_evidence_does_not_invent_semantic_early_stop_signature(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "case-state"
    case_root.mkdir()

    evidence = benchmark_runner._runtime_evidence(
        {
            "run_id": "run-1",
            "outcome": {
                "status": "abstained",
                "reason_code": "SCHEMA_CLARIFICATION_REQUIRED",
                "generated": False,
                "approved": False,
                "executed": False,
            },
        },
        SimpleNamespace(case_root=case_root),
    )

    assert evidence["semantic_evidence"] == {"availability": "unavailable"}


def test_runtime_evidence_verifies_exact_typed_receipt(tmp_path: Path) -> None:
    case_root = tmp_path / "case-state"
    case_root.mkdir()
    receipt = {
        "schema_version": 1,
        "record_kind": "text2sql_adaptive_early_stop_evidence",
        "terminal_source": "solver",
        "root_mechanism": "missing_evidence",
        "error_class": "missing_evidence",
        "violated_typed_requirement": "required_formula",
        "pipeline_component": "adaptive_sql_solver",
        "state_sha256": "sha256:" + "b" * 64,
    }

    evidence = benchmark_runner._runtime_evidence(
        {
            "run_id": "run-1",
            "outcome": {
                "status": "abstained",
                "reason_code": "SCHEMA_CLARIFICATION_REQUIRED",
                "semantic_evidence_receipt": receipt,
            },
        },
        SimpleNamespace(case_root=case_root),
    )

    assert evidence["schema_version"] == 2
    assert evidence["semantic_evidence"] == {
        "availability": "verified",
        "error_class": "missing_evidence",
        "violated_typed_requirement": "required_formula",
        "pipeline_component": "adaptive_sql_solver",
    }
    assert evidence["semantic_evidence_authority"] == receipt


def test_runtime_evidence_verifies_stagnation_receipt_by_each_pair(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "case-state"
    case_root.mkdir()
    receipt = {
        "schema_version": 1,
        "record_kind": "text2sql_research_stagnation_evidence",
        "terminal_source": "research",
        "terminal_reason_code": "RESEARCH_STAGNATED",
        "rejection_signatures": [
            ["invalid_stop", "INVALID_STOP"],
            ["research_query_admission", "research_query_limit"],
        ],
        "state_sha256": "sha256:" + "d" * 64,
    }

    evidence = benchmark_runner._runtime_evidence(
        {
            "run_id": "run-1",
            "outcome": {
                "status": "abstained",
                "reason_code": "RESEARCH_STAGNATED",
                "stagnation_receipt": receipt,
            },
        },
        SimpleNamespace(case_root=case_root),
    )

    assert evidence["stagnation_classification"] == {
        "availability": "verified",
        "rejection_signatures": receipt["rejection_signatures"],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_kind", "unexpected"),
        ("state_sha256", "sha256:not-a-digest"),
        ("root_mechanism", "ambiguous"),
        ("terminal_source", []),
    ],
)
def test_runtime_evidence_rejects_mismatched_typed_receipt(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    case_root = tmp_path / "case-state"
    case_root.mkdir()
    receipt = {
        "schema_version": 1,
        "record_kind": "text2sql_adaptive_early_stop_evidence",
        "terminal_source": "solver",
        "root_mechanism": "missing_evidence",
        "error_class": "missing_evidence",
        "violated_typed_requirement": "required_filter",
        "pipeline_component": "adaptive_sql_solver",
        "state_sha256": "sha256:" + "c" * 64,
    }
    receipt[field] = value

    evidence = benchmark_runner._runtime_evidence(
        {
            "run_id": "run-1",
            "outcome": {
                "status": "abstained",
                "reason_code": "SCHEMA_CLARIFICATION_REQUIRED",
                "semantic_evidence_receipt": receipt,
            },
        },
        SimpleNamespace(case_root=case_root),
    )

    assert evidence["semantic_evidence"] == {"availability": "unavailable"}
    assert evidence["semantic_evidence_authority"] == {
        "availability": "unavailable"
    }


def test_release_plan_is_complete_ordered_and_uses_three_frozen_seeds() -> None:
    policy = {
        "repeat_seeds": [730201, 918273, 160947],
    }

    plan = _build_release_plan(policy)

    assert CANONICAL_RELEASE_DATASET_ORDER == ("bird", "spider")
    assert [(item["benchmark"], item["repeat_ordinal"]) for item in plan] == [
        ("bird", 1),
        ("bird", 2),
        ("bird", 3),
        ("spider", 1),
        ("spider", 2),
        ("spider", 3),
    ]
    assert [item["seed"] for item in plan] == [
        730201,
        918273,
        160947,
        730201,
        918273,
        160947,
    ]
    with pytest.raises(ValueError, match="three distinct"):
        _build_release_plan({"repeat_seeds": [1, 1, 2]})


def test_canonical_environment_is_closed_typed_and_forces_empty_memory() -> None:
    environment = _canonical_runtime_environment(
        {"model_api_base": "http://127.0.0.1:9999/v1"}
    )

    assert environment == {
        "OPENAI_API_BASE_DB": "http://127.0.0.1:9999/v1",
        "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS": "/benchmark-input",
        "TEXT_TO_SQL_ALLOWED_DB_SCHEMES": "sqlite",
        "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "0",
    }
    policy_path = (
        Path(__file__).resolve().parents[1]
        / "config/text_to_sql/public_benchmark_release_policy.json"
    )
    policy_environment = json.loads(policy_path.read_text(encoding="utf-8"))[
        "canonical_environment"
    ]
    expected_policy_environment = dict(environment)
    expected_policy_environment.pop("OPENAI_API_BASE_DB")
    assert policy_environment == expected_policy_environment
    with pytest.raises(ValueError, match="unknown canonical environment"):
        _canonical_runtime_environment(
            {
                "model_api_base": "http://127.0.0.1:9999/v1",
                "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "1",
            }
        )
    with pytest.raises(ValueError, match="http"):
        _canonical_runtime_environment({"model_api_base": "file:///tmp/model"})



def test_case_manifest_hashes_a_shared_database_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "database" / "shared.sqlite"
    _sqlite_file(database_path)
    cases = [
        benchmark_runner.BenchmarkCase(
            ordinal=index,
            case_key=f"bird:{index}",
            case_id=str(index),
            database_id="shared",
            database_path=database_path,
            question=f"Question {index}",
            external_knowledge="",
            difficulty=None,
        )
        for index in range(3)
    ]
    calls = 0
    original = benchmark_runner.release_support.sha256_file

    def counted(path: Path) -> str:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(benchmark_runner.release_support, "sha256_file", counted)

    manifest = benchmark_runner.release_support.stable_case_manifest("bird", cases)

    assert calls == 1
    assert len({row["database_sha256"] for row in manifest["cases"]}) == 1


def _release_fixture_args(tmp_path: Path) -> SimpleNamespace:
    bird_root = tmp_path / "bird"
    bird_root.mkdir()
    (bird_root / "mini_dev_sqlite.json").write_text(
        json.dumps(
            [
                {
                    "question_id": 0,
                    "db_id": "bird_db",
                    "question": "Bird question",
                    "evidence": "Bird evidence",
                }
            ]
        ),
        encoding="utf-8",
    )
    _sqlite_file(bird_root / "dev_databases" / "bird_db" / "bird_db.sqlite")
    (bird_root / "evaluation").mkdir()
    (bird_root / "evaluation" / "evaluation_ex.py").write_text(
        "# BIRD evaluator\n", encoding="utf-8"
    )

    spider_root = tmp_path / "spider" / "spider2-lite"
    documents = spider_root / "resource" / "documents"
    documents.mkdir(parents=True)
    (documents / "rule.md").write_text("Spider rule", encoding="utf-8")
    (spider_root / "spider2-lite.jsonl").write_text(
        json.dumps(
            {
                "instance_id": "local001",
                "question": "Spider question",
                "external_knowledge": "rule.md",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (spider_root / "evaluation_suite").mkdir()
    (spider_root / "evaluation_suite" / "evaluate.py").write_text(
        "# Spider evaluator\n", encoding="utf-8"
    )
    sqlite_root = tmp_path / "spider" / "sqlite"
    _sqlite_file(sqlite_root / "spider_db.sqlite")
    database_map = tmp_path / "local-map.jsonl"
    database_map.write_text(json.dumps({"local001": "spider_db"}), encoding="utf-8")
    schema_memory_source = tmp_path / "schema-memory"
    schema_memory_database = (
        schema_memory_source / "bird_db" / "digest" / "smolagents_memory.db"
    )
    schema_memory_database.parent.mkdir(parents=True)
    with sqlite3.connect(schema_memory_database) as connection:
        connection.execute(
            "CREATE TABLE agent_memory "
            "(session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_memory VALUES "
            "('schema', 'Schema-RAG-Agent', 1, '{\"cache_kind\":\"schema_table\"}')"
        )
    return SimpleNamespace(
        bird_root=bird_root,
        spider_root=spider_root,
        spider_sqlite_root=sqlite_root,
        spider_database_map=database_map,
        model_api_base="http://127.0.0.1:9999/v1",
        model_backend_id="gateway-release-test",
        schema_memory_source=schema_memory_source,
    )


def _release_fixture_policy(args: SimpleNamespace) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_kind": "text2sql_public_benchmark_release_policy",
        "repeat_seeds": [730201, 918273, 160947],
        "datasets": {
            "bird": {
                "revision": "bird-revision",
                "origin": "https://github.com/bird-bench/mini_dev",
                "case_count": 1,
                "task_file": {
                    "path": "mini_dev_sqlite.json",
                    "size_bytes": (args.bird_root / "mini_dev_sqlite.json").stat().st_size,
                    "sha256": hashlib.sha256(
                        (args.bird_root / "mini_dev_sqlite.json").read_bytes()
                    ).hexdigest(),
                },
                "evaluator": {
                    "origin": "https://github.com/bird-bench/mini_dev",
                    "revision": "bird-revision",
                    "entrypoint": "evaluation/evaluation_ex.py",
                    "sha256": hashlib.sha256(
                        (args.bird_root / "evaluation" / "evaluation_ex.py").read_bytes()
                    ).hexdigest(),
                },
            },
            "spider": {
                "revision": "spider-revision",
                "origin": "https://github.com/xlang-ai/Spider2",
                "case_count": 1,
                "task_file": {
                    "path": "spider2-lite.jsonl",
                    "size_bytes": (args.spider_root / "spider2-lite.jsonl").stat().st_size,
                    "sha256": hashlib.sha256(
                        (args.spider_root / "spider2-lite.jsonl").read_bytes()
                    ).hexdigest(),
                },
                "database_map": {
                    "size_bytes": args.spider_database_map.stat().st_size,
                    "sha256": hashlib.sha256(
                        args.spider_database_map.read_bytes()
                    ).hexdigest(),
                },
                "evaluator": {
                    "origin": "https://github.com/xlang-ai/Spider2",
                    "revision": "spider-revision",
                    "entrypoint": "evaluation_suite/evaluate.py",
                    "sha256": hashlib.sha256(
                        (args.spider_root / "evaluation_suite" / "evaluate.py").read_bytes()
                    ).hexdigest(),
                },
            },
        },
    }


def test_release_lock_inventories_every_used_input_and_detects_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _release_fixture_args(tmp_path)
    policy = _release_fixture_policy(args)
    monkeypatch.setattr(
        benchmark_runner,
        "_verified_git_provenance",
        lambda _path, *, origin, revision: {
            "origin": origin,
            "revision": revision,
            "worktree": "verified",
        },
    )

    lock = _create_release_input_lock(args, policy=policy)

    assert lock["release_plan"] == _build_release_plan(policy)
    assert lock["canonical_environment"][
        "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED"
    ] == "0"
    kinds = {item["kind"] for item in lock["inputs"]}
    assert kinds == {"task", "database_map", "database", "document"}
    assert {item["benchmark"] for item in lock["inputs"]} == {"bird", "spider"}
    assert lock["evaluator_identities"]["spider"]["entrypoint"] == (
        "evaluation_suite/evaluate.py"
    )
    assert len(lock["case_manifests"]["bird"]["cases"]) == 1
    assert len(lock["case_manifests"]["spider"]["cases"]) == 1
    _validate_release_input_lock(args, lock, policy=policy)

    (args.spider_sqlite_root / "spider_db.sqlite").write_bytes(b"tampered")
    with pytest.raises(benchmark_runner.SandboxError, match="input.*changed"):
        _validate_release_input_lock(args, lock, policy=policy)


def test_release_lock_rejects_an_evaluator_outside_or_changed_under_dataset_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _release_fixture_args(tmp_path)
    policy = _release_fixture_policy(args)
    monkeypatch.setattr(
        benchmark_runner,
        "_verified_git_provenance",
        lambda _path, *, origin, revision: {
            "origin": origin, "revision": revision, "worktree": "verified"
        },
    )
    policy["datasets"]["spider"]["evaluator"]["entrypoint"] = "../evaluate.py"  # type: ignore[index]
    with pytest.raises(benchmark_runner.SandboxError, match="entrypoint"):
        _create_release_input_lock(args, policy=policy)

    policy = _release_fixture_policy(args)
    (args.spider_root / "evaluation_suite" / "evaluate.py").write_text(
        "changed\n", encoding="utf-8"
    )
    with pytest.raises(benchmark_runner.SandboxError, match="evaluator"):
        _create_release_input_lock(args, policy=policy)


def test_release_lock_validation_rejects_swapped_or_missing_leg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = _release_fixture_args(tmp_path)
    monkeypatch.setattr(
        benchmark_runner,
        "_verified_git_provenance",
        lambda _path, *, origin, revision: {
            "origin": origin,
            "revision": revision,
            "worktree": "verified",
        },
    )
    policy = _release_fixture_policy(args)
    lock = _create_release_input_lock(args, policy=policy)

    lock["release_plan"] = list(reversed(lock["release_plan"]))
    with pytest.raises(benchmark_runner.SandboxError, match="release plan"):
        _validate_release_input_lock(args, lock, policy=policy)
    lock = _create_release_input_lock(args, policy=policy)
    lock["release_plan"].pop()
    with pytest.raises(benchmark_runner.SandboxError, match="release plan"):
        _validate_release_input_lock(args, lock, policy=policy)


def _write_valid_completed_leg(
    output: Path,
    *,
    bundle_id: str,
    plan_item: dict[str, object],
    identity: dict[str, str],
    locked_case_manifest: dict[str, object],
    evaluator_identity: dict[str, object] | None = None,
) -> Path:
    benchmark = str(plan_item["benchmark"])
    repeat_ordinal = int(plan_item["repeat_ordinal"])
    leg_dir = output / "runs" / benchmark / f"r{repeat_ordinal}"
    leg_dir.mkdir(parents=True)
    case_manifest = {
        "schema_version": 1,
        "record_kind": "text2sql_case_manifest",
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": "canonical_release",
        "seed": int(plan_item["seed"]),
        "run_scope": "full_release",
        "cases": locked_case_manifest["cases"],
    }
    source_snapshot = sandbox_module.source_snapshot_from_manifest(
        output / "source-snapshot",
        json.loads((output / "source_snapshot_manifest.json").read_text(encoding="utf-8")),
    )
    execution_policy = public_benchmark_artifacts._execution_policy_from_snapshot(
        source_snapshot,
        SimpleNamespace(case_timeout=1_800.0, workers=1),
    )
    artifacts = {
        "manifest.json": {
            "schema_version": 2,
            "created_at": "2026-08-10T00:00:00+00:00",
            "artifact_contract_version": 1,
            "bundle_id": bundle_id,
            "benchmark": benchmark,
            "case_count": 0,
            "completed_before": 0,
            "repo_revision": "test-revision",
            "pipeline_revision": None,
            "base_url": "http://example.invalid",
            "repeat_ordinal": repeat_ordinal,
            "execution_mode": "canonical_release",
            "seed": int(plan_item["seed"]),
            "run_scope": "full_release",
            "workers": 1,
            "case_timeout": 1_800.0,
            "max_rows": 100,
            "model_configuration": {"reported_by_runtime": False},
            "configuration_sources": [],
            "successful_sql_memory_enabled": "0",
            "principal": {"subject": "benchmark", "tenant_id": None, "roles": ["admin"]},
            "sources": [],
            "source_snapshot_digest": identity["source_snapshot_digest"],
            "source_snapshot_manifest_digest": identity[
                "source_snapshot_manifest_digest"
            ],
            "configuration_digest": identity["configuration_digest"],
            "release_identity": identity,
            "state_root": "/test/state",
            "history_mode": "empty_per_case",
            "case_manifest_digest": "sha256:" + "0" * 64,
            "canonical_environment": {},
            "model_identity": {},
            "evaluator_identity": {},
            "execution_policy": execution_policy,
        },
        "case_manifest.json": case_manifest,
        "observations.jsonl": "{}\n",
        "empty_history_evidence.json": {"receipts": []},
    }
    if evaluator_identity is not None:
        artifacts["manifest.json"]["evaluator_identity"] = evaluator_identity
    for name, payload in artifacts.items():
        path = leg_dir / name
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    artifact_digests = {
        name: "sha256:" + hashlib.sha256((leg_dir / name).read_bytes()).hexdigest()
        for name in artifacts
    }
    handshake = {
        "schema_version": 1,
        "record_kind": "text2sql_benchmark_artifact_handshake",
        "bundle_id": bundle_id,
        "benchmark": benchmark,
        "repeat_ordinal": repeat_ordinal,
        "execution_mode": "canonical_release",
        "seed": int(plan_item["seed"]),
        "run_scope": "full_release",
        "case_manifest_digest": artifact_digests["case_manifest.json"],
        "source_snapshot_digest": identity["source_snapshot_digest"],
        "source_snapshot_manifest_digest": identity[
            "source_snapshot_manifest_digest"
        ],
        "configuration_digest": identity["configuration_digest"],
        "artifacts": artifact_digests,
    }
    handshake_path = leg_dir / "artifact_handshake.json"
    handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    return handshake_path


def _test_release_identity(output: Path) -> dict[str, str]:
    snapshot = sandbox_module.create_source_snapshot(
        benchmark_runner.REPO_ROOT,
        output / "source-snapshot",
        allowed_paths=(
            Path("config/text_to_sql/adaptive.yaml"),
            Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
        ),
    )
    snapshot_manifest = output / "source_snapshot_manifest.json"
    snapshot_manifest.write_text(
        json.dumps(sandbox_module.source_snapshot_manifest(snapshot)) + "\n",
        encoding="utf-8",
    )
    snapshot_manifest.chmod(0o444)
    return {
        "release_lock_digest": "sha256:" + "a" * 64,
        "source_snapshot_digest": snapshot.digest,
        "source_snapshot_manifest_digest": "sha256:"
        + hashlib.sha256(snapshot_manifest.read_bytes()).hexdigest(),
        "configuration_digest": "sha256:" + "d" * 64,
        "canonical_environment_digest": "sha256:" + "e" * 64,
        "model_identity_digest": "sha256:" + "f" * 64,
    }


@pytest.mark.parametrize(
    ("path", "value", "remove"),
    (
        (("execution_policy",), None, True),
        (("execution_policy", "workers"), None, True),
        (("execution_policy", "unexpected"), "unexpected", False),
        (("case_timeout",), True, False),
        (("workers",), True, False),
        (("execution_policy", "outer_case_deadline_seconds"), 900.0, False),
        (("execution_policy", "workers"), 2, False),
    ),
    ids=(
        "missing-policy",
        "missing-policy-key",
        "extra-policy-key",
        "boolean-top-level-deadline",
        "boolean-top-level-workers",
        "mismatched-deadline",
        "mismatched-workers",
    ),
)
def test_completed_leg_rejects_invalid_execution_policy(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
    remove: bool,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    plan_item = {"benchmark": "bird", "repeat_ordinal": 1, "seed": 730201}
    identity = _test_release_identity(output)
    locked_case_manifest = {
        "case_count": 0,
        "cases_digest": benchmark_runner._json_digest([]),
        "cases": [],
    }
    handshake_path = _write_valid_completed_leg(
        output,
        bundle_id="bundle-1",
        plan_item=plan_item,
        identity=identity,
        locked_case_manifest=locked_case_manifest,
    )
    manifest_path = handshake_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target: dict[str, object] = manifest
    for key in path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    if remove:
        target.pop(path[-1])
    else:
        target[path[-1]] = value
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
    handshake["artifacts"]["manifest.json"] = "sha256:" + hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")

    with pytest.raises(benchmark_runner.SandboxError, match="execution policy"):
        benchmark_runner.release_support._validate_completed_leg(
            output,
            record={
                "benchmark": "bird",
                "repeat_ordinal": 1,
                "seed": 730201,
                "return_code": 0,
                "artifact_handshake_sha256": "sha256:"
                + hashlib.sha256(handshake_path.read_bytes()).hexdigest(),
            },
            plan_item=plan_item,
            bundle_id="bundle-1",
            identity=identity,
            locked_case_manifests={"bird": locked_case_manifest},
            evaluator_identities=None,
        )


def test_completed_leg_rejects_execution_policy_that_differs_from_snapshot(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    identity = _test_release_identity(output)
    plan_item = {"benchmark": "bird", "repeat_ordinal": 1, "seed": 730201}
    locked_case_manifest = {
        "case_count": 0,
        "cases_digest": benchmark_runner._json_digest([]),
        "cases": [],
    }
    handshake_path = _write_valid_completed_leg(
        output,
        bundle_id="bundle-1",
        plan_item=plan_item,
        identity=identity,
        locked_case_manifest=locked_case_manifest,
    )
    manifest_path = handshake_path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["execution_policy"]["adaptive_policy"]["wall_clock"][
        "wall_clock_seconds"
    ] = 1_799
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
    handshake["artifacts"]["manifest.json"] = "sha256:" + hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")

    with pytest.raises(benchmark_runner.SandboxError, match="execution policy"):
        benchmark_runner.release_support._validate_completed_leg(
            output,
            record={
                "benchmark": "bird",
                "repeat_ordinal": 1,
                "seed": 730201,
                "return_code": 0,
                "artifact_handshake_sha256": "sha256:"
                + hashlib.sha256(handshake_path.read_bytes()).hexdigest(),
            },
            plan_item=plan_item,
            bundle_id="bundle-1",
            identity=identity,
            locked_case_manifests={"bird": locked_case_manifest},
            evaluator_identities=None,
        )


@pytest.mark.parametrize("target", ("record", "handshake", "manifest"))
def test_completed_leg_rejects_extra_closed_object_field(
    tmp_path: Path,
    target: str,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    identity = _test_release_identity(output)
    plan_item = {"benchmark": "bird", "repeat_ordinal": 1, "seed": 730201}
    locked_case_manifest = {
        "case_count": 0,
        "cases_digest": benchmark_runner._json_digest([]),
        "cases": [],
    }
    handshake_path = _write_valid_completed_leg(
        output,
        bundle_id="bundle-1",
        plan_item=plan_item,
        identity=identity,
        locked_case_manifest=locked_case_manifest,
    )
    record: dict[str, object] = {
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "seed": 730201,
        "return_code": 0,
    }
    if target == "record":
        record["unexpected"] = True
    elif target == "handshake":
        handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
        handshake["unexpected"] = True
        handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    else:
        manifest_path = handshake_path.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["unexpected"] = True
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
        handshake["artifacts"]["manifest.json"] = "sha256:" + hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
        handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    record["artifact_handshake_sha256"] = "sha256:" + hashlib.sha256(
        handshake_path.read_bytes()
    ).hexdigest()

    with pytest.raises(benchmark_runner.SandboxError):
        benchmark_runner.release_support._validate_completed_leg(
            output,
            record=record,
            plan_item=plan_item,
            bundle_id="bundle-1",
            identity=identity,
            locked_case_manifests={"bird": locked_case_manifest},
            evaluator_identities=None,
        )


@pytest.mark.parametrize("target", ("record", "handshake", "manifest"))
def test_completed_leg_rejects_boolean_repeat_ordinal(
    tmp_path: Path,
    target: str,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    identity = _test_release_identity(output)
    plan_item = {"benchmark": "bird", "repeat_ordinal": 1, "seed": 730201}
    locked_case_manifest = {
        "case_count": 0,
        "cases_digest": benchmark_runner._json_digest([]),
        "cases": [],
    }
    handshake_path = _write_valid_completed_leg(
        output,
        bundle_id="bundle-1",
        plan_item=plan_item,
        identity=identity,
        locked_case_manifest=locked_case_manifest,
    )
    record: dict[str, object] = {
        "benchmark": "bird",
        "repeat_ordinal": 1,
        "seed": 730201,
        "return_code": 0,
    }
    if target == "record":
        record["repeat_ordinal"] = True
    elif target == "handshake":
        handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
        handshake["repeat_ordinal"] = True
        handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    else:
        manifest_path = handshake_path.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["repeat_ordinal"] = True
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
        handshake["artifacts"]["manifest.json"] = "sha256:" + hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
        handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    record["artifact_handshake_sha256"] = "sha256:" + hashlib.sha256(
        handshake_path.read_bytes()
    ).hexdigest()

    with pytest.raises(benchmark_runner.SandboxError):
        benchmark_runner.release_support._validate_completed_leg(
            output,
            record=record,
            plan_item=plan_item,
            bundle_id="bundle-1",
            identity=identity,
            locked_case_manifests={"bird": locked_case_manifest},
            evaluator_identities=None,
        )


def _add_continue_governance_to_leg(
    leg_dir: Path,
    handshake_path: Path,
) -> tuple[str, ...]:
    events: list[dict[str, str]] = []
    for event_kind in ("mid_repeat", "post_repeat"):
        completed_case_count = 1
        event_dir = leg_dir / "governance" / event_kind / "000001"
        event_dir.mkdir(parents=True)
        candidate_path = event_dir / "early_stop_candidate.json"
        candidate_path.write_text(
            json.dumps(
                {
                    "benchmark": "bird",
                    "repeat_ordinal": 1,
                    "completed_case_count": 1,
                    "observations_sha256": "sha256:" + "a" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        candidate_path.chmod(0o444)
        decision_path = event_dir / "repair_decision.json"
        decision_path.write_text(
            json.dumps(
                {
                    "decision": "CONTINUE",
                    "candidate_sha256": "sha256:"
                    + hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        decision_path.chmod(0o444)
        events.append(
            release_diagnostics.finalize_continue_decision(
                leg_dir,
                event_kind=event_kind,
                completed_case_count=completed_case_count,
            )
        )
    handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
    for event in events:
        for path_field, digest_field in (
            ("candidate_path", "candidate_sha256"),
            ("decision_path", "decision_sha256"),
            ("result_path", "result_sha256"),
        ):
            handshake["artifacts"][event[path_field]] = event[digest_field]
    handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    return tuple(
        event[path_field]
        for event in events
        for path_field in ("candidate_path", "decision_path", "result_path")
    )


def _locked_case_manifests() -> dict[str, object]:
    bird_rows = [
        {
            "ordinal": 0,
            "case_key": "bird:0",
            "case_id": "0",
            "database_id": "bird_db",
            "question_sha256": "q",
            "external_knowledge_sha256": "e",
            "prompt_sha256": "p",
            "database_sha256": "d",
        }
    ]
    empty_rows: list[dict[str, object]] = []
    return {
        "bird": {
            "benchmark": "bird",
            "case_count": 1,
            "cases_digest": benchmark_runner._json_digest(bird_rows),
            "cases": bird_rows,
        },
        "spider": {
            "benchmark": "spider",
            "case_count": 0,
            "cases_digest": benchmark_runner._json_digest(empty_rows),
            "cases": empty_rows,
        },
    }


def test_release_resume_requires_exact_identity_plan_and_completed_artifacts(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    state_root = tmp_path / "state"
    output.mkdir()
    state_root.mkdir()
    plan = _build_release_plan({"repeat_seeds": [730201, 918273, 160947]})
    identity = _test_release_identity(output)
    bundle_id = "bundle-1"
    locked_case_manifests = _locked_case_manifests()
    (output / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_manifest",
                "bundle_id": bundle_id,
                "release_plan": plan,
                "state_root": str(state_root.resolve()),
                "case_manifests": locked_case_manifests,
                **identity,
            }
        ),
        encoding="utf-8",
    )
    handshake = _write_valid_completed_leg(
        output,
        bundle_id=bundle_id,
        plan_item=plan[0],
        identity=identity,
        locked_case_manifest=locked_case_manifests["bird"],
    )
    state = {
        "record_kind": "text2sql_public_benchmark_bundle_state",
        "bundle_id": bundle_id,
        "release_plan": plan,
        "active_leg": None,
        "completed_legs": [
            {
                "benchmark": "bird",
                "repeat_ordinal": 1,
                "seed": plan[0]["seed"],
                "return_code": 0,
                "artifact_handshake_sha256": "sha256:"
                + hashlib.sha256(handshake.read_bytes()).hexdigest(),
            }
        ],
    }
    _write_bundle_state(output / "bundle_state.json", state)

    completed = _validate_release_resume(
        output,
        state_root=state_root,
        identity=identity,
        release_plan=plan,
        locked_case_manifests=locked_case_manifests,
    )

    assert completed == {("bird", 1)}
    state["active_leg"] = {
        "benchmark": plan[1]["benchmark"],
        "repeat_ordinal": plan[1]["repeat_ordinal"],
        "seed": plan[1]["seed"],
    }
    _write_bundle_state(output / "bundle_state.json", state)
    with pytest.raises(benchmark_runner.SandboxError, match="partial active"):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=locked_case_manifests,
        )
    assert _validate_release_resume(
        output,
        state_root=state_root,
        identity=identity,
        release_plan=plan,
        locked_case_manifests=locked_case_manifests,
        allow_active_leg=True,
    ) == {("bird", 1)}
    state["active_leg"] = None
    _write_bundle_state(output / "bundle_state.json", state)
    handshake.write_text('{"status":"tampered"}\n', encoding="utf-8")
    with pytest.raises(benchmark_runner.SandboxError, match="artifact"):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=locked_case_manifests,
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "governance/mid_repeat/000001/early_stop_candidate.json",
        "governance/mid_repeat/000001/repair_decision.json",
        "governance/mid_repeat/000001/early_stop.json",
        "governance/post_repeat/000001/early_stop_candidate.json",
        "governance/post_repeat/000001/repair_decision.json",
        "governance/post_repeat/000001/early_stop.json",
    ),
)
@pytest.mark.parametrize("mutation", ("delete", "tamper", "chmod"))
def test_complete_resume_rejects_changed_continue_governance_artifact(
    tmp_path: Path,
    relative_path: str,
    mutation: str,
) -> None:
    output = tmp_path / "bundle"
    state_root = tmp_path / "state"
    output.mkdir()
    state_root.mkdir()
    plan = _build_release_plan({"repeat_seeds": [730201, 918273, 160947]})
    identity = _test_release_identity(output)
    bundle_id = "bundle-complete-governed"
    manifests = _locked_case_manifests()
    (output / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_manifest",
                "bundle_id": bundle_id,
                "release_plan": plan,
                "state_root": str(state_root.resolve()),
                "case_manifests": manifests,
                **identity,
            }
        ),
        encoding="utf-8",
    )
    completed_legs: list[dict[str, object]] = []
    leg_artifacts: dict[str, str] = {}
    governed_leg_dir: Path | None = None
    governed_paths: tuple[str, ...] = ()
    for plan_item in plan:
        benchmark = str(plan_item["benchmark"])
        handshake_path = _write_valid_completed_leg(
            output,
            bundle_id=bundle_id,
            plan_item=plan_item,
            identity=identity,
            locked_case_manifest=manifests[benchmark],
        )
        if plan_item == plan[0]:
            governed_leg_dir = handshake_path.parent
            governed_paths = _add_continue_governance_to_leg(
                governed_leg_dir, handshake_path
            )
        handshake_digest = "sha256:" + hashlib.sha256(
            handshake_path.read_bytes()
        ).hexdigest()
        leg_key = f"{benchmark}:r{plan_item['repeat_ordinal']}"
        leg_artifacts[leg_key] = handshake_digest
        completed_legs.append(
            {
                "benchmark": benchmark,
                "repeat_ordinal": plan_item["repeat_ordinal"],
                "seed": plan_item["seed"],
                "return_code": 0,
                "artifact_handshake_sha256": handshake_digest,
            }
        )
    _write_bundle_state(
        output / "bundle_state.json",
        {
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": bundle_id,
            "release_plan": plan,
            "active_leg": None,
            "completed_legs": completed_legs,
            "status": "complete",
            "return_code": 0,
        },
    )
    bundle_handshake = output / "bundle_artifact_handshake.json"
    bundle_handshake.write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_artifact_handshake",
                "bundle_id": bundle_id,
                "leg_artifacts": leg_artifacts,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bundle_handshake.chmod(0o444)
    assert governed_leg_dir is not None
    assert relative_path in governed_paths
    assert len(
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=manifests,
        )
    ) == 6

    target = governed_leg_dir / relative_path
    if mutation == "delete":
        target.unlink()
    elif mutation == "tamper":
        target.chmod(0o644)
        target.write_text("{}\n", encoding="utf-8")
        target.chmod(0o444)
    else:
        target.chmod(0o644)
    with pytest.raises(benchmark_runner.SandboxError):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=manifests,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_kind", "wrong"),
        ("bundle_id", "other-bundle"),
        ("benchmark", "spider"),
        ("repeat_ordinal", 2),
        ("seed", 1),
        ("source_snapshot_digest", "sha256:" + "0" * 64),
        ("source_snapshot_manifest_digest", "sha256:" + "1" * 64),
        ("configuration_digest", "sha256:" + "2" * 64),
    ],
)
def test_release_resume_rejects_rewritten_state_and_handshake_pair(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    output = tmp_path / "bundle"
    state_root = tmp_path / "state"
    output.mkdir()
    state_root.mkdir()
    plan = _build_release_plan({"repeat_seeds": [730201, 918273, 160947]})
    identity = _test_release_identity(output)
    bundle_id = "bundle-1"
    locked_case_manifests = _locked_case_manifests()
    (output / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_manifest",
                "bundle_id": bundle_id,
                "release_plan": plan,
                "state_root": str(state_root.resolve()),
                "case_manifests": locked_case_manifests,
                **identity,
            }
        ),
        encoding="utf-8",
    )
    handshake_path = _write_valid_completed_leg(
        output,
        bundle_id=bundle_id,
        plan_item=plan[0],
        identity=identity,
        locked_case_manifest=locked_case_manifests["bird"],
    )
    handshake = json.loads(handshake_path.read_text())
    handshake[field] = value
    handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    _write_bundle_state(
        output / "bundle_state.json",
        {
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": bundle_id,
            "release_plan": plan,
            "active_leg": None,
            "completed_legs": [
                {
                    "benchmark": "bird",
                    "repeat_ordinal": 1,
                    "seed": plan[0]["seed"],
                    "return_code": 0,
                    "artifact_handshake_sha256": "sha256:"
                    + hashlib.sha256(handshake_path.read_bytes()).hexdigest(),
                }
            ],
        },
    )

    with pytest.raises(benchmark_runner.SandboxError, match="handshake|identity"):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=locked_case_manifests,
        )


def test_release_resume_rejects_incomplete_artifact_inventory(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    state_root = tmp_path / "state"
    output.mkdir()
    state_root.mkdir()
    plan = _build_release_plan({"repeat_seeds": [730201, 918273, 160947]})
    identity = _test_release_identity(output)
    bundle_id = "bundle-1"
    locked_case_manifests = _locked_case_manifests()
    (output / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_manifest",
                "bundle_id": bundle_id,
                "release_plan": plan,
                "state_root": str(state_root.resolve()),
                "case_manifests": locked_case_manifests,
                **identity,
            }
        ),
        encoding="utf-8",
    )
    handshake_path = _write_valid_completed_leg(
        output,
        bundle_id=bundle_id,
        plan_item=plan[0],
        identity=identity,
        locked_case_manifest=locked_case_manifests["bird"],
    )
    handshake = json.loads(handshake_path.read_text())
    handshake["artifacts"].pop("observations.jsonl")
    handshake_path.write_text(json.dumps(handshake) + "\n", encoding="utf-8")
    _write_bundle_state(
        output / "bundle_state.json",
        {
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": bundle_id,
            "release_plan": plan,
            "active_leg": None,
            "completed_legs": [
                {
                    "benchmark": "bird",
                    "repeat_ordinal": 1,
                    "seed": plan[0]["seed"],
                    "return_code": 0,
                    "artifact_handshake_sha256": "sha256:"
                    + hashlib.sha256(handshake_path.read_bytes()).hexdigest(),
                }
            ],
        },
    )

    with pytest.raises(benchmark_runner.SandboxError, match="inventory"):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=locked_case_manifests,
        )


def test_release_resume_rejects_active_partial_leg_and_identity_mismatch(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    state_root = tmp_path / "state"
    output.mkdir()
    state_root.mkdir()
    plan = _build_release_plan({"repeat_seeds": [730201, 918273, 160947]})
    identity = {"configuration_digest": "sha256:" + "a" * 64}
    locked_case_manifests = _locked_case_manifests()
    (output / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_manifest",
                "bundle_id": "bundle-1",
                "release_plan": plan,
                "state_root": str(state_root.resolve()),
                "case_manifests": locked_case_manifests,
                **identity,
            }
        ),
        encoding="utf-8",
    )
    _write_bundle_state(
        output / "bundle_state.json",
        {
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": "bundle-1",
            "release_plan": plan,
            "active_leg": {"benchmark": "bird", "repeat_ordinal": 1},
            "completed_legs": [],
        },
    )
    with pytest.raises(benchmark_runner.SandboxError, match="partial"):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity=identity,
            release_plan=plan,
            locked_case_manifests=locked_case_manifests,
        )

    state = json.loads((output / "bundle_state.json").read_text())
    state["active_leg"] = None
    _write_bundle_state(output / "bundle_state.json", state)
    with pytest.raises(benchmark_runner.SandboxError, match="identity"):
        _validate_release_resume(
            output,
            state_root=state_root,
            identity={"configuration_digest": "sha256:" + "b" * 64},
            release_plan=plan,
            locked_case_manifests=locked_case_manifests,
        )


def test_canonical_leg_uses_frozen_cases_without_reloading_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "db" / "tiny.sqlite"
    _sqlite_file(database)
    case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="0",
        database_id="tiny",
        database_path=database,
        question="Locked question",
        external_knowledge="",
        difficulty=None,
    )
    locked_manifest = benchmark_runner._stable_case_manifest("bird", [case])
    args = SimpleNamespace(
        workers=1,
        sandbox_state_root=tmp_path / "state",
        sandbox_secret_dir=tmp_path / "secrets",
        dataset="bird",
        limit=None,
        case_id=[],
        ordinal_start=None,
        ordinal_stop=None,
        canonical_release_leg=True,
        expected_case_count=1,
        seed=730201,
        release_cases=(case,),
        release_case_manifest=locked_manifest,
        release_database_digests={case.case_key: locked_manifest["cases"][0]["database_sha256"]},
        output_dir=tmp_path / "output",
    )
    monkeypatch.setattr(
        benchmark_runner,
        "_load_cases",
        lambda _args: (_ for _ in ()).throw(AssertionError("canonical input reload")),
    )

    class ReachedOutput(RuntimeError):
        pass

    monkeypatch.setattr(
        benchmark_runner,
        "_create_canonical_output_dir",
        lambda _path: (_ for _ in ()).throw(ReachedOutput),
    )

    with pytest.raises(ReachedOutput):
        benchmark_runner._run_bwrap_benchmark(args, "token")


def test_canonical_leg_rejects_frozen_case_that_differs_from_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "db" / "tiny.sqlite"
    _sqlite_file(database)
    locked_case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="0",
        database_id="tiny",
        database_path=database,
        question="Locked question",
        external_knowledge="",
        difficulty=None,
    )
    changed_case = benchmark_runner.BenchmarkCase(
        ordinal=0,
        case_key="bird:0",
        case_id="0",
        database_id="tiny",
        database_path=database,
        question="Changed question",
        external_knowledge="",
        difficulty=None,
    )
    locked_manifest = benchmark_runner._stable_case_manifest("bird", [locked_case])
    args = SimpleNamespace(
        workers=1,
        sandbox_state_root=tmp_path / "state",
        sandbox_secret_dir=tmp_path / "secrets",
        dataset="bird",
        canonical_release_leg=True,
        expected_case_count=1,
        seed=730201,
        release_cases=(changed_case,),
        release_case_manifest=locked_manifest,
        release_database_digests={
            locked_case.case_key: locked_manifest["cases"][0]["database_sha256"]
        },
        output_dir=tmp_path / "output",
    )
    monkeypatch.setattr(
        benchmark_runner,
        "_load_cases",
        lambda _args: (_ for _ in ()).throw(AssertionError("canonical input reload")),
    )

    with pytest.raises(
        benchmark_runner.SandboxError,
        match="canonical release cases differ from release lock",
    ):
        benchmark_runner._run_bwrap_benchmark(args, "token")

    assert not args.output_dir.exists()


def test_canonical_leg_binds_full_evaluator_identity_map(tmp_path: Path) -> None:
    configuration_sources = [{"path": "config", "sha256": "a" * 64, "size_bytes": 1}]
    canonical_environment = {"TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "0"}
    model_identity = {"backend_release_id": "gateway-release-test"}
    evaluator_identities = {
        "bird": {"revision": "bird-r1", "sha256": "b" * 64},
        "spider": {"revision": "spider-r1", "sha256": "c" * 64},
    }
    expected_digest = benchmark_runner._json_digest(
        {
            "configuration_sources": configuration_sources,
            "canonical_environment": canonical_environment,
            "model_identity": model_identity,
            "evaluator_identities": evaluator_identities,
        }
    )

    def bind(identity_map: object = evaluator_identities) -> None:
        args = SimpleNamespace(
            release_configuration_sources=configuration_sources,
            canonical_runtime_env=canonical_environment,
            sandbox_env=[],
            release_model_identity=model_identity,
            release_configuration_digest=expected_digest,
        )
        if identity_map is not None:
            args.release_evaluator_identities = identity_map
        execution = BwrapBenchmarkExecution(args, "token")
        execution.snapshot = SimpleNamespace(root=tmp_path)
        execution.canonical_release_leg = True
        execution._bind_configuration()

    bind()
    with pytest.raises(
        benchmark_runner.SandboxError,
        match="release configuration identity mismatch",
    ):
        bind(None)
    with pytest.raises(
        benchmark_runner.SandboxError,
        match="release configuration identity mismatch",
    ):
        bind({**evaluator_identities, "spider": {"revision": "tampered"}})


def test_cli_rejects_conflicting_release_modes_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        benchmark_runner,
        "run_benchmark",
        lambda args: calls.append(args) or 0,
    )
    with pytest.raises(SystemExit):
        benchmark_runner.main(
            [
                "--create-release-lock",
                str(tmp_path / "create.json"),
                "--release-lock",
                str(tmp_path / "run.json"),
            ]
        )
    with pytest.raises(SystemExit):
        benchmark_runner.main(["--resume-release"])
    with pytest.raises(SystemExit):
        benchmark_runner.main(
            ["--resume-release", "--create-release-lock", str(tmp_path / "create.json")]
        )
    assert calls == []



def test_release_runner_executes_one_atomic_six_leg_bundle_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy = {
        "record_kind": "text2sql_public_benchmark_release_policy",
        "repeat_seeds": [730201, 918273, 160947],
        "datasets": {"bird": {"case_count": 0}, "spider": {"case_count": 0}},
    }
    locked_case_manifests = {
        "bird": {
            "benchmark": "bird",
            "case_count": 0,
            "cases_digest": benchmark_runner._json_digest([]),
            "cases": [],
        },
        "spider": {
            "benchmark": "spider",
            "case_count": 0,
            "cases_digest": benchmark_runner._json_digest([]),
            "cases": [],
        },
    }
    lock = {
        "record_kind": "text2sql_public_benchmark_input_lock",
        "canonical_environment": {
            "TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED": "0"
        },
        "canonical_environment_digest": "sha256:" + "a" * 64,
        "model_identity": {"backend_release_id": "gateway-release-test"},
        "model_identity_digest": "sha256:" + "b" * 64,
        "evaluator_identities": {
            "bird": {
                "origin": "https://example.test/bird",
                "revision": "r1",
                "entrypoint": "evaluate.py",
                "sha256": "c" * 64,
            },
            "spider": {
                "origin": "https://example.test/spider",
                "revision": "r1",
                "entrypoint": "evaluate.py",
                "sha256": "d" * 64,
            },
        },
        "case_manifests": locked_case_manifests,
    }
    frozen_inputs = benchmark_runner.release_support.FrozenReleaseInputs(
        cases_by_benchmark={"bird": (), "spider": ()},
        case_manifests=locked_case_manifests,
        database_digests_by_benchmark={"bird": {}, "spider": {}},
    )
    snapshot_root = tmp_path / "snapshot"
    snapshot = sandbox_module.create_source_snapshot(
        benchmark_runner.REPO_ROOT,
        snapshot_root,
        allowed_paths=(
            Path("config/text_to_sql/adaptive.yaml"),
            Path("workflow_pipelines/text_to_sql_pipeline.yaml"),
        ),
    )
    prior_schema_memory = tmp_path / "prior-state" / "schema-memory"
    prior_schema_memory_database = (
        prior_schema_memory / "tiny" / "digest" / "smolagents_memory.db"
    )
    prior_schema_memory_database.parent.mkdir(parents=True)
    with sqlite3.connect(prior_schema_memory_database) as connection:
        connection.execute(
            "CREATE TABLE agent_memory "
            "(session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_memory VALUES "
            "('schema', 'Schema-RAG-Agent', 1, '{\"cache_kind\":\"schema_table\"}')"
        )
    lock["schema_memory_source"] = release_inputs.schema_memory_source_identity(
        prior_schema_memory
    )
    args = SimpleNamespace(
        bird_root=tmp_path / "bird",
        spider_root=tmp_path / "spider",
        spider_sqlite_root=tmp_path / "sqlite",
        spider_database_map=tmp_path / "map.json",
        model_api_base="http://127.0.0.1:9999/v1",
        model_backend_id="gateway-release-test",
        output_dir=tmp_path / "bundle",
        sandbox_state_root=tmp_path / "state",
        schema_memory_source=prior_schema_memory,
        sandbox_secret_dir=tmp_path / "secrets",
        sandbox_venv_root=tmp_path / "venv",
        workers=1,
        dataset=None,
        dataset_root=None,
        repeat_ordinal=1,
        seed=None,
        limit=None,
        case_id=[],
        ordinal_start=None,
        ordinal_stop=None,
        diagnostic_subset=False,
        sandbox_env=[],
        release_lock=tmp_path / "lock.json",
        resume_release=False,
        execution_mode="remote",
        base_url="http://127.0.0.1:8765",
        case_timeout=1.0,
        max_rows=1,
        pipeline_revision=None,
    )
    monkeypatch.setattr(
        benchmark_runner.release_support,
        "load_release_policy",
        lambda _path: policy,
    )
    monkeypatch.setattr(
        benchmark_runner.release_support,
        "load_release_input_lock",
        lambda _path: lock,
    )
    validations: list[str] = []
    monkeypatch.setattr(
        benchmark_runner.release_support,
        "validate_release_input_lock",
        lambda *_args, **_kwargs: validations.append("validated") or frozen_inputs,
    )
    monkeypatch.setattr(
        benchmark_runner.release_support,
        "create_source_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    def write_snapshot_manifest(path: Path, _snapshot: object) -> str:
        assert _snapshot is snapshot
        shutil.copytree(snapshot.root, path.parent / "source-snapshot")
        path.write_text(
            json.dumps(sandbox_module.source_snapshot_manifest(snapshot)) + "\n",
            encoding="utf-8",
        )
        path.chmod(0o444)
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(
        benchmark_runner.release_support,
        "write_source_snapshot_artifact",
        write_snapshot_manifest,
    )
    monkeypatch.setattr(
        benchmark_runner.release_support,
        "configuration_sources",
        lambda _root, _paths: [
            {"path": "config", "sha256": "e" * 64, "size_bytes": 1}
        ],
    )
    calls: list[tuple[str, int, int, object, dict[str, str]]] = []

    def fake_leg(leg_args: SimpleNamespace, _token: str) -> int:
        assert (args.output_dir / "bundle_manifest.json").is_file()
        assert leg_args.release_progress_path == (
            args.sandbox_state_root.resolve() / "release_progress.sqlite3"
        )
        assert not leg_args.release_progress_path.is_relative_to(args.output_dir)
        assert leg_args.release_progress_path.parent == args.sandbox_state_root
        assert leg_args.sandbox_state_root == (
            args.sandbox_state_root
            / f"{leg_args.dataset}-r{leg_args.repeat_ordinal}"
        )
        assert leg_args.release_evaluator_identities == lock["evaluator_identities"]
        copied_database = (
            leg_args.shared_schema_memory_base
            / "tiny"
            / "digest"
            / "smolagents_memory.db"
        )
        with sqlite3.connect(copied_database) as connection:
            assert connection.execute(
                "SELECT session_id, agent_name, step, data FROM agent_memory"
            ).fetchall() == [
                ("schema", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}')
            ]
        state = json.loads((args.output_dir / "bundle_state.json").read_text())
        assert state["active_leg"] == {
            "benchmark": leg_args.dataset,
            "repeat_ordinal": leg_args.repeat_ordinal,
            "seed": leg_args.seed,
        }
        calls.append(
            (
                leg_args.dataset,
                leg_args.repeat_ordinal,
                leg_args.seed,
                leg_args.release_snapshot,
                leg_args.canonical_runtime_env,
            )
        )
        bundle_execution.release_state.ReleaseProgressStore(
            leg_args.release_progress_path
        ).bind_leg_inputs(
            benchmark=leg_args.dataset,
            repeat_ordinal=leg_args.repeat_ordinal,
            run_manifest_sha256="sha256:run",
            case_manifest_sha256="sha256:cases",
            ordered_case_keys=[],
        )
        leg_args.output_dir.mkdir(parents=True, exist_ok=True)
        (leg_args.output_dir / "artifact_handshake.json").write_text(
            "{}\n", encoding="utf-8"
        )
        print(f"stdout:{leg_args.dataset}:r{leg_args.repeat_ordinal}")
        print(f"stderr:{leg_args.dataset}:r{leg_args.repeat_ordinal}", file=sys.stderr)
        return 0

    monkeypatch.setattr(benchmark_runner, "_run_bwrap_benchmark", fake_leg)

    monkeypatch.setattr(
        bundle_execution.ReleaseBundleExecution,
        "_validate_release_resume",
        lambda _self, **_kwargs: set(),
    )
    monkeypatch.setattr(
        bundle_execution.ReleaseBundleExecution,
        "_finalize_bundle",
        lambda _self: 0,
    )
    result = benchmark_runner._run_release_bundle(args, "token")

    assert result == 0
    assert [(benchmark, repeat, seed) for benchmark, repeat, seed, _, _ in calls] == [
        ("bird", 1, 730201),
        ("bird", 2, 918273),
        ("bird", 3, 160947),
        ("spider", 1, 730201),
        ("spider", 2, 918273),
        ("spider", 3, 160947),
    ]
    assert all(item[3] is snapshot for item in calls)
    assert all(
        item[4]["TEXT_TO_SQL_SUCCESSFUL_SQL_MEMORY_ENABLED"] == "0"
        for item in calls
    )
    for benchmark, repeat, _seed, _snapshot, _environment in calls:
        leg_dir = args.output_dir / "runs" / benchmark / f"r{repeat}"
        stdout = leg_dir / "runner_stdout.log"
        stderr = leg_dir / "runner_stderr.log"
        assert stdout.read_text(encoding="utf-8") == f"stdout:{benchmark}:r{repeat}\n"
        assert stderr.read_text(encoding="utf-8") == f"stderr:{benchmark}:r{repeat}\n"
        assert stdout.stat().st_mode & 0o777 == 0o444
        assert stderr.stat().st_mode & 0o777 == 0o444
    assert len(validations) == 7
    bundle_manifest = json.loads(
        (args.output_dir / "bundle_manifest.json").read_text(encoding="utf-8")
    )
    schema_memory_seed = bundle_manifest["schema_memory_seed"]
    assert schema_memory_seed["source_root"] == str(prior_schema_memory.resolve())
    assert schema_memory_seed["source_digest"].startswith("sha256:")
    assert schema_memory_seed["copied_digest"] == schema_memory_seed["source_digest"]
    assert (args.sandbox_state_root / "release_progress.sqlite3").is_file()
    copied_database = (
        args.sandbox_state_root
        / "schema-memory"
        / "tiny"
        / "digest"
        / "smolagents_memory.db"
    )
    with sqlite3.connect(copied_database) as connection:
        assert connection.execute(
            "SELECT session_id, agent_name, step, data FROM agent_memory"
        ).fetchall() == [
            ("schema", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}')
        ]


def test_release_runner_logs_append_for_continued_active_leg_then_seal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan_item = {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7}
    leg_dir = tmp_path / "bundle" / "runs" / "bird" / "r1"
    progress_calls: list[str] = []

    class ProgressStore:
        def start_leg(self, **_kwargs: object) -> None:
            progress_calls.append("start")

        def complete_leg(self, **_kwargs: object) -> None:
            progress_calls.append("complete")

    execution = object.__new__(ReleaseBundleExecution)
    execution.args = SimpleNamespace()
    execution.token = "token"
    execution.output_dir = tmp_path / "bundle"
    execution.state_root = tmp_path / "state"
    execution.release_progress_path = tmp_path / "state" / "release.sqlite3"
    execution.snapshot = SimpleNamespace()
    execution.source_snapshot_manifest_digest = "sha256:snapshot"
    execution.frozen_configuration_sources = []
    execution.configuration_digest = "sha256:configuration"
    execution.bundle_id = "bundle-1"
    execution.identity = {}
    execution.lock = {}
    execution.policy = {}
    execution.frozen_inputs = object()
    execution.progress_store = ProgressStore()
    execution.resuming_active_leg = False
    execution.post_repeat_evaluation_barrier = False
    execution.state = {"active_leg": dict(plan_item)}
    execution._validate_frozen_inputs = lambda: object()
    execution._reconcile_state = lambda: None
    execution._validate_release_resume = lambda: set()
    execution._pause_after_early_stop = lambda _leg_output: 2
    calls: list[bool] = []

    def fake_leg(leg_args: SimpleNamespace, _token: str) -> int:
        leg_args.output_dir.mkdir(parents=True, exist_ok=True)
        calls.append(leg_args.resume_partial_leg)
        label = "first" if len(calls) == 1 else "continued"
        print(f"{label}-stdout")
        print(f"{label}-stderr", file=sys.stderr)
        if len(calls) == 1:
            return 2
        (leg_args.output_dir / "artifact_handshake.json").write_text(
            "{}\n", encoding="utf-8"
        )
        return 0

    execution.run_leg = fake_leg
    monkeypatch.setattr(
        bundle_execution.support,
        "release_leg_args",
        lambda *_args, **kwargs: SimpleNamespace(output_dir=kwargs["output_dir"]),
    )

    assert execution._execute_one_leg(plan_item) == 2
    for name in ("runner_stdout.log", "runner_stderr.log"):
        assert (leg_dir / name).stat().st_mode & 0o777 == 0o600

    execution.resuming_active_leg = True
    assert execution._execute_one_leg(plan_item) is None
    assert calls == [False, True]
    assert (leg_dir / "runner_stdout.log").read_text(encoding="utf-8") == (
        "first-stdout\ncontinued-stdout\n"
    )
    assert (leg_dir / "runner_stderr.log").read_text(encoding="utf-8") == (
        "first-stderr\ncontinued-stderr\n"
    )
    for name in ("runner_stdout.log", "runner_stderr.log"):
        assert (leg_dir / name).stat().st_mode & 0o777 == 0o444
    assert progress_calls == ["start", "complete"]


def test_release_schema_memory_seed_copies_prior_release_once(tmp_path: Path) -> None:
    source = tmp_path / "prior-state" / "schema-memory"
    source_root = source / "tiny" / "digest"
    source_root.mkdir(parents=True)
    source_fact = source_root / "smolagents_memory.db"
    with sqlite3.connect(source_fact) as connection:
        connection.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_memory VALUES ('schema', 'Schema-RAG-Agent', 1, '{\"cache_kind\":\"schema_table\"}')"
        )
    execution = object.__new__(ReleaseBundleExecution)
    source_identity = release_inputs.schema_memory_source_identity(source)
    execution.args = SimpleNamespace(schema_memory_source=None)
    execution.lock = {"schema_memory_source": source_identity}
    execution.state_root = tmp_path / "new-state"
    execution.state_root.mkdir()

    execution._prepare_schema_memory_seed()

    copied_fact = execution.state_root / "schema-memory" / "tiny" / "digest" / "smolagents_memory.db"
    with sqlite3.connect(copied_fact) as connection:
        assert connection.execute("SELECT data FROM agent_memory").fetchall() == [
            ('{"cache_kind":"schema_table"}',)
        ]
    assert execution.schema_memory_seed == {
        "source_root": source_identity["root"],
        "source_digest": source_identity["digest"],
        "copied_digest": execution._schema_memory_digest(
            execution.state_root / "schema-memory"
        ),
    }
    with sqlite3.connect(copied_fact) as connection:
        connection.execute("DELETE FROM agent_memory")
    with sqlite3.connect(source_fact) as connection:
        assert connection.execute("SELECT data FROM agent_memory").fetchall() == [
            ('{"cache_kind":"schema_table"}',)
        ]


def test_release_schema_memory_seed_transfers_only_schema_records(tmp_path: Path) -> None:
    import chromadb

    source = tmp_path / "prior-state" / "schema-memory"
    database_root = source / "tiny" / "digest"
    database_root.mkdir(parents=True)
    database = database_root / "smolagents_memory.db"
    conn = sqlite3.connect(database)
    try:
        conn.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        conn.executemany(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?)",
            (
                ("schema", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                ("workflow", "agent", 2, '{"cache_kind":"checkpoint"}'),
            ),
        )
        conn.execute("CREATE TABLE strategic_memory (session_id TEXT, type TEXT, content TEXT)")
        conn.execute("INSERT INTO strategic_memory VALUES ('workflow', 'goal', 'checkpoint')")
        conn.commit()
    finally:
        conn.close()
    client = chromadb.PersistentClient(path=str(database_root / "chromadb"))
    collection = client.create_collection("schema_memory")
    collection.add(
        ids=["schema", "workflow"],
        embeddings=[[0.0, 0.0], [1.0, 1.0]],
        documents=["schema fact", "workflow checkpoint"],
        metadatas=[
            {"session_id": "schema", "cache_kind": "schema_table"},
            {"session_id": "workflow", "cache_kind": "checkpoint"},
        ],
    )
    execution = object.__new__(ReleaseBundleExecution)
    execution.args = SimpleNamespace(schema_memory_source=None)
    execution.lock = {
        "schema_memory_source": release_inputs.schema_memory_source_identity(source)
    }
    execution.state_root = tmp_path / "new-state"
    execution.state_root.mkdir()

    execution._prepare_schema_memory_seed()

    copied_root = execution.state_root / "schema-memory" / "tiny" / "digest"
    with sqlite3.connect(copied_root / "smolagents_memory.db") as copied:
        assert copied.execute("SELECT data FROM agent_memory").fetchall() == [
            ('{"cache_kind":"schema_table"}',)
        ]
        assert copied.execute("SELECT * FROM strategic_memory").fetchall() == []
    copied_client = chromadb.PersistentClient(path=str(copied_root / "chromadb"))
    copied_collection = copied_client.get_collection("schema_memory")
    assert copied_collection.get(include=["metadatas"])["ids"] == ["schema"]
    sandbox_module.prepare_shared_schema_memory(copied_root)
    assert sandbox_module.verify_shared_schema_memory(copied_root)["sqlite_records"] == 1
    assert execution.schema_memory_seed["copied_digest"] == execution.lock[
        "schema_memory_source"
    ]["digest"]


def test_diagnostic_bwrap_seeds_only_transferable_schema_memory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "prior-state" / "schema-memory"
    database_root = source / "tiny" / "digest"
    database_root.mkdir(parents=True)
    database = database_root / "smolagents_memory.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        connection.executemany(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?)",
            (
                ("schema", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
                ("workflow", "agent", 2, '{"cache_kind":"checkpoint"}'),
            ),
        )
        connection.execute(
            "CREATE TABLE strategic_memory (session_id TEXT, type TEXT, content TEXT)"
        )
        connection.execute(
            "INSERT INTO strategic_memory VALUES ('workflow', 'goal', 'checkpoint')"
        )
        connection.execute("CREATE TABLE successful_sql (sql TEXT)")
        connection.execute("INSERT INTO successful_sql VALUES ('SELECT 1')")

    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    execution = BwrapBenchmarkExecution(
        SimpleNamespace(
            output_dir=tmp_path / "output",
            sandbox_state_root=tmp_path / "state",
            schema_memory_source=source,
            release_snapshot=sandbox_module.SourceSnapshot(
                root=snapshot_root,
                digest="sha256:snapshot",
                files=(),
                tree_paths=(),
            ),
            source_snapshot_manifest_digest="sha256:manifest",
            release_bundle_id="diagnostic-bundle",
        ),
        "token",
    )
    execution.partial_resume = False
    execution.canonical_release_leg = False

    execution._prepare_filesystem_and_snapshot()

    copied = (
        execution.state_root
        / "schema-memory"
        / "tiny"
        / "digest"
        / "smolagents_memory.db"
    )
    with sqlite3.connect(copied) as connection:
        assert connection.execute("SELECT data FROM agent_memory").fetchall() == [
            ('{"cache_kind":"schema_table"}',)
        ]
        assert connection.execute("SELECT * FROM strategic_memory").fetchall() == []
        assert connection.execute("SELECT * FROM successful_sql").fetchall() == []
    assert {path.name for path in execution.state_root.iterdir()} == {"schema-memory"}


def test_release_legs_keep_new_state_roots_but_share_seeded_schema_memory(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        bird_root=tmp_path / "bird",
        spider_root=tmp_path / "spider",
        spider_sqlite_root=tmp_path / "sqlite",
        spider_database_map=tmp_path / "map.json",
    )
    policy = {"datasets": {"bird": {"case_count": 0}, "spider": {"case_count": 0}}}
    lock = {
        "canonical_environment": {},
        "model_identity": {},
        "evaluator_identities": {"bird": {}, "spider": {}},
    }
    inputs = release_inputs.FrozenReleaseInputs(
        cases_by_benchmark={"bird": (), "spider": ()},
        case_manifests={"bird": {}, "spider": {}},
        database_digests_by_benchmark={"bird": {}, "spider": {}},
    )
    outer = tmp_path / "release-state"
    shared = outer / "schema-memory"
    legs = [
        benchmark_runner.release_support.release_leg_args(
            args,
            plan_item={"benchmark": benchmark, "repeat_ordinal": repeat, "seed": repeat},
            output_dir=tmp_path / "bundle" / benchmark / str(repeat),
            state_root=outer / f"{benchmark}-r{repeat}",
            shared_schema_memory_base=shared,
            release_progress_path=outer / "release_progress.sqlite3",
            snapshot=SimpleNamespace(),
            source_snapshot_manifest_digest="sha256:snapshot",
            configuration_sources=[],
            configuration_digest="sha256:configuration",
            bundle_id="bundle",
            release_identity={},
            lock=lock,
            policy=policy,
            frozen_inputs=inputs,
        )
        for benchmark in ("bird", "spider")
        for repeat in (1, 2, 3)
    ]

    assert len({leg.sandbox_state_root for leg in legs}) == 6
    assert all(not leg.sandbox_state_root.exists() for leg in legs)
    assert {leg.shared_schema_memory_base for leg in legs} == {shared}


def test_schema_memory_source_identity_binds_schema_embedding(tmp_path: Path) -> None:
    import chromadb

    source = tmp_path / "schema-memory" / "tiny" / "digest"
    source.mkdir(parents=True)
    collection = chromadb.PersistentClient(path=str(source / "chromadb")).create_collection(
        "schema_memory"
    )
    collection.add(
        ids=["schema"],
        embeddings=[[0.0, 0.0]],
        documents=["schema fact"],
        metadatas=[{"session_id": "schema", "cache_kind": "schema_table"}],
    )
    first = release_inputs.schema_memory_source_identity(source.parents[1])
    collection.update(ids=["schema"], embeddings=[[1.0, 1.0]])

    assert release_inputs.schema_memory_source_identity(source.parents[1]) != first


def test_release_resume_never_recopies_schema_memory_seed() -> None:
    execution = object.__new__(ReleaseBundleExecution)
    execution.resume = True
    execution._load_and_validate_inputs = lambda: None
    execution._prepare_snapshot = lambda: None
    execution._bind_release_identity = lambda: None
    execution._prepare_schema_memory_seed = lambda: pytest.fail(
        "resume must not copy schema memory"
    )
    execution._resume_bundle = lambda: 0

    assert execution.run() == 0


def test_schema_memory_source_identity_rejects_missing_or_empty_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(sandbox_module.SandboxError, match="required"):
        release_inputs.schema_memory_source_identity(None)
    empty = tmp_path / "empty-schema-memory"
    empty.mkdir()
    with pytest.raises(sandbox_module.SandboxError, match="empty"):
        release_inputs.schema_memory_source_identity(empty)


def test_release_schema_memory_seed_rejects_lock_digest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "prior-state" / "schema-memory"
    database_root = source / "tiny" / "digest"
    database_root.mkdir(parents=True)
    with sqlite3.connect(database_root / "smolagents_memory.db") as connection:
        connection.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_memory VALUES ('schema', 'Schema-RAG-Agent', 1, '{\"cache_kind\":\"schema_table\"}')"
        )
    execution = object.__new__(ReleaseBundleExecution)
    execution.args = SimpleNamespace(schema_memory_source=None)
    execution.lock = {
        "schema_memory_source": {"root": str(source.resolve()), "digest": "sha256:bad"}
    }
    execution.state_root = tmp_path / "new-state"
    execution.state_root.mkdir()

    with pytest.raises(sandbox_module.SandboxError, match="changed"):
        execution._prepare_schema_memory_seed()


def test_release_schema_memory_seed_rejects_changed_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "prior-state" / "schema-memory"
    source_database = source / "tiny" / "digest" / "smolagents_memory.db"
    source_database.parent.mkdir(parents=True)
    with sqlite3.connect(source_database) as connection:
        connection.execute(
            "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
        )
        connection.execute(
            "INSERT INTO agent_memory VALUES (?, ?, ?, ?)",
            ("schema", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_table"}'),
        )
    execution = object.__new__(ReleaseBundleExecution)
    execution.lock = {
        "schema_memory_source": release_inputs.schema_memory_source_identity(source)
    }
    execution.state_root = tmp_path / "new-state"
    execution.state_root.mkdir()

    def changed_copy(_source: Path, destination: Path) -> None:
        copied_database = destination / "tiny" / "digest" / "smolagents_memory.db"
        copied_database.parent.mkdir(parents=True)
        with sqlite3.connect(copied_database) as connection:
            connection.execute(
                "CREATE TABLE agent_memory (session_id TEXT, agent_name TEXT, step INTEGER, data TEXT)"
            )
            connection.execute(
                "INSERT INTO agent_memory VALUES (?, ?, ?, ?)",
                ("schema", "Schema-RAG-Agent", 1, '{"cache_kind":"schema_ready"}'),
            )

    monkeypatch.setattr(bundle_execution.shutil, "copytree", changed_copy)

    with pytest.raises(sandbox_module.SandboxError, match="does not match"):
        execution._prepare_schema_memory_seed()


@pytest.mark.parametrize("seed", [{}, {"source_root": "/old", "source_digest": "sha256:x", "copied_digest": "sha256:x"}])
def test_release_resume_rejects_missing_schema_memory_or_seed_mismatch(
    tmp_path: Path, seed: dict[str, str]
) -> None:
    execution = object.__new__(ReleaseBundleExecution)
    execution.state_root = tmp_path / "state"
    execution.state_root.mkdir()
    execution.lock = {
        "schema_memory_source": {"root": "/prior", "digest": "sha256:expected"}
    }
    if seed:
        (execution.state_root / "schema-memory").mkdir()

    with pytest.raises(sandbox_module.SandboxError, match="schema-memory"):
        execution._verify_resumed_schema_memory({"schema_memory_seed": seed})


def test_release_runner_seals_logs_before_post_repeat_barrier(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan_item = {"benchmark": "bird", "repeat_ordinal": 1, "seed": 7}
    leg_dir = tmp_path / "bundle" / "runs" / "bird" / "r1"

    class ProgressStore:
        def start_leg(self, **_kwargs: object) -> None:
            return None

        def defer_leg_for_post_repeat(self, **_kwargs: object) -> None:
            for name in ("runner_stdout.log", "runner_stderr.log"):
                assert (leg_dir / name).stat().st_mode & 0o777 == 0o444

    execution = object.__new__(ReleaseBundleExecution)
    execution.args = SimpleNamespace()
    execution.token = "token"
    execution.output_dir = tmp_path / "bundle"
    execution.state_root = tmp_path / "state"
    execution.release_progress_path = tmp_path / "state" / "release.sqlite3"
    execution.snapshot = SimpleNamespace()
    execution.source_snapshot_manifest_digest = "sha256:snapshot"
    execution.frozen_configuration_sources = []
    execution.configuration_digest = "sha256:configuration"
    execution.bundle_id = "bundle-1"
    execution.identity = {}
    execution.lock = {}
    execution.policy = {}
    execution.frozen_inputs = object()
    execution.progress_store = ProgressStore()
    execution.resuming_active_leg = False
    execution.post_repeat_evaluation_barrier = True
    execution.state = {"active_leg": dict(plan_item)}
    execution._validate_frozen_inputs = lambda: object()
    execution._reconcile_state = lambda: None
    execution.run_leg = lambda leg_args, _token: (
        leg_args.output_dir.mkdir(parents=True, exist_ok=True)
        or print("barrier-stdout")
        or print("barrier-stderr", file=sys.stderr)
        or 0
    )
    monkeypatch.setattr(
        bundle_execution.support,
        "release_leg_args",
        lambda *_args, **kwargs: SimpleNamespace(output_dir=kwargs["output_dir"]),
    )

    assert execution._execute_one_leg(plan_item) == 3


def test_release_runner_seals_logs_before_mid_repeat_stop_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "bundle" / "runs" / "bird" / "r1"
    leg_dir.mkdir(parents=True)
    (leg_dir / "observations.jsonl").write_text("{}\n", encoding="utf-8")
    for name in ("runner_stdout.log", "runner_stderr.log"):
        (leg_dir / name).write_text(name + "\n", encoding="utf-8")
        (leg_dir / name).chmod(0o600)

    class ProgressStore:
        def progress(self) -> SimpleNamespace:
            return SimpleNamespace(
                phase=bundle_execution.release_state.ReleasePhase.AWAITING_REPAIR_DECISION
            )

        def transition(self, **_kwargs: object) -> None:
            return None

        def seal_terminal_state(self, **_kwargs: object) -> None:
            return None

    execution = object.__new__(ReleaseBundleExecution)
    execution.progress_store = ProgressStore()
    execution.release_plan = []
    execution.state = {}
    execution.output_dir = tmp_path / "bundle"
    execution._write_state = lambda: None

    def finalize(*_args: object, **_kwargs: object) -> dict[str, str]:
        for name in ("runner_stdout.log", "runner_stderr.log"):
            assert (leg_dir / name).stat().st_mode & 0o777 == 0o444
        return {}

    monkeypatch.setattr(bundle_execution, "finalize_partial_stop", finalize)

    candidate_path = leg_dir / "governance/mid_repeat/000001/early_stop_candidate.json"
    candidate_path.parent.mkdir(parents=True)
    decision_path = candidate_path.with_name("repair_decision.json")

    assert execution._stop_mid_repeat(
        candidate_path,
        decision_path,
        "sha256:decision",
        "candidate",
        {"completed_case_count": 1},
        {"benchmark": "bird", "repeat_ordinal": 1},
    ) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("seed", 7), ("repeat_ordinal", 2), ("sandbox_env", ["X=1"])],
)
def test_release_runner_rejects_diagnostic_overrides_before_preflight(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    args = SimpleNamespace(
        bird_root=tmp_path / "bird",
        spider_root=tmp_path / "spider",
        spider_sqlite_root=tmp_path / "sqlite",
        spider_database_map=tmp_path / "map.json",
        model_api_base="http://127.0.0.1:9999/v1",
        model_backend_id="gateway-release-test",
        output_dir=tmp_path / "bundle",
        sandbox_state_root=tmp_path / "state",
        sandbox_secret_dir=tmp_path / "secrets",
        workers=1,
        dataset=None,
        dataset_root=None,
        repeat_ordinal=1,
        seed=None,
        limit=None,
        case_id=[],
        ordinal_start=None,
        ordinal_stop=None,
        diagnostic_subset=False,
        sandbox_env=[],
    )
    setattr(args, field, value)

    with pytest.raises(ValueError, match="diagnostic overrides"):
        benchmark_runner._run_release_bundle(args, "token")


def test_continue_does_not_rediscover_prior_prefix_before_new_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observations.jsonl"
    observations.write_text('{"case_key":"bird:0"}\n', encoding="utf-8")
    execution = BwrapBenchmarkExecution(
        SimpleNamespace(
            early_stop_policy={
                "schema_version": 2,
                "record_kind": "text2sql_public_benchmark_early_stop_policy",
                "block_size": 1,
                "min_completed": 2,
                "min_signature_cases": 2,
            }
        ),
        "token",
    )
    execution._select_cases = lambda: None
    execution._prepare_filesystem_and_snapshot = lambda: setattr(
        execution, "observations_path", observations
    )
    execution._bind_configuration = lambda: None
    execution._write_input_artifacts = lambda: None
    execution._prepare_history = lambda: None
    execution.partial_resume = True
    execution.leg_progress = SimpleNamespace(
        should_evaluate_resumed_prefix=False,
        authenticated_failure_count=lambda: 0,
    )
    execution._run_missing_cases = lambda: 0
    calls: list[object] = []
    monkeypatch.setattr(
        bwrap_execution.benchmark_reporting,
        "find_early_stop_candidate",
        lambda *_args: calls.append("candidate") or {},
    )

    assert execution.run() == 0
    assert calls == []


def test_continue_new_case_second_mid_repeat_event_preserves_first_indexed_triple(
    tmp_path: Path,
) -> None:
    leg_dir = tmp_path / "leg"
    events: list[dict[str, str]] = []
    first_snapshot: dict[str, tuple[bytes, int, str]] = {}
    for completed_case_count in (2, 3):
        event_dir = leg_dir / "governance/mid_repeat" / f"{completed_case_count:06d}"
        event_dir.mkdir(parents=True)
        candidate_path = event_dir / "early_stop_candidate.json"
        candidate_path.write_text(
            json.dumps(
                {
                    "benchmark": "bird",
                    "repeat_ordinal": 1,
                    "completed_case_count": completed_case_count,
                    "observations_sha256": "sha256:" + "a" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        candidate_path.chmod(0o444)
        decision_path = event_dir / "repair_decision.json"
        decision_path.write_text(
            json.dumps(
                {
                    "decision": "CONTINUE",
                    "candidate_sha256": "sha256:"
                    + hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        decision_path.chmod(0o444)
        events.append(
            release_diagnostics.finalize_continue_decision(
                leg_dir,
                event_kind="mid_repeat",
                completed_case_count=completed_case_count,
            )
        )
        if completed_case_count == 2:
            for path in (
                candidate_path,
                decision_path,
                event_dir / "early_stop.json",
            ):
                first_snapshot[str(path.relative_to(leg_dir))] = (
                    path.read_bytes(),
                    path.stat().st_mode & 0o777,
                    "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                )

    for relative_path, expected in first_snapshot.items():
        path = leg_dir / relative_path
        assert (
            path.read_bytes(),
            path.stat().st_mode & 0o777,
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        ) == expected
    assert events[1]["completed_case_count"] == 3
    assert {
        "governance/mid_repeat/000003/early_stop_candidate.json",
        "governance/mid_repeat/000003/repair_decision.json",
        "governance/mid_repeat/000003/early_stop.json",
    } == {
        events[1]["candidate_path"],
        events[1]["decision_path"],
        events[1]["result_path"],
    }


def test_complete_resume_authenticates_all_indexed_mid_repeat_continue_events(
    tmp_path: Path,
) -> None:
    output = tmp_path / "bundle"
    state_root = tmp_path / "state"
    output.mkdir()
    state_root.mkdir()
    plan = _build_release_plan({"repeat_seeds": [730201, 918273, 160947]})
    identity = _test_release_identity(output)
    bundle_id = "bundle-indexed-governance"
    manifests = _locked_case_manifests()
    (output / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "record_kind": "text2sql_public_benchmark_bundle_manifest",
                "bundle_id": bundle_id,
                "release_plan": plan,
                "state_root": str(state_root.resolve()),
                "case_manifests": manifests,
                **identity,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    completed_legs: list[dict[str, object]] = []
    for plan_item in plan:
        benchmark = str(plan_item["benchmark"])
        handshake_path = _write_valid_completed_leg(
            output,
            bundle_id=bundle_id,
            plan_item=plan_item,
            identity=identity,
            locked_case_manifest=manifests[benchmark],
        )
        if plan_item == plan[0]:
            leg_dir = handshake_path.parent
            handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
            for completed_case_count in (2, 3):
                event_dir = (
                    leg_dir
                    / "governance/mid_repeat"
                    / f"{completed_case_count:06d}"
                )
                event_dir.mkdir(parents=True)
                candidate_path = event_dir / "early_stop_candidate.json"
                candidate_path.write_text(
                    json.dumps(
                        {
                            "benchmark": "bird",
                            "repeat_ordinal": 1,
                            "completed_case_count": completed_case_count,
                            "observations_sha256": "sha256:" + "a" * 64,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                candidate_path.chmod(0o444)
                decision_path = event_dir / "repair_decision.json"
                decision_path.write_text(
                    json.dumps(
                        {
                            "decision": "CONTINUE",
                            "candidate_sha256": "sha256:"
                            + hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                decision_path.chmod(0o444)
                event = release_diagnostics.finalize_continue_decision(
                    leg_dir,
                    event_kind="mid_repeat",
                    completed_case_count=completed_case_count,
                )
                for path_field, digest_field in (
                    ("candidate_path", "candidate_sha256"),
                    ("decision_path", "decision_sha256"),
                    ("result_path", "result_sha256"),
                ):
                    handshake["artifacts"][event[path_field]] = event[digest_field]
            handshake_path.write_text(
                json.dumps(handshake) + "\n", encoding="utf-8"
            )
        completed_legs.append(
            {
                "benchmark": benchmark,
                "repeat_ordinal": plan_item["repeat_ordinal"],
                "seed": plan_item["seed"],
                "return_code": 0,
                "artifact_handshake_sha256": "sha256:"
                + hashlib.sha256(handshake_path.read_bytes()).hexdigest(),
            }
        )
    _write_bundle_state(
        output / "bundle_state.json",
        {
            "record_kind": "text2sql_public_benchmark_bundle_state",
            "bundle_id": bundle_id,
            "release_plan": plan,
            "active_leg": None,
            "completed_legs": completed_legs,
            "status": "complete",
            "return_code": 0,
        },
    )

    assert _validate_release_resume(
        output,
        state_root=state_root,
        identity=identity,
        release_plan=plan,
        locked_case_manifests=manifests,
    ) == {
        (str(item["benchmark"]), int(item["repeat_ordinal"]))
        for item in plan
    }
