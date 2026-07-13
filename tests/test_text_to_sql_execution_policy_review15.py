"""Adversarial contracts for the review15 Text-to-SQL runtime boundary."""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from collections.abc import Mapping

import pytest

from db_plugins.base import (
    Capability,
    DatabaseCapabilities,
    EnforcementMode,
    PluginHealth,
)
import workflow.text_to_sql_contract as text_to_sql_contract
from workflow.models import (
    TEXT_TO_SQL_MAX_JSON_NODES,
    TextToSqlTerminalResult,
    normalize_text_to_sql_json_value,
    text_to_sql_executor_contract_error,
)


class _AdmittedPluginDouble:
    dialect = "sqlite"

    def get_capabilities(self, _dsn=None):
        native = Capability.supported(EnforcementMode.DRIVER, "TEST_NATIVE")
        return DatabaseCapabilities(
            dialect=self.dialect,
            read_only=native,
            statement_timeout=native,
            cancellation=native,
            explain=native,
            introspection=native,
            composite_fk_introspection=Capability.unsupported("TEST_NOT_REQUIRED"),
            parameter_binding=Capability.unsupported("TEST_NOT_REQUIRED"),
        )

    def probe_capabilities(self, _conn=None, dsn=None):
        return PluginHealth(
            self.dialect,
            self.get_capabilities(dsn),
            True,
            ("TEST_PROBE_OK",),
        )

    def set_statement_timeout(self, _conn, _timeout_ms):
        return None


class _HostileError(Exception):
    def __str__(self):
        raise RuntimeError("hostile error str")

    def __repr__(self):
        raise RuntimeError("hostile error repr")


class _HostileAdapterResult:
    def __str__(self):
        raise RuntimeError("hostile object str")

    def __repr__(self):
        raise RuntimeError("hostile object repr")


def _nested_arrays(depth: int):
    value = 1
    for _ in range(depth):
        value = [value]
    return value


class _OversizedMapping(Mapping):
    def __init__(self, *, prefix=None):
        self.prefix = dict(prefix or {})
        self.yielded = 0

    def __len__(self):
        return 0

    def __iter__(self):
        yield from self.prefix
        for index in range(TEXT_TO_SQL_MAX_JSON_NODES + 10):
            self.yielded += 1
            yield f"extra_{index}"

    def __getitem__(self, key):
        return self.prefix.get(key)


class _LazyList(list):
    def __init__(self, children, counter):
        super().__init__()
        self.children = children
        self.counter = counter

    def __len__(self):
        return 0

    def __iter__(self):
        for child in self.children:
            self.counter["yielded"] += 1
            yield child


class _LazyMapping(Mapping):
    def __init__(self, children, counter):
        self.children = dict(enumerate(children))
        self.counter = counter

    def __len__(self):
        return 0

    def __iter__(self):
        for key in self.children:
            self.counter["yielded"] += 1
            yield str(key)

    def __getitem__(self, key):
        return self.children[int(key)]


def _configure_runtime(
    monkeypatch, *, strategy_result=None, strategy_error=None, close_error=None
):
    core = importlib.import_module("custom_tools.text_to_sql.core")
    calls: list[str] = []

    class Plugin(_AdmittedPluginDouble):
        def connect(self, _dsn):
            calls.append("connect")
            return object()

        def execute_select(self, *_args, **_kwargs):
            calls.append("strategy")
            if strategy_error is not None:
                raise strategy_error
            return strategy_result

        def close(self, _conn):
            calls.append("close")
            if close_error is not None:
                raise close_error

    monkeypatch.setattr(core, "get_plugin", lambda _dsn: Plugin())
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda *_args, **_kwargs: {"is_safe": True, "issues": []},
    )
    return core, calls


def _finalize(
    monkeypatch, *, strategy_result=None, strategy_error=None, close_error=None
):
    core, calls = _configure_runtime(
        monkeypatch,
        strategy_result=strategy_result,
        strategy_error=strategy_error,
        close_error=close_error,
    )
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    audit_entries = []
    persistence_calls = []

    def audit_logger(entry):
        calls.append("audit")
        audit_entries.append(entry)
        return {"status": "logged", "log_id": "audit-review15"}

    monkeypatch.setattr(core, "audit_logger", audit_logger)

    def save_successful_sql(**kwargs):
        persistence_calls.append(kwargs)
        return {"status": "saved", "filename": "q.md", "path": "/q.md"}

    monkeypatch.setattr(core, "save_successful_sql", save_successful_sql)
    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-review15",
        "run-review15",
    )
    return result, calls, audit_entries, persistence_calls


