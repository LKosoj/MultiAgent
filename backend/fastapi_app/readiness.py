"""Fail-closed Text-to-SQL readiness checks with stable public reason codes."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping


REQUIRED_SCHEMA_HEADS = {
    "event_store": 8,
    "memory_db": 1,
    "result_outbox": 3,
}
_STORE_NAMES = tuple(REQUIRED_SCHEMA_HEADS)
KNOWN_PROBE_LANES = frozenset(
    {"duckdb", "impala", "mysql", "postgres", "sapiq", "sqlite"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True)
class _ProbeFlight:
    completed: threading.Event = field(default_factory=threading.Event)
    healthy: bool = False


class ProbeResultCache:
    """Bounded shared execution and TTL cache for potentially blocking probes."""

    def __init__(
        self,
        *,
        max_workers: int,
        timeout_seconds: float,
        ttl_seconds: float,
    ) -> None:
        if max_workers < 1 or timeout_seconds <= 0 or ttl_seconds <= 0:
            raise ValueError("probe cache limits must be positive")
        self._timeout_seconds = float(timeout_seconds)
        self._ttl_seconds = float(ttl_seconds)
        self._max_workers = int(max_workers)
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], tuple[float, bool]] = {}
        self._in_flight: dict[tuple[str, str], _ProbeFlight] = {}
        self._closed = False

    @staticmethod
    def _key(lane: str, dsn: str | None) -> tuple[str, str]:
        digest = hashlib.sha256((dsn or "").encode("utf-8")).hexdigest()
        return lane, digest

    def _run_probe(
        self,
        key: tuple[str, str],
        flight: _ProbeFlight,
        lane: str,
        dsn: str | None,
        probe: Callable[[str, str | None], bool],
    ) -> None:
        try:
            healthy = probe(lane, dsn) is True
        except Exception:
            healthy = False
        with self._lock:
            if self._in_flight.get(key) is not flight:
                return
            self._in_flight.pop(key, None)
            if not self._closed:
                self._cache[key] = (
                    time.monotonic() + self._ttl_seconds,
                    healthy,
                )
            flight.healthy = healthy
            flight.completed.set()

    def probe(
        self,
        lane: str,
        dsn: str | None,
        probe: Callable[[str, str | None], bool],
    ) -> bool:
        key = self._key(lane, dsn)
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return False
            expired = [
                cached_key
                for cached_key, (expires_at, _healthy) in self._cache.items()
                if expires_at <= now
            ]
            for cached_key in expired:
                self._cache.pop(cached_key, None)
            cached = self._cache.get(key)
            if cached is not None:
                return cached[1]
            flight = self._in_flight.get(key)
            if flight is None:
                if len(self._in_flight) >= self._max_workers:
                    return False
                flight = _ProbeFlight()
                self._in_flight[key] = flight
                worker = threading.Thread(
                    target=self._run_probe,
                    args=(key, flight, lane, dsn, probe),
                    name=f"text2sql-readiness-probe-{lane}",
                    daemon=True,
                )
                worker.start()
        if not flight.completed.wait(self._timeout_seconds):
            return False
        return flight.healthy is True

    def shutdown(self, *, wait: bool) -> None:
        del wait
        with self._lock:
            self._closed = True
            self._cache.clear()


_SHARED_PROBE_CACHE = ProbeResultCache(
    max_workers=len(KNOWN_PROBE_LANES),
    timeout_seconds=2.0,
    ttl_seconds=30.0,
)


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status_code: int
    body: dict


@dataclass(frozen=True, slots=True)
class ReadinessRuntime:
    manifest_path: Path
    project_root: Path
    store_paths: Mapping[str, Path]
    supervisor_ready: Callable[[], bool]
    retention_ready: Callable[[], bool]
    config_validator: Callable[[], None]
    environment: Mapping[str, str]
    lane_probe: Callable[[str, str | None], bool]
    probe_cache: ProbeResultCache = field(
        default_factory=lambda: _SHARED_PROBE_CACHE
    )


def _result(ready: bool, reason_code: str) -> dict[str, str]:
    return {
        "status": "ready" if ready else "not_ready",
        "reason_code": reason_code,
    }


def _probe_sqlite(path: Path, expected: int) -> tuple[dict[str, str], int | None]:
    if not path.is_file():
        return _result(False, "STORE_MISSING"), None
    connection = None
    actual = None
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=rw",
            uri=True,
            timeout=0.25,
        )
        actual = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if actual < expected:
            return _result(False, "SCHEMA_BEHIND"), actual
        if actual > expected:
            return _result(False, "SCHEMA_AHEAD"), actual
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"PRAGMA user_version={actual}")
        connection.rollback()
        return _result(True, "READY"), actual
    except (OSError, sqlite3.Error, TypeError, ValueError):
        if connection is not None:
            connection.rollback()
        return _result(False, "STORE_UNWRITABLE"), actual
    finally:
        if connection is not None:
            connection.close()


def _enabled_probe_lanes(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw = environment.get("TEXT2SQL_ENABLED_PROBE_LANES", "")
    lanes = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return tuple(sorted(lanes))


def default_lane_probe(lane: str, dsn: str | None) -> bool:
    if not dsn:
        return False
    try:
        from db_plugins import get_plugin

        plugin = get_plugin(dsn)
        if getattr(plugin, "dialect", None) != lane:
            return False
        connection = plugin.connect(dsn)
        try:
            health = plugin.probe_capabilities(connection, dsn)
            return bool(health.production_ready)
        finally:
            plugin.close(connection)
    except Exception:
        return False


def validate_text2sql_core_configuration(project_root: Path) -> None:
    from custom_tools.text_to_sql.llm_models_config import load_llm_models_config
    from custom_tools.text_to_sql.prompts_config import load_prompts_config
    from custom_tools.text_to_sql.schema_linking_examples_config import (
        load_schema_linking_examples_config,
    )
    from custom_tools.text_to_sql.validators.safety_config import (
        load_startup_safety_policy,
    )
    from workflow.models import WorkflowDefinition

    load_startup_safety_policy()
    load_prompts_config()
    load_llm_models_config()
    load_schema_linking_examples_config()
    WorkflowDefinition.from_yaml(
        project_root / "workflow_pipelines" / "text_to_sql_pipeline.yaml"
    )


def collect_text2sql_readiness(runtime: ReadinessRuntime) -> ReadinessReport:
    checks: dict[str, dict[str, str]] = {}
    schema_heads = {
        name: {"expected": expected, "actual": None}
        for name, expected in REQUIRED_SCHEMA_HEADS.items()
    }

    try:
        from scripts.migrate_text2sql_state import load_state_schema_manifest

        manifest_heads = load_state_schema_manifest(runtime.manifest_path)
        manifest_valid = manifest_heads == REQUIRED_SCHEMA_HEADS
    except Exception:
        manifest_valid = False

    for name in _STORE_NAMES:
        path = runtime.store_paths.get(name)
        if not manifest_valid or not isinstance(path, Path):
            checks[name] = _result(False, "MANIFEST_INVALID")
            continue
        check, actual = _probe_sqlite(path, REQUIRED_SCHEMA_HEADS[name])
        checks[name] = check
        schema_heads[name]["actual"] = actual

    target_policy_sha256 = runtime.environment.get("TEXT2SQL_TARGET_POLICY_SHA256")
    target_policy_valid = target_policy_sha256 is None
    if target_policy_sha256 is not None and _SHA256.fullmatch(target_policy_sha256):
        try:
            from backend.fastapi_app.agui.connection_registry import (
                connection_target_policy_sha256,
            )

            target_policy_valid = (
                connection_target_policy_sha256(runtime.environment)
                == target_policy_sha256
            )
        except (OSError, TypeError, ValueError):
            target_policy_valid = False
    try:
        runtime.config_validator()
        config_ready = manifest_valid and target_policy_valid
    except Exception:
        config_ready = False
    checks["config"] = _result(
        config_ready,
        "READY" if config_ready else "CONFIG_INVALID",
    )

    try:
        supervisor_ready = runtime.supervisor_ready() is True
    except Exception:
        supervisor_ready = False
    checks["supervisor"] = _result(
        supervisor_ready,
        "READY" if supervisor_ready else "SUPERVISOR_NOT_ACCEPTING",
    )

    try:
        retention_ready = runtime.retention_ready() is True
    except Exception:
        retention_ready = False
    checks["retention"] = _result(
        retention_ready,
        "READY" if retention_ready else "RETENTION_NOT_RUNNING",
    )

    lanes = _enabled_probe_lanes(runtime.environment)
    probes_ready = True
    invalid_lanes = set(lanes) - KNOWN_PROBE_LANES
    if invalid_lanes:
        probes_ready = False
    else:
        for lane in lanes:
            dsn = runtime.environment.get(f"TEXT2SQL_PROBE_{lane.upper()}_DSN")
            healthy = runtime.probe_cache.probe(
                lane,
                dsn,
                runtime.lane_probe,
            )
            probes_ready = probes_ready and healthy
    if invalid_lanes:
        checks["probe_lanes"] = _result(
            False,
            "PROBE_LANE_CONFIG_INVALID",
        )
    elif not lanes:
        checks["probe_lanes"] = _result(True, "NO_PROBE_LANES_ENABLED")
    else:
        checks["probe_lanes"] = _result(
            probes_ready,
            "READY" if probes_ready else "PROBE_LANE_UNHEALTHY",
        )

    ordered_checks = {
        name: checks[name]
        for name in (
            "event_store",
            "memory_db",
            "result_outbox",
            "supervisor",
            "config",
            "retention",
            "probe_lanes",
        )
    }
    ready = all(check["status"] == "ready" for check in ordered_checks.values())
    return ReadinessReport(
        status_code=200 if ready else 503,
        body={
            "status": "ready" if ready else "not_ready",
            "checks": ordered_checks,
            "schema_heads": schema_heads,
            "probe_lanes_enabled": list(lanes),
            "target_policy_sha256": (
                target_policy_sha256 if target_policy_valid else None
            ),
        },
    )


def runtime_for_application(
    app,
    *,
    project_root: Path,
    event_store_path: Path,
) -> ReadinessRuntime:
    from backend.fastapi_app.agui.auth import validate_auth_configuration
    from workflow.process_supervisor import SupervisorConfig

    manifest_path = project_root / "config" / "text_to_sql" / "state_schema.yaml"

    def validate_config() -> None:
        validate_auth_configuration()
        SupervisorConfig.from_environment()
        validate_text2sql_core_configuration(project_root)

    def supervisor_ready() -> bool:
        supervisor = getattr(app.state, "workflow_supervisor", None)
        return bool(
            supervisor is not None and supervisor.is_accepting() is True
        )

    def retention_ready() -> bool:
        retention = getattr(app.state, "operational_retention", None)
        task = getattr(retention, "_task", None)
        return bool(task is not None and not task.done())

    return ReadinessRuntime(
        manifest_path=manifest_path,
        project_root=project_root,
        store_paths={
            "event_store": event_store_path,
            "memory_db": project_root / "memory" / "smolagents_memory.db",
            "result_outbox": (
                project_root
                / "data"
                / "multiagent_state"
                / "workflow_result_outbox.db"
            ),
        },
        supervisor_ready=supervisor_ready,
        retention_ready=retention_ready,
        config_validator=validate_config,
        environment=os.environ,
        lane_probe=default_lane_probe,
        probe_cache=_SHARED_PROBE_CACHE,
    )
