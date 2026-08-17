from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "config" / "text_to_sql" / "state_schema.yaml"


def _migrated_paths(tmp_path: Path) -> dict[str, Path]:
    from scripts.migrate_text2sql_state import migrate_text2sql_state

    paths = {
        "event_store": tmp_path / "agui_events.db",
        "memory_db": tmp_path / "memory.db",
        "result_outbox": tmp_path / "workflow_result_outbox.db",
    }
    migrate_text2sql_state(
        manifest_path=MANIFEST_PATH,
        project_root=PROJECT_ROOT,
        store_paths=paths,
    )
    return paths


def _runtime(paths: dict[str, Path], **overrides):
    from backend.fastapi_app.readiness import ProbeResultCache, ReadinessRuntime

    values = {
        "manifest_path": MANIFEST_PATH,
        "project_root": PROJECT_ROOT,
        "store_paths": paths,
        "supervisor_ready": lambda: True,
        "retention_ready": lambda: True,
        "config_validator": lambda: None,
        "environment": {},
        "lane_probe": lambda _lane, _dsn: True,
        "probe_cache": ProbeResultCache(
            max_workers=1,
            timeout_seconds=0.1,
            ttl_seconds=60,
        ),
    }
    values.update(overrides)
    return ReadinessRuntime(**values)


def test_readiness_is_200_only_when_every_required_check_is_ready(tmp_path: Path) -> None:
    from backend.fastapi_app.readiness import collect_text2sql_readiness

    report = collect_text2sql_readiness(_runtime(_migrated_paths(tmp_path)))

    assert report.status_code == 200
    assert report.body == {
        "status": "ready",
        "checks": {
            name: {"status": "ready", "reason_code": reason}
            for name, reason in {
                "event_store": "READY",
                "memory_db": "READY",
                "result_outbox": "READY",
                "supervisor": "READY",
                "config": "READY",
                "retention": "READY",
                "probe_lanes": "NO_PROBE_LANES_ENABLED",
            }.items()
        },
        "schema_heads": {
            name: {"expected": head, "actual": head}
            for name, head in {
                "event_store": 10,
                "memory_db": 1,
                "result_outbox": 3,
            }.items()
        },
        "probe_lanes_enabled": [],
        "target_policy_sha256": None,
    }


def test_readiness_recomputes_target_policy_digest_from_runtime_environment(
    tmp_path: Path,
) -> None:
    from backend.fastapi_app.agui.connection_registry import (
        connection_target_policy_sha256,
    )
    from backend.fastapi_app.readiness import collect_text2sql_readiness

    environment = {
        "TEXT_TO_SQL_ALLOWED_DB_SCHEMES": "postgresql,mysql,sqlite",
        "TEXT_TO_SQL_ALLOWED_DB_TARGETS": "db.example:5432,db.example:3306",
        "TEXT_TO_SQL_ALLOWED_DB_CIDRS": "10.0.0.0/8:5432",
        "TEXT_TO_SQL_ALLOWED_DB_FILE_ROOTS": "/var/lib/text2sql",
    }
    environment["TEXT2SQL_TARGET_POLICY_SHA256"] = (
        connection_target_policy_sha256(environment)
    )
    environment["TEXT_TO_SQL_ALLOWED_DB_TARGETS"] += ",other.example:5432"

    report = collect_text2sql_readiness(
        _runtime(_migrated_paths(tmp_path), environment=environment)
    )

    assert report.status_code == 503
    assert report.body["checks"]["config"]["reason_code"] == "CONFIG_INVALID"
    assert report.body["target_policy_sha256"] is None