def _valid_strategy_result():
    return {
        "success": True,
        "data": [[1]],
        "columns": ["one"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
    }


def _valid_executor_result():
    return {
        **_valid_strategy_result(),
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": "SELECT 1",
        "applied_row_limit": 10,
    }


def _executor_contract_error(result):
    return text_to_sql_executor_contract_error(
        result,
        expected_dry_run_only=False,
        expected_sql_query="SELECT 1",
        expected_row_limit=10,
    )


def test_enforcement_modes_remain_optional_as_a_pair(monkeypatch):
    assert _executor_contract_error(_valid_executor_result()) is None

    terminal_result, _calls, _audit_entries, _persistence_calls = _finalize(
        monkeypatch,
        strategy_result=_valid_strategy_result(),
    )
    terminal_result["execution"].pop("timeout_enforcement_mode")
    terminal_result["execution"].pop("cancellation_enforcement_mode")

    parsed = TextToSqlTerminalResult.from_mapping(terminal_result)

    assert "timeout_enforcement_mode" not in parsed.execution
    assert "cancellation_enforcement_mode" not in parsed.execution


@pytest.mark.parametrize(
    ("enforcement_modes", "error_match"),
    [
        pytest.param(
            {"timeout_enforcement_mode": "driver"},
            "require",
            id="missing-pair",
        ),
        pytest.param(
            {
                "timeout_enforcement_mode": "future",
                "cancellation_enforcement_mode": "driver",
            },
            "must be one of",
            id="unknown-mode",
        ),
    ],
)
def test_enforcement_modes_are_strict_at_executor_and_terminal_boundaries(
    monkeypatch,
    enforcement_modes,
    error_match,
):
    executor_result = {**_valid_executor_result(), **enforcement_modes}

    error = _executor_contract_error(executor_result)

    assert error is not None
    assert error_match in error

    terminal_result, _calls, _audit_entries, _persistence_calls = _finalize(
        monkeypatch,
        strategy_result=_valid_strategy_result(),
    )
    terminal_result["execution"].pop("timeout_enforcement_mode")
    terminal_result["execution"].pop("cancellation_enforcement_mode")
    terminal_result["execution"].update(enforcement_modes)

    with pytest.raises(ValueError, match=error_match):
        TextToSqlTerminalResult.from_mapping(terminal_result)


@pytest.mark.parametrize("exception_name", ["RuntimeError", "SystemExit"])
def test_bounded_error_is_total_for_hostile_str_repr_and_type_name(exception_name):
    # SystemExit is a BaseException, not an Exception: it must also be
    # absorbed, or the documented "never raise" contract of
    # bound_text_to_sql_error/text_to_sql_type_name would break.
    script = textwrap.dedent(
        """
        from workflow.models import bound_text_to_sql_error, text_to_sql_type_name

        class HostileMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise EXCEPTION_NAME("hostile type name")
                return super().__getattribute__(name)
            def __str__(cls):
                raise EXCEPTION_NAME("hostile type str")
            def __repr__(cls):
                raise EXCEPTION_NAME("hostile type repr")

        class Hostile(metaclass=HostileMeta):
            def __str__(self):
                raise EXCEPTION_NAME("hostile str")
            def __repr__(self):
                raise EXCEPTION_NAME("hostile repr")

        result = bound_text_to_sql_error(Hostile())
        assert isinstance(result, str) and result

        type_name = text_to_sql_type_name(Hostile())
        assert isinstance(type_name, str) and type_name
        """
    ).replace("EXCEPTION_NAME", exception_name)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("boundary", ["strategy", "close"])
def test_hostile_post_strategy_error_is_attempted_execution(monkeypatch, boundary):
    kwargs = (
        {"strategy_error": _HostileError()}
        if boundary == "strategy"
        else {
            "strategy_result": _valid_strategy_result(),
            "close_error": _HostileError(),
        }
    )
    result, calls, _audit_entries, persistence_calls = _finalize(
        monkeypatch,
        **kwargs,
    )

    assert calls == ["connect", "strategy", "close", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["executed"] is True
    assert result["dry_run"] is False
    assert result["execution"]["success"] is False
    assert result["execution"]["skipped_execution"] is False
    assert result["persistence"] == {"status": "not_attempted"}
    assert persistence_calls == []


@pytest.mark.parametrize(
    "strategy_result",
    [
        pytest.param({**_valid_strategy_result(), "success": "yes"}, id="success"),
        pytest.param({**_valid_strategy_result(), "data": "rows"}, id="data"),
        pytest.param({**_valid_strategy_result(), "columns": [1]}, id="columns"),
        pytest.param(
            {**_valid_strategy_result(), "data": [[1, 2]]},
            id="row-column-shape",
        ),
        pytest.param(
            {**_valid_strategy_result(), "rows_affected": True},
            id="rows-affected",
        ),
        pytest.param(
            {**_valid_strategy_result(), "execution_time_ms": "1"},
            id="execution-time",
        ),
        pytest.param({**_valid_strategy_result(), "unknown": 1}, id="unknown"),
        pytest.param(
            {**_valid_strategy_result(), "data": [[1]] * 11},
            id="row-limit",
        ),
    ],
)
def test_post_strategy_semantic_error_becomes_execution_failure(
    monkeypatch,
    strategy_result,
):
    result, calls, _audit_entries, persistence_calls = _finalize(
        monkeypatch,
        strategy_result=strategy_result,
    )

    assert calls == ["connect", "strategy", "close", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["executed"] is True
    assert result["execution"]["success"] is False
    assert result["execution"]["skipped_execution"] is False
    assert persistence_calls == []


def test_executor_owns_sql_limit_and_execution_markers(monkeypatch):
    forged = {
        **_valid_strategy_result(),
        "sql_query": "SELECT secret",
        "applied_row_limit": 999,
        "dry_run_only": True,
        "skipped_execution": True,
    }
    core, calls = _configure_runtime(monkeypatch, strategy_result=forged)

    result = core.secure_db_executor(
        "SELECT 1",
        row_limit=10,
        dsn="sqlite:///unused.db",
        dry_run_only=False,
    )

    assert calls == ["connect", "strategy", "close"]
    assert result["success"] is True
    assert result["sql_query"] == "SELECT 1"
    assert result["applied_row_limit"] == 10
    assert result["dry_run_only"] is False
    assert result["skipped_execution"] is False


def test_post_strategy_depth_limit_fails_before_persistence(monkeypatch):
    over_deep = {
        **_valid_strategy_result(),
        "data": _nested_arrays(500),
        "columns": [],
        "rows_affected": 0,
    }

    result, calls, audit_entries, persistence_calls = _finalize(
        monkeypatch,
        strategy_result=over_deep,
    )

    assert calls == ["connect", "strategy", "close", "audit"]
    assert result["status"] == "failed"
    assert result["reason_code"] == "EXECUTION_FAILED"
    assert result["executed"] is True
    assert result["execution"]["skipped_execution"] is False
    assert len(str(audit_entries[0])) < 10_000
    assert persistence_calls == []


def test_terminal_from_mapping_rejects_over_deep_json_without_recursion_error():
    mapping = {
        "run_id": "run-review15",
        "status": "succeeded",
        "reason_code": "",
        "sql": "SELECT 1",
        "generated": True,
        "approved": True,
        "executed": True,
        "dry_run": False,
        "audited": True,
        "data": _nested_arrays(500),
        "columns": [],
        "rows_affected": 0,
        "error": None,
        "execution": {
            **_valid_strategy_result(),
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": "SELECT 1",
            "applied_row_limit": 10,
        },
        "audit": {"status": "logged", "log_id": "audit-review15"},
        "persistence": {"status": "saved", "filename": "q.md", "path": "/q.md"},
    }

    with pytest.raises(ValueError, match="depth|nesting"):
        TextToSqlTerminalResult.from_mapping(mapping)


def test_json_preflight_enforces_total_node_budget():
    too_wide = [None] * TEXT_TO_SQL_MAX_JSON_NODES

    with pytest.raises(ValueError, match="node limit"):
        normalize_text_to_sql_json_value(too_wide, field_name="too_wide")


def test_json_preflight_bounds_arbitrary_mapping_iteration():
    oversized = _OversizedMapping()

    with pytest.raises(ValueError, match="node limit"):
        normalize_text_to_sql_json_value(oversized, field_name="oversized")

    assert oversized.yielded <= TEXT_TO_SQL_MAX_JSON_NODES


def test_terminal_from_mapping_preflights_before_field_copies():
    oversized = _OversizedMapping(
        prefix={
            "run_id": "run-review15",
            "status": "failed",
            "reason_code": "DB_AUDIT_MISSING",
            "sql": "",
            "generated": False,
            "approved": False,
            "executed": False,
            "dry_run": False,
            "audited": False,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "error": "missing",
            "execution": {},
            "audit": {},
            "persistence": {"status": "not_attempted"},
        }
    )

    with pytest.raises(ValueError, match="node limit"):
        TextToSqlTerminalResult.from_mapping(oversized)

    assert oversized.yielded <= TEXT_TO_SQL_MAX_JSON_NODES


@pytest.mark.parametrize("container_type", ["sequence", "mapping"])
def test_json_node_budget_reserves_scheduled_nested_siblings(
    monkeypatch,
    container_type: str,
) -> None:
    node_limit = 20
    monkeypatch.setattr(text_to_sql_contract, "TEXT_TO_SQL_MAX_JSON_NODES", node_limit)
    counter = {"yielded": 0}
    container = _LazyList if container_type == "sequence" else _LazyMapping
    nested = [container([None] * 30, counter) for _ in range(10)]
    value = container(nested, counter)

    with pytest.raises(ValueError, match="node limit"):
        normalize_text_to_sql_json_value(value, field_name="nested-lazy")

    assert counter["yielded"] <= node_limit


@pytest.mark.parametrize("adapter", ["audit", "persistence"])
def test_hostile_adapter_exception_never_escapes(monkeypatch, adapter):
    core, _calls = _configure_runtime(
        monkeypatch,
        strategy_result=_valid_strategy_result(),
    )
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    if adapter == "audit":
        monkeypatch.setattr(
            core,
            "audit_logger",
            lambda _entry: (_ for _ in ()).throw(_HostileError()),
        )
        monkeypatch.setattr(
            core,
            "save_successful_sql",
            lambda **_kwargs: pytest.fail("audit failure must not persist"),
        )
    else:
        monkeypatch.setattr(
            core,
            "audit_logger",
            lambda _entry: {"status": "logged", "log_id": "audit-review15"},
        )
        monkeypatch.setattr(
            core,
            "save_successful_sql",
            lambda **_kwargs: (_ for _ in ()).throw(_HostileError()),
        )

    result = terminal.finalize_text_to_sql_run(
        "SELECT 1",
        "one",
        "Approved",
        "sqlite:///unused.db",
        10,
        False,
        "session-review15",
        "run-review15",
    )

    assert result["status"] == "failed" if adapter == "audit" else "succeeded"
    assert result["audit"]["status"] == ("error" if adapter == "audit" else "logged")
    assert result["persistence"]["status"] == (
        "not_attempted" if adapter == "audit" else "error"
    )


def test_hostile_adapter_result_type_never_escapes(monkeypatch):
    core, _calls = _configure_runtime(
        monkeypatch,
        strategy_result=_valid_strategy_result(),
    )
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(core, "audit_logger", lambda _entry: _HostileAdapterResult())
    monkeypatch.setattr(
        core,
        "save_successful_sql",
        lambda **_kwargs: pytest.fail("invalid audit adapter must not persist"),
    )

    try:
        result = terminal.finalize_text_to_sql_run(
            "SELECT 1",
            "one",
            "Approved",
            "sqlite:///unused.db",
            10,
            False,
            "session-review15",
            "run-review15",
        )
    except BaseException:
        result = None

    assert result is not None
    assert result["status"] == "failed"
    assert result["reason_code"] == "AUDIT_CONTRACT_INVALID"
    assert result["audit"]["status"] == "error"
