from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import db_plugins.base as base
import db_plugins.manager as plugin_manager
from custom_tools.text_to_sql import core as core_facade
from custom_tools.text_to_sql.core._db_exec import (
    QueryExecutionRequest,
    QueryExecutor,
    QueryPurpose,
    _apply_statement_timeout,
)
from db_plugins.base import BaseDBPlugin
from db_plugins.impala import ImpalaPlugin
from db_plugins.postgres import PostgresPlugin
from db_plugins.sapiq import SAPIQPlugin
from db_plugins.sqlite import SQLitePlugin
from db_plugins.streamlit_api import DBPluginManager
from tool_runtime_context import (
    SupervisorExecutionEvidence,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)
from workflow.deadline import DeadlineBudget


_CAPABILITY_FIELDS = {
    "read_only",
    "statement_timeout",
    "cancellation",
    "explain",
    "introspection",
    "composite_fk_introspection",
    "parameter_binding",
}


def test_canonical_matrix_is_complete_immutable_and_alias_free() -> None:
    matrix = plugin_manager.get_support_matrix()

    assert isinstance(matrix, tuple)
    assert [item.dialect for item in matrix] == [
        "duckdb",
        "impala",
        "mysql",
        "postgres",
        "sapiq",
        "sqlite",
    ]
    assert len(plugin_manager.serialize_support_matrix()) == 6
    for capabilities in matrix:
        assert set(capabilities.to_mapping()) == {"dialect", *_CAPABILITY_FIELDS}
        for field in _CAPABILITY_FIELDS:
            capability = getattr(capabilities, field)
            assert isinstance(capability, base.Capability)
            assert set(capability.to_mapping()) == {"state", "mode", "reason_code"}
        with pytest.raises(FrozenInstanceError):
            capabilities.dialect = "changed"  # type: ignore[misc]

    assert plugin_manager.get_capabilities("postgresql") == (
        plugin_manager.get_capabilities("postgres")
    )
    assert plugin_manager.get_capabilities("postgresql://u:p@host/db") == (
        plugin_manager.get_capabilities("postgres://u:p@host/db")
    )


def test_matrix_timeout_modes_and_t11_grouping_facts_are_truthful() -> None:
    matrix = {item.dialect: item for item in plugin_manager.get_support_matrix()}

    assert matrix["postgres"].statement_timeout == base.Capability(
        base.CapabilityState.SUPPORTED,
        base.EnforcementMode.DATABASE,
        "POSTGRES_STATEMENT_TIMEOUT",
    )
    for dialect in {"mysql", "sqlite", "duckdb", "sapiq", "impala"}:
        timeout = matrix[dialect].statement_timeout
        assert timeout.state is base.CapabilityState.SUPPORTED
        assert timeout.mode is base.EnforcementMode.SUPERVISOR

    for dialect in {"postgres", "mysql", "sqlite"}:
        assert matrix[dialect].composite_fk_introspection.state is (
            base.CapabilityState.SUPPORTED
        )
    for dialect in {"duckdb", "sapiq", "impala"}:
        assert matrix[dialect].composite_fk_introspection.state is (
            base.CapabilityState.UNSUPPORTED
        )


def test_inherited_timeout_is_unsupported_and_never_applied() -> None:
    plugin = BaseDBPlugin()
    capabilities = plugin.get_capabilities()

    assert capabilities.statement_timeout.state is base.CapabilityState.UNSUPPORTED
    with pytest.raises(base.UnsupportedCapabilityError, match="statement timeout"):
        plugin.set_statement_timeout(object(), 100)

    failure, mode = _apply_statement_timeout(
        plugin,
        object(),
        100,
        0.0,
        "SELECT 1",
        10,
        capabilities=capabilities,
    )
    assert failure is not None
    assert mode is base.EnforcementMode.NONE


def test_require_capabilities_rejects_unenforced_read_only() -> None:
    required = base.RequiredDatabaseCapabilities(
        read_only=True,
        statement_timeout=True,
        cancellation=True,
        allow_supervisor=True,
    )

    plugin_manager.require_capabilities("postgres", required)
    with pytest.raises(base.DatabaseCapabilityError) as exc_info:
        plugin_manager.require_capabilities("sapiq", required)

    assert exc_info.value.capability == "read_only"
    assert exc_info.value.reason_code == "READ_ONLY_ENFORCEMENT_UNAVAILABLE"


def test_optional_driver_and_vendor_health_reasons_are_actionable(monkeypatch) -> None:
    sapiq = SAPIQPlugin()
    monkeypatch.setattr(sapiq, "_python_driver_available", lambda: False)
    missing_python = sapiq.probe_capabilities(None, "sapiq://host/db")
    assert missing_python.production_ready is False
    assert "OPTIONAL_DRIVER_MISSING" in missing_python.reason_codes

    monkeypatch.setattr(sapiq, "_python_driver_available", lambda: True)
    monkeypatch.setattr(sapiq, "_vendor_driver_available", lambda: False)
    missing_vendor = sapiq.probe_capabilities(None, "sapiq://host/db")
    assert "VENDOR_ODBC_DRIVER_MISSING" in missing_vendor.reason_codes

    impala = ImpalaPlugin()
    monkeypatch.setattr(impala, "_python_driver_available", lambda: False)
    impala_health = impala.probe_capabilities(None, "impala://host/db")
    assert impala_health.production_ready is False
    assert "OPTIONAL_DRIVER_MISSING" in impala_health.reason_codes