def test_readiness_fails_closed_for_schema_lifecycle_and_config_failures(
    tmp_path: Path,
) -> None:
    from backend.fastapi_app.readiness import collect_text2sql_readiness

    paths = _migrated_paths(tmp_path)
    connection = sqlite3.connect(paths["memory_db"])
    connection.execute("PRAGMA user_version=0")
    connection.close()

    def invalid_config() -> None:
        raise ValueError("postgresql://alice:secret@example.invalid/db")

    report = collect_text2sql_readiness(
        _runtime(
            paths,
            supervisor_ready=lambda: False,
            retention_ready=lambda: False,
            config_validator=invalid_config,
        )
    )

    assert report.status_code == 503
    assert report.body["status"] == "not_ready"
    assert report.body["checks"]["memory_db"] == {
        "status": "not_ready",
        "reason_code": "SCHEMA_BEHIND",
    }
    assert report.body["checks"]["supervisor"]["reason_code"] == (
        "SUPERVISOR_NOT_ACCEPTING"
    )
    assert report.body["checks"]["retention"]["reason_code"] == (
        "RETENTION_NOT_RUNNING"
    )
    assert report.body["checks"]["config"]["reason_code"] == "CONFIG_INVALID"
    assert "secret" not in str(report.body)


def test_only_explicit_probe_lanes_are_required_and_fail_closed(tmp_path: Path) -> None:
    from backend.fastapi_app.readiness import collect_text2sql_readiness

    calls = []
    environment = {
        "TEXT2SQL_ENABLED_PROBE_LANES": "postgres,mysql",
        "TEXT2SQL_PROBE_POSTGRES_DSN": "postgresql://hidden",
        "TEXT2SQL_PROBE_MYSQL_DSN": "mysql://hidden",
    }

    def probe(lane: str, dsn: str | None) -> bool:
        calls.append((lane, dsn))
        return lane == "postgres"

    report = collect_text2sql_readiness(
        _runtime(
            _migrated_paths(tmp_path),
            environment=environment,
            lane_probe=probe,
        )
    )

    assert report.status_code == 503
    assert report.body["checks"]["probe_lanes"] == {
        "status": "not_ready",
        "reason_code": "PROBE_LANE_UNHEALTHY",
    }
    assert [lane for lane, _dsn in calls] == ["mysql", "postgres"]
    assert "hidden" not in str(report.body)


def test_probe_lanes_reject_unknown_names_without_calling_probe(tmp_path: Path) -> None:
    from backend.fastapi_app.readiness import collect_text2sql_readiness

    calls = []
    report = collect_text2sql_readiness(
        _runtime(
            _migrated_paths(tmp_path),
            environment={"TEXT2SQL_ENABLED_PROBE_LANES": "postgres,unknown"},
            lane_probe=lambda lane, dsn: calls.append((lane, dsn)) or True,
        )
    )

    assert report.status_code == 503
    assert report.body["checks"]["probe_lanes"]["reason_code"] == (
        "PROBE_LANE_CONFIG_INVALID"
    )
    assert calls == []


def test_probe_timeout_is_single_flight_and_does_not_expose_dsn(tmp_path: Path) -> None:
    from backend.fastapi_app.readiness import (
        ProbeResultCache,
        collect_text2sql_readiness,
    )

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_probe(lane: str, dsn: str | None) -> bool:
        calls.append((lane, dsn))
        entered.set()
        release.wait(timeout=5)
        return True

    cache = ProbeResultCache(
        max_workers=1,
        timeout_seconds=0.05,
        ttl_seconds=60,
    )
    runtime = _runtime(
        _migrated_paths(tmp_path),
        environment={
            "TEXT2SQL_ENABLED_PROBE_LANES": "postgres",
            "TEXT2SQL_PROBE_POSTGRES_DSN": "postgresql://alice:secret@db/app",
        },
        lane_probe=blocked_probe,
        probe_cache=cache,
    )
    try:
        first = collect_text2sql_readiness(runtime)
        assert entered.is_set()
        second = collect_text2sql_readiness(runtime)
        assert len(calls) == 1
        assert first.status_code == second.status_code == 503
        assert "secret" not in str(first.body)
    finally:
        release.set()
        cache.shutdown(wait=True)


