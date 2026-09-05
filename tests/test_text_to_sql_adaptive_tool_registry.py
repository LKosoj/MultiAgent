"""Closed lazy registry contract for the ten adaptive research tools."""

from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest
from pydantic import ValidationError
import yaml

from custom_tools.text_to_sql.adaptive.controller import (
    NormalizedToolResult,
    ToolCall,
    ToolInvocation,
)
from custom_tools.text_to_sql.adaptive.models import (
    ColumnRef,
    DocumentRef,
    EvidenceCost,
    ResearchActionKind,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.probes import (
    ProbeResult,
    ProbeStatus,
    build_probe_result,
)
from custom_tools.text_to_sql.adaptive.tool_registry import (
    ADAPTIVE_RESEARCH_DEFINITIONS_DIR,
    ADAPTIVE_RESEARCH_TOOL_NAMES,
    ExecuteResearchProbeArguments,
    GetDistinctValuesArguments,
    InspectColumnArguments,
    InspectRelationshipsArguments,
    InspectTableArguments,
    MAX_RESEARCH_PARAMETERS,
    MAX_RESEARCH_PARAMETER_STRING_CHARS,
    MAX_SEARCH_VALUE_STRING_CHARS,
    ProfileColumnArguments,
    ReadSchemaEvidenceArguments,
    SampleRowsArguments,
    SearchSchemaCatalogArguments,
    SearchValueArguments,
    AdaptiveResearchToolContext,
    AdaptiveResearchToolRegistry,
)
from workflow.adaptive_state_store import AdaptiveLoopKind


EXPECTED_NAMES = (
    "search_schema_catalog",
    "inspect_table",
    "inspect_column",
    "inspect_relationships",
    "profile_column",
    "sample_rows",
    "search_value",
    "get_distinct_values",
    "execute_research_probe",
    "read_schema_evidence",
)
SCHEMA_VERSION = "sha256:" + "a" * 64


def _table() -> dict[str, object]:
    return {"namespace": "main", "schema": None, "table": "orders"}


VALID_ARGUMENTS = {
    "search_schema_catalog": {"query": "orders", "top_k": 3},
    "inspect_table": {"table": "orders"},
    "inspect_column": {"table": "orders", "column": "status"},
    "inspect_relationships": {"table": "orders", "top_k": 3, "depth": 2},
    "profile_column": {"table": "orders", "column": "status"},
    "sample_rows": {"table": "orders", "columns": ["id", "status"], "limit": 3},
    "search_value": {
        "table": "orders",
        "column": "status",
        "value": "open",
        "top_k": 3,
    },
    "get_distinct_values": {"table": "orders", "column": "status", "top_k": 3},
    "execute_research_probe": {
        "sql": "SELECT id, status FROM orders ORDER BY id LIMIT 2",
        "parameters": [],
    },
    "read_schema_evidence": {"document_id": "schema-doc"},
}

INVALID_ARGUMENTS = {
    "search_schema_catalog": {"query": "orders", "top_k": 0},
    "inspect_table": {"table": ""},
    "inspect_column": {"table": "orders"},
    "inspect_relationships": {"table": "orders", "top_k": 3, "depth": 5},
    "profile_column": {"table": "orders", "column": ""},
    "sample_rows": {"table": "orders", "columns": [], "limit": 3},
    "search_value": {
        "table": "orders",
        "column": "status",
        "value": [],
        "top_k": 3,
    },
    "get_distinct_values": {"table": "orders", "column": "status", "top_k": 51},
    "execute_research_probe": {
        "sql": "SELECT id FROM orders ORDER BY id LIMIT 2",
        "parameters": [{"not": "a scalar"}],
    },
    "read_schema_evidence": {"document_id": "bad id"},
}


def _invocation(name: str, arguments: dict[str, object]) -> ToolInvocation:
    return ToolInvocation(
        run_id="run-1",
        run_incarnation="incarnation-1",
        loop_kind=AdaptiveLoopKind.RESEARCH,
        revision=0,
        tool_call=ToolCall("call-1", name, arguments),
        invocation_id="invocation-1",
        remaining_seconds=5.0,
        deadline=None,
    )


def _context(
    claims: list[tuple[ResearchActionKind, object, tuple[tuple[str, object], ...]]]
    | None = None,
) -> AdaptiveResearchToolContext:
    captured = claims if claims is not None else []

    def make_budget(kind, target, parameters) -> object:
        captured.append((kind, target, parameters))
        return SimpleNamespace(
            state=SimpleNamespace(
                run_id="run-1",
                run_incarnation="incarnation-1",
                revision=0,
            ),
            action=SimpleNamespace(
                expected_revision=0,
                kind=kind,
                target=target,
                parameters=parameters,
            ),
            invocation_id="invocation-1",
            maximum_cost=object(),
            config=object(),
            ledger=object(),
        )

    return AdaptiveResearchToolContext(
        schema_runtime=SimpleNamespace(
            name="schema-runtime",
            table_namespace="main",
            documents=(SimpleNamespace(document_id="schema-doc", namespace="main"),),
        ),
        data_runtime=SimpleNamespace(
            name="data-runtime",
            dsn="sqlite://ignored",
            table_namespace="main",
            namespace=SimpleNamespace(version_key="a" * 64),
            get_plugin=lambda _dsn: SimpleNamespace(dialect="sqlite"),
        ),
        budget_factory=make_budget,
    )


def _failed_result(claim, *, invocation_id: str = "invocation-1") -> ProbeResult:
    kind, target, _parameters = claim
    now = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    return build_probe_result(
        run_id="run-1",
        run_incarnation="incarnation-1",
        revision=0,
        schema_namespace_version=SCHEMA_VERSION,
        invocation_id=invocation_id,
        action_digest="sha256:" + "b" * 64,
        probe_kind=kind,
        status=ProbeStatus.FAILED,
        target=target,
        started_at=now,
        completed_at=now,
        summary="typed test failure",
        cost=EvidenceCost(
            wall_clock_ms=0,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=0,
        ),
        row_count=0,
        failure_code="typed_test_failure",
    )


def test_registry_has_exact_names_and_local_yaml_parity() -> None:
    registry = AdaptiveResearchToolRegistry(_context())
    paths = sorted(ADAPTIVE_RESEARCH_DEFINITIONS_DIR.glob("*.yaml"))
    documents = [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]

    assert ADAPTIVE_RESEARCH_TOOL_NAMES == EXPECTED_NAMES
    assert registry.names == EXPECTED_NAMES
    assert tuple(sorted(document["name"] for document in documents)) == tuple(
        sorted(EXPECTED_NAMES)
    )
    assert len(paths) == len(EXPECTED_NAMES)
    assert all(document["implementation"] == document["name"] for document in documents)
    assert all("." not in document["implementation"] for document in documents)


def test_public_logical_argument_aliases_are_the_exact_registry_models() -> None:
    from custom_tools.text_to_sql.adaptive import tool_registry

    assert tool_registry._REQUEST_MODELS == {
        "search_schema_catalog": SearchSchemaCatalogArguments,
        "inspect_table": InspectTableArguments,
        "inspect_column": InspectColumnArguments,
        "inspect_relationships": InspectRelationshipsArguments,
        "profile_column": ProfileColumnArguments,
        "sample_rows": SampleRowsArguments,
        "search_value": SearchValueArguments,
        "get_distinct_values": GetDistinctValuesArguments,
        "execute_research_probe": ExecuteResearchProbeArguments,
        "read_schema_evidence": ReadSchemaEvidenceArguments,
    }


def test_constructor_and_off_path_do_no_io_import_or_adapter_build() -> None:
    script = """
from pathlib import Path
import sys
from types import SimpleNamespace
from custom_tools.text_to_sql.adaptive.tool_registry import (
    AdaptiveResearchToolContext, AdaptiveResearchToolRegistry,
)
original = Path.read_text
Path.read_text = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("YAML read"))
try:
    context = AdaptiveResearchToolContext(
        schema_runtime=object(), data_runtime=object(),
        budget_factory=lambda kind, target, parameters: object(),
    )
    registry = AdaptiveResearchToolRegistry(context)
    assert len(registry.names) == 10
    assert registry.cached_names == ()
    assert "custom_tools.text_to_sql.adaptive.schema_probes" not in sys.modules
    assert "custom_tools.text_to_sql.adaptive.data_probes" not in sys.modules
finally:
    Path.read_text = original
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_descriptions_and_adapters_load_lazily_and_cache_stably() -> None:
    registry = AdaptiveResearchToolRegistry(_context())

    assert registry.cached_names == ()
    assert (
        registry.description("inspect_table")
        == "Inspect one exact table's structural metadata."
    )
    assert registry.cached_names == ()
    first = registry.resolve("inspect_table")
    second = registry.resolve("inspect_table")

    assert first is second
    assert registry.cached_names == ("inspect_table",)
    assert registry.resolve("unknown") is None
    assert registry.description("unknown") is None


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_each_adapter_accepts_its_exact_valid_arguments_and_returns_typed_json(
    monkeypatch,
    name: str,
) -> None:
    claims = []
    context = _context(claims)
    registry = AdaptiveResearchToolRegistry(context)
    seen: dict[str, object] = {}

    def execute(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _failed_result(claims[-1])

    if name == "execute_research_probe":
        from custom_tools.text_to_sql.adaptive import data_probes

        monkeypatch.setattr(data_probes, "execute_raw_research_query", execute)
    else:
        from custom_tools.text_to_sql.adaptive import probes

        implementation_name = (
            "get_distinct_values_probe" if name == "get_distinct_values" else name
        )
        monkeypatch.setattr(
            probes,
            implementation_name,
            execute,
        )

    result = registry.resolve(name).execute(_invocation(name, VALID_ARGUMENTS[name]))

    assert isinstance(result, NormalizedToolResult)
    assert result.status == "error"
    assert result.value["contract_name"] == "probe_result"
    assert result.value["failure_code"] == "typed_test_failure"
    assert len(claims) == 1
    assert claims[0][0] in set(ResearchActionKind)
    assert seen["kwargs"]["runtime"] is (
        context.schema_runtime
        if name
        in {
            "search_schema_catalog",
            "inspect_table",
            "inspect_column",
            "inspect_relationships",
            "read_schema_evidence",
        }
        else context.data_runtime
    )
    assert seen["kwargs"]["budget"].action.target == claims[0][1]
    _assert_exact_w2_arguments(name, seen["args"])


def _assert_exact_w2_arguments(name: str, arguments: tuple[object, ...]) -> None:
    table = TableRef(namespace="main", schema=None, table="orders")
    column = ColumnRef(table=table, column="status")
    expected = {
        "search_schema_catalog": (table, 3),
        "inspect_table": (table,),
        "inspect_column": (column,),
        "inspect_relationships": (table, 3, 2),
        "profile_column": (column,),
        "sample_rows": (table, ("id", "status"), 3),
        "search_value": (column, "open", 3),
        "get_distinct_values": (column, 3),
        "read_schema_evidence": (
            DocumentRef(document_id="schema-doc", namespace="main"),
        ),
    }
    if name == "execute_research_probe":
        from custom_tools.text_to_sql.adaptive.research_query import RawResearchQuery

        assert arguments == (
            RawResearchQuery(
                sql="SELECT id, status FROM orders ORDER BY id LIMIT 2",
                parameters=(),
            ),
        )
    else:
        assert arguments == expected[name]


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_each_adapter_rejects_invalid_and_extra_model_arguments_before_budget(
    name: str,
) -> None:
    claims = []
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve(name)

    with pytest.raises(ValidationError):
        adapter.execute(_invocation(name, INVALID_ARGUMENTS[name]))
    with pytest.raises(ValidationError):
        adapter.execute(
            _invocation(name, {**VALID_ARGUMENTS[name], "runtime": "model-controlled"})
        )

    assert claims == []


@pytest.mark.parametrize(
    ("name", "forbidden"),
    [
        ("search_schema_catalog", {"target": _table()}),
        ("inspect_table", {"namespace": "other"}),
        ("inspect_column", {"schema": "private"}),
        ("read_schema_evidence", {"target": {"document_id": "schema-doc"}}),
    ],
)
def test_model_cannot_supply_target_namespace_or_schema_before_budget(
    name: str,
    forbidden: dict[str, object],
) -> None:
    claims = []
    arguments = {**VALID_ARGUMENTS[name], **forbidden}
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve(name)

    with pytest.raises(ValidationError):
        adapter.execute(_invocation(name, arguments))

    assert claims == []


def test_unknown_schema_document_is_rejected_before_budget() -> None:
    claims = []
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve(
        "read_schema_evidence"
    )

    with pytest.raises(ValueError, match="allowlisted"):
        adapter.execute(
            _invocation("read_schema_evidence", {"document_id": "unknown-doc"})
        )

    assert claims == []


@pytest.mark.parametrize(
    "parameters",
    [
        [None] * (MAX_RESEARCH_PARAMETERS + 1),
        ["x" * (MAX_RESEARCH_PARAMETER_STRING_CHARS + 1)],
    ],
)
def test_raw_parameter_bounds_reject_before_budget(parameters) -> None:
    claims = []
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve(
        "execute_research_probe"
    )

    with pytest.raises(ValidationError):
        adapter.execute(
            _invocation(
                "execute_research_probe",
                {
                    "sql": "SELECT id FROM orders ORDER BY id LIMIT 1",
                    "parameters": parameters,
                },
            )
        )

    assert claims == []


def test_raw_parameter_count_and_string_bounds_are_inclusive(monkeypatch) -> None:
    from custom_tools.text_to_sql.adaptive import data_probes

    claims = []
    parameters = ["x" * MAX_RESEARCH_PARAMETER_STRING_CHARS] + [None] * (
        MAX_RESEARCH_PARAMETERS - 1
    )
    monkeypatch.setattr(
        data_probes,
        "execute_raw_research_query",
        lambda *args, **kwargs: _failed_result(claims[-1]),
    )
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve(
        "execute_research_probe"
    )

    adapter.execute(
        _invocation(
            "execute_research_probe",
            {
                "sql": "SELECT id FROM orders ORDER BY id LIMIT 1",
                "parameters": parameters,
            },
        )
    )

    assert len(claims[0][2]) == MAX_RESEARCH_PARAMETERS
    assert claims[0][2][0][1] == "x" * MAX_RESEARCH_PARAMETER_STRING_CHARS


def test_search_value_string_bound_is_exact_and_rejects_before_budget(
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import probes

    claims = []
    monkeypatch.setattr(
        probes,
        "search_value",
        lambda *args, **kwargs: _failed_result(claims[-1]),
    )
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve("search_value")
    exact = {
        **VALID_ARGUMENTS["search_value"],
        "value": "x" * MAX_SEARCH_VALUE_STRING_CHARS,
    }
    adapter.execute(_invocation("search_value", exact))
    assert len(claims) == 1

    with pytest.raises(ValidationError):
        adapter.execute(
            _invocation(
                "search_value",
                {**exact, "value": "x" * (MAX_SEARCH_VALUE_STRING_CHARS + 1)},
            )
        )
    assert len(claims) == 1


def test_adapter_recovery_rejects_mismatched_invocation_before_normalization(
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import policy, probes

    claims = []
    monkeypatch.setattr(
        probes,
        "inspect_table",
        lambda *args, **kwargs: pytest.fail("recovery must not execute the probe"),
    )
    monkeypatch.setattr(
        policy,
        "recover_probe_with_budget",
        lambda *args, **kwargs: _failed_result(
            claims[-1],
            invocation_id="other-invocation",
        ),
    )
    adapter = AdaptiveResearchToolRegistry(_context(claims)).resolve("inspect_table")

    with pytest.raises(ValueError, match="invocation"):
        adapter.recover(_invocation("inspect_table", {"table": "orders"}))

    assert len(claims) == 1


def test_claims_use_only_trusted_runtime_and_canonical_action_parameters(
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import probes

    claims = []
    context = _context(claims)
    monkeypatch.setattr(
        probes,
        "sample_rows",
        lambda *args, **kwargs: _failed_result(claims[-1]),
    )
    adapter = AdaptiveResearchToolRegistry(context).resolve("sample_rows")

    adapter.execute(_invocation("sample_rows", VALID_ARGUMENTS["sample_rows"]))

    kind, target, parameters = claims[0]
    assert kind is ResearchActionKind.SAMPLE_ROWS
    assert target == TableRef.model_validate(_table())
    assert parameters == (("column_000", "id"), ("column_001", "status"), ("limit", 3))


def test_raw_json_parameter_list_becomes_immutable_and_preclaims_schema_free(
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import data_probes

    claims = []
    seen: dict[str, object] = {}
    context = _context(claims)

    def execute(query, *, runtime, budget):
        seen["query"] = query
        seen["runtime"] = runtime
        seen["budget"] = budget
        return _failed_result(claims[-1])

    monkeypatch.setattr(data_probes, "execute_raw_research_query", execute)
    arguments = {
        "sql": "SELECT id FROM orders WHERE status = ? ORDER BY id LIMIT 2",
        "parameters": ["open"],
    }
    adapter = AdaptiveResearchToolRegistry(context).resolve("execute_research_probe")

    adapter.execute(_invocation("execute_research_probe", arguments))

    assert seen["query"].parameters == ("open",)
    assert seen["runtime"] is context.data_runtime
    assert claims[0][0] is ResearchActionKind.EXECUTE_PROBE
    assert claims[0][2] == (("parameter_000", "open"),)
    assert claims[0][1] == seen["budget"].action.target


def test_local_distinct_name_never_resolves_the_legacy_helper(monkeypatch) -> None:
    from custom_tools import sql_tools
    from custom_tools.text_to_sql.adaptive import probes

    claims = []
    monkeypatch.setattr(
        sql_tools,
        "get_distinct_values",
        lambda *args, **kwargs: pytest.fail("legacy helper was called"),
    )
    monkeypatch.setattr(
        probes,
        "get_distinct_values_probe",
        lambda *args, **kwargs: _failed_result(claims[-1]),
    )

    result = (
        AdaptiveResearchToolRegistry(_context(claims))
        .resolve("get_distinct_values")
        .execute(
            _invocation("get_distinct_values", VALID_ARGUMENTS["get_distinct_values"])
        )
    )

    assert result.value["probe_kind"] == ResearchActionKind.DISTINCT_VALUES.value


def test_registry_does_not_register_adaptive_definitions_globally() -> None:
    root_definitions = Path(__file__).parents[1] / "tool_definitions"

    assert ADAPTIVE_RESEARCH_DEFINITIONS_DIR.parent.name == "adaptive"
    assert ADAPTIVE_RESEARCH_DEFINITIONS_DIR != root_definitions
    assert (root_definitions / "get_distinct_values.yaml").is_file()
    assert not any(
        path.stem in set(EXPECTED_NAMES) - {"get_distinct_values"}
        for path in root_definitions.glob("*.yaml")
    )


def test_raw_unsafe_query_is_claimed_once_and_returns_typed_failure_with_one_live_load(
    tmp_path: Path,
) -> None:
    from tests.test_text_to_sql_research_query import (
        INCARNATION,
        RUN_ID,
        _budget,
        _sqlite_execution_fixture,
    )

    loaded, loader, plugin, runtime = _sqlite_execution_fixture(tmp_path)
    claims = []
    ledgers = []

    def make_budget(kind, target, parameters):
        claims.append((kind, target, parameters))
        identity = SimpleNamespace(
            target=target,
            action_parameters=parameters,
        )
        budget, ledger = _budget(
            tmp_path,
            loaded.namespace,
            identity,
            suffix="registry-star",
        )
        ledgers.append(ledger)
        return replace(budget, invocation_id="invocation-1")

    context = AdaptiveResearchToolContext(
        schema_runtime=object(),
        data_runtime=runtime,
        budget_factory=make_budget,
    )
    try:
        result = (
            AdaptiveResearchToolRegistry(context)
            .resolve("execute_research_probe")
            .execute(
                replace(
                    _invocation(
                        "execute_research_probe",
                        {
                            "sql": "SELECT s.* FROM sales_fact AS s LIMIT 2",
                            "parameters": [],
                        },
                    ),
                    run_id=RUN_ID,
                    run_incarnation=INCARNATION,
                )
            )
        )

        assert result.status == "error"
        assert result.value["status"] == ProbeStatus.FAILED.value
        assert result.value["failure_code"] == "research_query_star"
        assert len(claims) == 1
        assert loader.calls == 1
        assert ledgers[0].claims == 1
        assert plugin.executions == 0
    finally:
        for ledger in ledgers:
            ledger.close()


def test_controller_resumes_registry_result_without_reexecution_and_rejects_tampering(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive.controller import (
        AdaptiveLoopController,
        ToolCall,
    )
    from custom_tools.text_to_sql.adaptive.models import ResearchAction
    from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
    from custom_tools.text_to_sql.adaptive.schema_probes import SchemaProbeBudgetRuntime
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
    from tests.test_text_to_sql_research_query import (
        INCARNATION,
        RUN_ID,
        _CountingLedger,
        _config,
        _sqlite_execution_fixture,
        _state,
        _supervised,
    )
    from workflow.adaptive_state_store import AdaptiveStateStore
    from custom_tools.text_to_sql.core._db_exec import QueryExecutor

    class _Model:
        def __init__(self, actions):
            self.actions = list(actions)
            self.calls = 0
            self.histories = []

        def next_action(self, history):
            self.calls += 1
            self.histories.append(history)
            return self.actions.pop(0)

    loaded, loader, _plugin, runtime = _sqlite_execution_fixture(tmp_path)
    executor_calls = []
    execute_query = QueryExecutor.execute

    def count_execution(self, request):
        executor_calls.append(request)
        return execute_query(self, request)

    monkeypatch.setattr(QueryExecutor, "execute", count_execution)
    config = _config()
    state = _state(loaded.namespace, config)
    ledger = _CountingLedger(tmp_path / "registry-recovery-budget.sqlite")
    factory_calls = []
    invocation_id = (
        "invoke:"
        + canonical_digest(
            {
                "run_id": RUN_ID,
                "run_incarnation": INCARNATION,
                "loop_kind": AdaptiveLoopKind.SOLVER.value,
                "tool_call_id": "call-1",
            }
        ).split(":", 1)[1]
    )

    def make_budget(kind, target, parameters):
        factory_calls.append((kind, target, parameters))
        action = ResearchAction(
            action_id="registry-recovery-action",
            kind=kind,
            hypothesis_id=None,
            target=target,
            parameters=parameters,
            action_digest=canonical_action_digest(
                kind=kind,
                hypothesis_id=None,
                target=target,
                parameters=parameters,
                expected_revision=0,
            ),
            expected_revision=0,
        )
        return SchemaProbeBudgetRuntime(
            state=state,
            action=action,
            maximum_cost=EvidenceCost(
                wall_clock_ms=1_000,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=1_000,
                rows=2,
                bytes=200_000,
            ),
            config=config,
            ledger=ledger,
            invocation_id=invocation_id,
        )

    registry = AdaptiveResearchToolRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=object(),
            data_runtime=runtime,
            budget_factory=make_budget,
        )
    )
    tool_action = {
        "kind": "tool",
        "tool_call_id": "call-1",
        "tool_name": "execute_research_probe",
        "arguments": {
            "sql": (
                "SELECT sale_id, sale_value FROM sales_fact ORDER BY sale_id LIMIT 2"
            ),
            "parameters": [],
        },
    }
    initial_model = _Model([tool_action])
    state_store = AdaptiveStateStore(tmp_path / "registry-recovery-state.sqlite")
    captured = {}

    def crash_before_observed(invocation, _result):
        captured["invocation"] = invocation
        raise RuntimeError("crash before OBSERVED")

    initial = AdaptiveLoopController(
        model=initial_model,
        tools=registry,
        state_store=state_store,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        loop_kind=AdaptiveLoopKind.SOLVER,
        before_observed=crash_before_observed,
    )
    try:
        with pytest.raises(RuntimeError, match="before OBSERVED"):
            _supervised(initial.run)

        resumed_model = _Model([{"kind": "final", "answer": "recovered"}])
        resumed = AdaptiveLoopController(
            model=resumed_model,
            tools=registry,
            state_store=state_store,
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            loop_kind=AdaptiveLoopKind.SOLVER,
        )
        result = resumed.run()

        assert result.answer == "recovered"
        assert len(result.accepted_results) == 1
        assert initial_model.calls == 1
        assert resumed_model.calls == 1
        assert len(factory_calls) == 2
        assert loader.calls == 1
        assert ledger.claims == 1
        assert result.accepted_results[0].result.value["status"] == "success", (
            result.accepted_results[0].result.value
        )
        assert len(executor_calls) == 1

        invocation = captured["invocation"]
        adapter = registry.resolve("execute_research_probe")
        with pytest.raises(ValueError):
            adapter.recover(
                replace(
                    invocation,
                    tool_call=ToolCall(
                        "call-1",
                        "execute_research_probe",
                        {
                            "sql": (
                                "SELECT sale_value FROM sales_fact "
                                "ORDER BY sale_id LIMIT 2"
                            ),
                            "parameters": (),
                        },
                    ),
                )
            )
        with pytest.raises(ValueError, match="exact tool invocation"):
            adapter.recover(replace(invocation, run_id="tampered-run"))
        calls_after_claim_tamper = len(factory_calls)
        with pytest.raises(ValueError, match="name"):
            adapter.recover(
                replace(
                    invocation,
                    tool_call=ToolCall(
                        "call-1", "inspect_table", {"table": "sales_fact"}
                    ),
                )
            )
        assert len(factory_calls) == calls_after_claim_tamper
        assert loader.calls == 1
        assert ledger.claims == 1
        assert len(executor_calls) == 1
    finally:
        ledger.close()


def test_controller_registry_recovery_rejects_other_invocation_without_observed_or_execute(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from custom_tools.text_to_sql.adaptive import probes
    from custom_tools.text_to_sql.adaptive.controller import (
        AdaptiveLoopController,
        AdaptiveLoopIndeterminateError,
        AdaptiveLoopToolFailure,
        MappingToolResolver,
    )
    from custom_tools.text_to_sql.adaptive.policy import reserve_probe_budget
    from custom_tools.text_to_sql.adaptive.schema_probes import SchemaProbeBudgetRuntime
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
    from tests.test_text_to_sql_research_policy import (
        INCARNATION,
        RUN_ID,
        _action,
        _config,
        _cost,
        _failed_result as _policy_failed_result,
        _state,
        _table as _policy_table,
    )
    from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
    from workflow.adaptive_state_store import (
        AdaptiveCheckpointKey,
        AdaptiveStateStore,
    )

    class _CountingLedger(AdaptiveBudgetLedger):
        def __init__(self, path):
            self.claims = 0
            super().__init__(path)

        def claim_execution(self, *args, **kwargs):
            self.claims += 1
            return super().claim_execution(*args, **kwargs)

    class _Model:
        def __init__(self, actions):
            self.actions = list(actions)
            self.calls = 0

        def next_action(self, _history):
            self.calls += 1
            return self.actions.pop(0)

    class _MismatchedResultAdapter:
        def execute(self, _invocation):
            budget = make_budget(
                ResearchActionKind.INSPECT_TABLE,
                _policy_table(),
                (),
            )
            reservation = reserve_probe_budget(
                budget.state,
                budget.action,
                budget.maximum_cost,
                config=budget.config,
                ledger=budget.ledger,
            )
            assert budget.ledger.claim_execution(
                reservation,
                "other-owner",
                now_ns=0,
            )
            budget.ledger.record_result(
                reservation,
                _policy_failed_result(
                    budget.action,
                    invocation_id="other-invocation",
                ),
                owner_token="other-owner",
            )
            raise TimeoutError("response lost after durable mismatched result")

    execute_calls = []
    monkeypatch.setattr(
        probes,
        "inspect_table",
        lambda *args, **kwargs: execute_calls.append((args, kwargs)),
    )
    config = _config()
    state = _state()
    ledger = _CountingLedger(tmp_path / "mismatched-invocation-budget.sqlite")
    expected_invocation_id = (
        "invoke:"
        + canonical_digest(
            {
                "run_id": RUN_ID,
                "run_incarnation": INCARNATION,
                "loop_kind": AdaptiveLoopKind.RESEARCH.value,
                "tool_call_id": "call-1",
            }
        ).split(":", 1)[1]
    )
    factory_calls = []

    def make_budget(kind, target, parameters):
        factory_calls.append((kind, target, parameters))
        return SchemaProbeBudgetRuntime(
            state=state,
            action=_action(
                0,
                kind=kind,
                target=target,
                parameters=parameters,
            ),
            maximum_cost=_cost(rows=2),
            config=config,
            ledger=ledger,
            invocation_id=expected_invocation_id,
        )

    state_store = AdaptiveStateStore(tmp_path / "mismatched-invocation-state.sqlite")
    tool_action = {
        "kind": "tool",
        "tool_call_id": "call-1",
        "tool_name": "inspect_table",
        "arguments": {"table": "orders"},
    }
    initial_model = _Model([tool_action])
    initial = AdaptiveLoopController(
        model=initial_model,
        tools=MappingToolResolver({"inspect_table": _MismatchedResultAdapter()}),
        state_store=state_store,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        loop_kind=AdaptiveLoopKind.RESEARCH,
    )
    registry = AdaptiveResearchToolRegistry(
        AdaptiveResearchToolContext(
            schema_runtime=SimpleNamespace(
                table_namespace="main",
                documents=(),
            ),
            data_runtime=object(),
            budget_factory=make_budget,
        )
    )
    resumed_model = _Model([{"kind": "final", "answer": "must-not-run"}])
    resumed = AdaptiveLoopController(
        model=resumed_model,
        tools=registry,
        state_store=state_store,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        loop_kind=AdaptiveLoopKind.RESEARCH,
    )
    key = AdaptiveCheckpointKey(
        RUN_ID,
        INCARNATION,
        AdaptiveLoopKind.RESEARCH,
        0,
    )
    try:
        with pytest.raises(AdaptiveLoopToolFailure):
            initial.run()
        with pytest.raises(AdaptiveLoopIndeterminateError, match="recovery failed"):
            resumed.run()

        snapshot = state_store.get_snapshot(key)
        assert snapshot.planned is not None
        assert snapshot.observed is None
        assert initial_model.calls == 1
        assert resumed_model.calls == 0
        assert len(factory_calls) == 2
        assert ledger.claims == 1
        assert execute_calls == []
        assert ledger.load_records(RUN_ID, INCARNATION)[0].reconciliation is None
    finally:
        ledger.close()