@pytest.mark.parametrize("dialect", ["sapiq", "impala"])
def test_fail_open_never_becomes_production_ready(dialect: str) -> None:
    plugin = SAPIQPlugin() if dialect == "sapiq" else ImpalaPlugin()
    dsn = f"{dialect}://host/db?read_only_fail_open=true"

    capabilities = plugin.get_capabilities(dsn)
    health = plugin.probe_capabilities(None, dsn)

    assert capabilities.read_only.state is base.CapabilityState.UNSUPPORTED
    assert health.production_ready is False
    assert "READ_ONLY_FAIL_OPEN_FORBIDDEN" in health.reason_codes


def test_postgres_probe_failure_downgrades_readiness() -> None:
    class BrokenCursor:
        def execute(self, _sql):
            raise RuntimeError("probe failed")

        def close(self):
            pass

    class BrokenConnection:
        def cursor(self):
            return BrokenCursor()

    health = PostgresPlugin().probe_capabilities(
        BrokenConnection(), "postgresql://host/db"
    )

    assert health.production_ready is False
    assert "CAPABILITY_PROBE_FAILED" in health.reason_codes
    assert health.capabilities.read_only.state is base.CapabilityState.UNVERIFIED
    assert health.capabilities.statement_timeout.state is base.CapabilityState.UNVERIFIED