def test_hung_probe_shutdown_is_nonblocking_and_capacity_is_fixed() -> None:
    from backend.fastapi_app.readiness import ProbeResultCache

    release = threading.Event()
    calls = []

    def hang(lane: str, dsn: str | None) -> bool:
        calls.append((lane, dsn, threading.current_thread().daemon))
        release.wait(timeout=5)
        return True

    cache = ProbeResultCache(
        max_workers=2,
        timeout_seconds=0.02,
        ttl_seconds=60,
    )
    assert cache.probe("postgres", "postgresql://one", hang) is False
    assert cache.probe("mysql", "mysql://two", hang) is False
    assert cache.probe("sqlite", "sqlite:///three", hang) is False
    assert len(calls) == 2
    assert all(daemon is True for _lane, _dsn, daemon in calls)

    shutdown_complete = threading.Event()

    def shutdown() -> None:
        cache.shutdown(wait=True)
        shutdown_complete.set()

    thread = threading.Thread(target=shutdown)
    thread.start()
    try:
        assert shutdown_complete.wait(timeout=0.2)
    finally:
        release.set()
        thread.join(timeout=1)


def test_core_config_validation_calls_canonical_runtime_loaders(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import custom_tools.text_to_sql.llm_models_config as models_config
    import custom_tools.text_to_sql.prompts_config as prompts_config
    import custom_tools.text_to_sql.schema_linking_examples_config as linking_config
    import custom_tools.text_to_sql.validators.safety_config as safety_config
    from backend.fastapi_app.readiness import validate_text2sql_core_configuration
    from workflow.models import WorkflowDefinition

    calls = []
    monkeypatch.setattr(
        safety_config,
        "load_startup_safety_policy",
        lambda: calls.append("safety"),
    )
    monkeypatch.setattr(
        prompts_config,
        "load_prompts_config",
        lambda: calls.append("prompts"),
    )
    monkeypatch.setattr(
        models_config,
        "load_llm_models_config",
        lambda: calls.append("models"),
    )
    monkeypatch.setattr(
        linking_config,
        "load_schema_linking_examples_config",
        lambda: calls.append("linking"),
    )
    monkeypatch.setattr(
        WorkflowDefinition,
        "from_yaml",
        lambda path: calls.append(Path(path).name),
    )

    validate_text2sql_core_configuration(tmp_path)

    assert calls == [
        "safety",
        "prompts",
        "models",
        "linking",
        "text_to_sql_pipeline.yaml",
    ]


def test_application_readiness_uses_public_supervisor_api_only() -> None:
    import inspect
    from backend.fastapi_app import readiness

    source = inspect.getsource(readiness.runtime_for_application)
    assert ".is_accepting()" in source
    assert '"_running"' not in source


def test_readyz_route_preserves_frozen_http_schema(monkeypatch, tmp_path: Path) -> None:
    from backend.fastapi_app.readiness import ReadinessReport
    from workflow import state_files

    state_path = tmp_path / "main-event-store.db"
    monkeypatch.setattr(
        state_files,
        "default_state_database_path",
        lambda *_args, **_kwargs: state_path,
    )
    sys.modules.pop("backend.fastapi_app.main", None)
    main = importlib.import_module("backend.fastapi_app.main")

    expected = {
        "status": "not_ready",
        "checks": {"config": {"status": "not_ready", "reason_code": "CONFIG_INVALID"}},
        "schema_heads": {},
    }
    monkeypatch.setattr(
        main,
        "application_readiness",
        lambda _app: ReadinessReport(status_code=503, body=expected),
    )

    try:
        response = asyncio.run(main.readyz(SimpleNamespace(app=main.app)))

        assert response.status_code == 503
        assert response.body.decode("utf-8") == (
            '{"status":"not_ready","checks":{"config":{"status":"not_ready",'
            '"reason_code":"CONFIG_INVALID"}},"schema_heads":{}}'
        )
    finally:
        main.store.close()
        sys.modules.pop("backend.fastapi_app.main", None)