def test_connection_probe_downgrade_blocks_query_and_closes_connection(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class DowngradedSQLite(SQLitePlugin):
        def connect(self, _dsn):
            calls.append("connect")
            return object()

        def probe_capabilities(self, conn=None, dsn=None):
            calls.append("probe")
            capabilities = self.get_capabilities(dsn).downgrade(
                read_only=base.Capability.unverified(
                    "READ_ONLY_PROBE_FAILED",
                    base.EnforcementMode.READ_ONLY_FILE,
                )
            )
            return base.PluginHealth(
                self.dialect,
                capabilities,
                False,
                ("READ_ONLY_PROBE_FAILED",),
            )

        def execute_select(self, *_args, **_kwargs):
            calls.append("execute")
            raise AssertionError("query submission must be blocked by probe health")

        def close(self, _conn):
            calls.append("close")

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    token = set_tool_runtime_context(
        {
            "supervisor_evidence": SupervisorExecutionEvidence("supervisor-1", 1),
        }
    )
    try:
        result = QueryExecutor(get_plugin=lambda _dsn: DowngradedSQLite()).execute(
            QueryExecutionRequest(
                sql_query="SELECT 1",
                purpose=QueryPurpose.GROUNDING,
                row_limit=5,
                dsn="sqlite:///tmp/app.db",
                deadline=DeadlineBudget.from_duration(5),
            )
        )
    finally:
        reset_tool_runtime_context(token)

    assert result.success is False
    assert result.outcome["capability_error"] == {
        "capability": "read_only",
        "reason_code": "READ_ONLY_PROBE_FAILED",
    }
    assert calls == ["connect", "probe", "close"]


def test_strict_executor_rejects_before_connect(monkeypatch) -> None:
    calls: list[str] = []

    class UnreadySAPIQ(SAPIQPlugin):
        def connect(self, _dsn):
            calls.append("connect")
            raise AssertionError("strict capability rejection must precede connect")

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    result = QueryExecutor(get_plugin=lambda _dsn: UnreadySAPIQ()).execute(
        QueryExecutionRequest(
            sql_query="SELECT 1",
            purpose=QueryPurpose.GROUNDING,
            row_limit=5,
            dsn="sapiq://host/db?read_only_fail_open=true",
        )
    )

    assert result.success is False
    assert calls == []
    assert result.outcome["capability_error"] == {
        "capability": "read_only",
        "reason_code": "READ_ONLY_ENFORCEMENT_UNAVAILABLE",
    }


def test_custom_plugin_missing_declaration_fails_before_connect(monkeypatch) -> None:
    calls: list[str] = []

    class MissingDeclaration:
        def connect(self, _dsn):
            calls.append("connect")
            raise AssertionError("missing declaration must fail before connect")

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    result = QueryExecutor(get_plugin=lambda _dsn: MissingDeclaration()).execute(
        QueryExecutionRequest(
            sql_query="SELECT 1",
            purpose=QueryPurpose.GROUNDING,
            row_limit=5,
            dsn="sqlite:///tmp/app.db",
        )
    )

    assert result.success is False
    assert result.outcome["capability_error"] == {
        "capability": "capability_contract",
        "reason_code": "CAPABILITY_DECLARATION_MISSING",
    }
    assert calls == []


def test_custom_plugin_missing_probe_closes_without_query_submission(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class MissingProbe(SQLitePlugin):
        probe_capabilities = None

        def connect(self, _dsn):
            calls.append("connect")
            return object()

        def execute_select(self, *_args, **_kwargs):
            calls.append("execute")
            raise AssertionError("missing probe must block query submission")

        def close(self, _conn):
            calls.append("close")

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    token = set_tool_runtime_context(
        {
            "supervisor_evidence": SupervisorExecutionEvidence("supervisor-1", 1),
        }
    )
    try:
        result = QueryExecutor(get_plugin=lambda _dsn: MissingProbe()).execute(
            QueryExecutionRequest(
                sql_query="SELECT 1",
                purpose=QueryPurpose.GROUNDING,
                row_limit=5,
                dsn="sqlite:///tmp/app.db",
                deadline=DeadlineBudget.from_duration(5),
            )
        )
    finally:
        reset_tool_runtime_context(token)

    assert result.success is False
    assert result.outcome["capability_error"] == {
        "capability": "production_ready",
        "reason_code": "CAPABILITY_PROBE_MISSING",
    }
    assert calls == ["connect", "close"]


def test_supervisor_timeout_is_labeled_without_calling_base_setter(monkeypatch) -> None:
    class SQLiteExecutorDouble(SQLitePlugin):
        def connect(self, _dsn):
            return object()

        def close(self, _conn):
            pass

        def execute_select(self, _conn, _sql, row_limit=500):
            return {
                "success": True,
                "data": [[1]],
                "columns": ["one"],
                "rows_affected": 1,
                "error_message": None,
            }

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    token = set_tool_runtime_context(
        {
            "supervisor_evidence": SupervisorExecutionEvidence("supervisor-1", 1),
        }
    )
    try:
        result = QueryExecutor(get_plugin=lambda _dsn: SQLiteExecutorDouble()).execute(
            QueryExecutionRequest(
                sql_query="SELECT 1",
                purpose=QueryPurpose.GROUNDING,
                row_limit=5,
                dsn="sqlite:///tmp/app.db",
                deadline=DeadlineBudget.from_duration(5),
            )
        )
    finally:
        reset_tool_runtime_context(token)

    assert result.success is True
    assert result.outcome["timeout_enforcement_mode"] == "supervisor"
    assert result.outcome["cancellation_enforcement_mode"] == "supervisor"


def test_deadline_without_supervisor_evidence_rejects_before_connect(monkeypatch) -> None:
    calls: list[str] = []

    class SQLiteExecutorDouble(SQLitePlugin):
        def connect(self, _dsn):
            calls.append("connect")
            raise AssertionError("untrusted supervisor mode must fail before connect")

    monkeypatch.setattr(
        core_facade,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    result = QueryExecutor(get_plugin=lambda _dsn: SQLiteExecutorDouble()).execute(
        QueryExecutionRequest(
            sql_query="SELECT 1",
            purpose=QueryPurpose.GROUNDING,
            row_limit=5,
            dsn="sqlite:///tmp/app.db",
            deadline=DeadlineBudget.from_duration(5),
        )
    )

    assert result.success is False
    assert result.outcome["capability_error"] == {
        "capability": "statement_timeout",
        "reason_code": "SUPERVISOR_ENFORCEMENT_DISALLOWED",
    }
    assert calls == []


def test_failed_database_timeout_setter_reports_no_enforcement() -> None:
    class FailingSetter:
        def set_statement_timeout(self, _conn, _timeout_ms):
            raise RuntimeError("timeout setup failed")

    capabilities = PostgresPlugin().get_capabilities("postgresql://host/db")
    failure, mode = _apply_statement_timeout(
        FailingSetter(),
        object(),
        100,
        0.0,
        "SELECT 1",
        10,
        capabilities=capabilities,
    )

    assert failure is not None
    assert mode is base.EnforcementMode.NONE


def test_streamlit_serializes_manager_matrix_without_alias_rows() -> None:
    streamlit_manager = DBPluginManager()

    assert streamlit_manager.get_capability_matrix() == (
        plugin_manager.serialize_support_matrix()
    )
    infos = streamlit_manager.list_plugins()
    assert len(infos) == 6
    for info in infos:
        assert info.capabilities == plugin_manager.get_capabilities(
            info.scheme
        ).to_mapping()
        assert info.supported_features == sorted(
            name
            for name, value in info.capabilities.items()
            if name in _CAPABILITY_FIELDS and value["state"] == "supported"
        )


def test_optional_driver_manifest_is_actionable() -> None:
    manifest = Path("requirements-db-optional.txt").read_text(encoding="utf-8")

    assert "impyla" in manifest
    assert "pyodbc" in manifest
    assert "vendor ODBC driver" in manifest
