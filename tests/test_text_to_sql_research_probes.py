"""Focused W2-02 tests for immutable probe results and evidence conversion."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive import schema_probes as schema_probe_module
from custom_tools.text_to_sql.adaptive.evidence import (
    ProbeEvidenceError,
    probe_result_to_evidence,
)
from custom_tools.text_to_sql.adaptive.freshness import (
    FreshnessContext,
    FreshnessStatus,
    evaluate_evidence_freshness,
)
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    ColumnRef,
    DocumentRef,
    EvidenceCost,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    QuerySpec,
    QueryProbeRef,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.probes import (
    ProbeArtifactError,
    ProbeResult,
    ProbeResultDecodeError,
    ProbeResultError,
    ProbeStatus,
    build_probe_result,
    deserialize_probe_result,
    execute_research_probe,
    get_distinct_values_probe,
    inspect_column,
    inspect_relationships,
    inspect_table,
    profile_column,
    read_probe_payload,
    read_schema_evidence,
    sample_rows,
    search_schema_catalog,
    search_value,
    serialize_probe_result,
)
from custom_tools.text_to_sql.adaptive.provenance import ProbeProvenance
from custom_tools.text_to_sql.adaptive.policy import (
    MAX_ACTIONS,
    MAX_DB_PROBE_MS,
    MAX_DB_PROBES,
    MAX_INLINE_BYTES,
    MAX_MODEL_DECISIONS,
    MAX_MODEL_TOKENS,
    MAX_RETURNED_ROWS,
    MAX_SAMPLE_ROWS,
    MAX_WALL_CLOCK_SECONDS,
    AdaptivePolicyConfig,
    OperationCountBudget,
    PerActionBudget,
    ResourceBudget,
    ResultVolumeBudget,
    WallClockBudget,
    canonical_action_digest,
    initial_budget_state,
)
from custom_tools.text_to_sql.adaptive.schema_probes import (
    SchemaEvidenceDocument,
    SchemaProbeBudgetRuntime,
    SchemaProbeRuntime,
)
from custom_tools.text_to_sql.adaptive.data_probes import (
    DataProbeRuntime,
    ResearchQueryTemplate,
    _cost as _data_probe_cost,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    ArtifactReference,
    SerializationLimits,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive.state import (
    ResearchTransitionConflictError,
    ResearchTransitionReferenceError,
    apply_research_transition,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema, SchemaLoader
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from db_plugins.base import (
    Capability,
    DatabaseCapabilities,
    EnforcementMode,
    PluginHealth,
)
from tests.fixtures.text_to_sql_adaptive.sqlite import (
    create_sqlite_adaptive_fixture,
)
from workflow.adaptive_budget_ledger import AdaptiveBudgetLedger
from workflow.deadline import DeadlineBudget
from tool_runtime_context import (
    SupervisorExecutionEvidence,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)


RUN_ID = "run-1"
INCARNATION = "incarnation-1"
SCHEMA = "sha256:" + "a" * 64
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _table(name: str = "orders") -> TableRef:
    return TableRef(namespace="main", schema=None, table=name)


def _action(
    *,
    action_id: str = "action-1",
    revision: int = 0,
    detail: str = "full",
    target: TableRef | None = None,
) -> ResearchAction:
    selected_target = target or _table()
    parameters = (("detail", detail),)
    return ResearchAction(
        action_id=action_id,
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=selected_target,
        parameters=parameters,
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=selected_target,
            parameters=parameters,
            expected_revision=revision,
        ),
        expected_revision=revision,
    )


def _cost(payload: object, *, rows: int = 1) -> EvidenceCost:
    return EvidenceCost(
        wall_clock_ms=5,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=4,
        rows=rows,
        bytes=len(canonical_json_bytes(payload)),
    )


def _result(
    payload: object,
    *,
    action: ResearchAction | None = None,
    invocation_id: str = "invocation-1",
    rows: int = 1,
    truncated: bool = False,
    limits: SerializationLimits = SerializationLimits(),
    store: _ArtifactStore | None = None,
) -> ProbeResult:
    producer = action or _action()
    return build_probe_result(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=producer.expected_revision,
        schema_namespace_version=SCHEMA,
        invocation_id=invocation_id,
        action_digest=producer.action_digest,
        probe_kind=producer.kind,
        status=ProbeStatus.SUCCESS,
        target=producer.target,
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=5),
        summary="orders observation",
        cost=_cost(payload, rows=rows),
        row_count=rows,
        truncated=truncated,
        payload=payload,
        limits=limits,
        write_artifact=store.write if store is not None else None,
        read_artifact=store.read if store is not None else None,
    )


def _budget() -> BudgetState:
    values: dict[str, int] = {}
    for name in (
        "wall_clock_ms",
        "model_calls",
        "model_tokens",
        "db_probe_ms",
        "rows",
        "bytes",
    ):
        values[f"initial_{name}"] = 100
        values[f"used_{name}"] = 0
        values[f"remaining_{name}"] = 100
    return BudgetState(**values)


def _state() -> ResearchState:
    query = QuerySpec(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=None,
        query_id="query-1",
        original_text="orders",
        semantic_items=(),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=(),
        result_expectations=(),
        budget_state=_budget(),
        stop_reason=None,
    )


def _schema_config() -> AdaptivePolicyConfig:
    return AdaptivePolicyConfig(
        policy_version=1,
        wall_clock=WallClockBudget(wall_clock_seconds=MAX_WALL_CLOCK_SECONDS),
        resource_limits=ResourceBudget(
            model_tokens=MAX_MODEL_TOKENS,
            db_probe_ms=MAX_DB_PROBE_MS,
        ),
        operation_counts=OperationCountBudget(
            actions=MAX_ACTIONS,
            model_decisions=MAX_MODEL_DECISIONS,
            db_probes=MAX_DB_PROBES,
        ),
        result_volume=ResultVolumeBudget(
            returned_rows=MAX_RETURNED_ROWS,
            inline_bytes=MAX_INLINE_BYTES,
        ),
        per_action=PerActionBudget(sample_rows=MAX_SAMPLE_ROWS),
    )


def _schema_scope(*, access_scope_id: str = "scope-1") -> SchemaScope:
    return SchemaScope.from_mapping(
        {
            "serialization_version": 1,
            "tenant_id": "tenant-1",
            "access_scope_id": access_scope_id,
            "connection_view_id": "connection-1",
            "transient": True,
        }
    )


def _schema_state(namespace: SchemaNamespace) -> ResearchState:
    version = f"sha256:{namespace.version_key}"
    config = _schema_config()
    query = QuerySpec(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=version,
        query_id="schema-query",
        original_text="inspect schema",
        semantic_items=(),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=version,
        query_spec=query,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=(),
        action_history=(),
        result_expectations=(),
        budget_state=initial_budget_state(config),
        stop_reason=None,
    )


def _schema_action(
    kind: ResearchActionKind,
    target: TableRef | ColumnRef | DocumentRef,
    parameters: tuple[tuple[str, int], ...] = (),
) -> ResearchAction:
    return ResearchAction(
        action_id="schema-action",
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


def _schema_budget_runtime(
    tmp_path: Path,
    namespace: SchemaNamespace,
    kind: ResearchActionKind,
    target: TableRef | ColumnRef | DocumentRef,
    *,
    parameters: tuple[tuple[str, int], ...] = (),
    suffix: str = "probe",
    rows: int = 50,
    bytes_: int = 200_000,
) -> tuple[SchemaProbeBudgetRuntime, AdaptiveBudgetLedger]:
    ledger = AdaptiveBudgetLedger(tmp_path / f"{suffix}-budget.sqlite")
    runtime = SchemaProbeBudgetRuntime(
        state=_schema_state(namespace),
        action=_schema_action(kind, target, parameters),
        maximum_cost=EvidenceCost(
            wall_clock_ms=1_000,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1_000,
            rows=rows,
            bytes=bytes_,
        ),
        config=_schema_config(),
        ledger=ledger,
        invocation_id=f"invocation-{suffix}",
    )
    return runtime, ledger


def _sqlite_schema_runtime(
    tmp_path: Path,
    fixture_id: str,
    *,
    ddl_order: str = "canonical",
) -> tuple[SchemaProbeRuntime, Path]:
    database = create_sqlite_adaptive_fixture(
        fixture_id,
        tmp_path / f"{fixture_id}-{ddl_order}.sqlite",
        ddl_order=ddl_order,
    )
    dsn = f"sqlite://{database}"
    loader = SchemaLoader(tmp_path / f"schema-cache-{fixture_id}-{ddl_order}")
    scope = _schema_scope()
    loaded = loader.load_scoped_schema({}, dsn, scope)
    return (
        SchemaProbeRuntime(
            loader=loader,
            dsn=dsn,
            scope=scope,
            namespace=loaded.namespace,
            table_namespace="main",
            deadline=DeadlineBudget.from_duration(30),
        ),
        database,
    )


def _column(table: str, column: str) -> ColumnRef:
    return ColumnRef(table=_table(table), column=column)


def _data_action(
    kind: ResearchActionKind,
    target: TableRef | ColumnRef | QueryProbeRef,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...] = (),
) -> ResearchAction:
    return ResearchAction(
        action_id="data-action",
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


def _data_budget_runtime(
    tmp_path: Path,
    namespace: SchemaNamespace,
    kind: ResearchActionKind,
    target: TableRef | ColumnRef | QueryProbeRef,
    *,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...] = (),
    suffix: str,
    rows: int = 50,
    bytes_: int = 200_000,
) -> tuple[SchemaProbeBudgetRuntime, AdaptiveBudgetLedger]:
    ledger = AdaptiveBudgetLedger(tmp_path / f"{suffix}-budget.sqlite")
    return (
        SchemaProbeBudgetRuntime(
            state=_schema_state(namespace),
            action=_data_action(kind, target, parameters),
            maximum_cost=EvidenceCost(
                wall_clock_ms=1_000,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=1_000,
                rows=rows,
                bytes=bytes_,
            ),
            config=_schema_config(),
            ledger=ledger,
            invocation_id=f"invocation-{suffix}",
        ),
        ledger,
    )


def _sqlite_data_runtime(
    tmp_path: Path,
    fixture_id: str,
    *,
    templates: tuple[ResearchQueryTemplate, ...] = (),
) -> DataProbeRuntime:
    schema_runtime, _ = _sqlite_schema_runtime(tmp_path, fixture_id)
    return DataProbeRuntime(
        loader=schema_runtime.loader,
        dsn=schema_runtime.dsn,
        scope=schema_runtime.scope,
        namespace=schema_runtime.namespace,
        table_namespace=schema_runtime.table_namespace,
        deadline=schema_runtime.deadline,
        query_templates=templates,
    )


def _run_supervised(call):
    token = set_tool_runtime_context(
        {"supervisor_evidence": SupervisorExecutionEvidence("data-probe-test", 1)}
    )
    try:
        return call()
    finally:
        reset_tool_runtime_context(token)


def _capabilities(
    *,
    introspection: bool = True,
    composite: bool = True,
) -> DatabaseCapabilities:
    supported = Capability.supported(EnforcementMode.DATABASE, "TEST_SUPPORTED")
    unsupported = Capability.unsupported("TEST_UNSUPPORTED")
    return DatabaseCapabilities(
        dialect="mock",
        read_only=supported,
        statement_timeout=supported,
        cancellation=supported,
        explain=supported,
        introspection=supported if introspection else unsupported,
        composite_fk_introspection=supported if composite else unsupported,
        parameter_binding=supported,
    )


class _DataPlugin:
    dialect = "sqlite"

    def __init__(
        self,
        *,
        rows: list[tuple[object, ...]],
        columns: list[str],
        capabilities: DatabaseCapabilities | None = None,
    ) -> None:
        self.rows = rows
        self.columns = columns
        self.capabilities = capabilities or _capabilities()
        self.calls: list[object] = []

    def get_capabilities(self, _dsn=None):
        return self.capabilities

    def probe_capabilities(self, _conn=None, _dsn=None):
        self.calls.append("probe")
        return PluginHealth("mock", self.capabilities, True, ("TEST_OK",))

    def connect(self, dsn):
        self.calls.append(("connect", dsn))
        return object()

    def close(self, _conn):
        self.calls.append("close")

    def set_statement_timeout(self, _conn, timeout_ms):
        self.calls.append(("timeout", timeout_ms))

    def quote_identifier(self, identifier):
        return ".".join(f'"{part}"' for part in identifier.split("."))

    def quote_identifier_part(self, identifier):
        return f'"{identifier}"'

    def execute_select(self, _conn, sql, row_limit):
        self.calls.append(("execute", sql, row_limit))
        return {
            "success": True,
            "data": self.rows[:row_limit],
            "columns": self.columns,
            "rows_affected": min(len(self.rows), row_limit),
            "error_message": None,
        }

    def execute_select_bound(self, _conn, sql, parameters, row_limit):
        self.calls.append(("execute_bound", sql, parameters, row_limit))
        return self.execute_select(_conn, sql, row_limit)


def _mock_data_runtime(
    schema: dict[str, object],
    plugin: _DataPlugin,
    *,
    deadline: DeadlineBudget | None = None,
    monotonic_ns=None,
) -> DataProbeRuntime:
    schema_runtime = _mock_schema_runtime(schema, capabilities=plugin.capabilities)
    return DataProbeRuntime(
        loader=schema_runtime.loader,
        dsn="sqlite:///tmp/trusted-data-probe.db",
        scope=schema_runtime.scope,
        namespace=schema_runtime.namespace,
        table_namespace=schema_runtime.table_namespace,
        deadline=deadline or schema_runtime.deadline,
        get_plugin=lambda _dsn: plugin,
        monotonic_ns=monotonic_ns or time.monotonic_ns,
    )


def _mock_schema_runtime(
    schema: dict[str, object],
    *,
    scope: SchemaScope | None = None,
    capabilities: DatabaseCapabilities | None = None,
) -> SchemaProbeRuntime:
    trusted_scope = scope or _schema_scope()
    namespace = SchemaNamespace(
        scope=trusted_scope,
        schema_fingerprint=canonical_schema_fingerprint(schema),
    )
    loader = SimpleNamespace(
        load_scoped_schema=lambda _schema_info, _dsn, _scope: LoadedSchema(
            schema,
            namespace,
            "mock",
        )
    )
    plugin = SimpleNamespace(
        get_capabilities=lambda _dsn: capabilities or _capabilities()
    )
    return SchemaProbeRuntime(
        loader=loader,
        dsn="mock://trusted",
        scope=trusted_scope,
        namespace=namespace,
        table_namespace="main",
        deadline=DeadlineBudget.from_duration(30),
        get_plugin=lambda _dsn: plugin,
    )


class _ArtifactStore:
    def __init__(self) -> None:
        self.content: dict[str, bytes] = {}

    def write(self, payload: bytes) -> ArtifactReference:
        artifact_id = f"artifact-{len(self.content) + 1}"
        self.content[artifact_id] = payload
        return ArtifactReference(
            artifact_id=artifact_id,
            digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            byte_count=len(payload),
        )

    def read(self, reference: ArtifactReference) -> bytes:
        return self.content[reference.artifact_id]


def test_probe_result_round_trip_digest_is_byte_stable_and_order_independent() -> None:
    first_payload = {"rows": [{"b": 2, "a": 1}], "meta": {"z": True, "a": None}}
    second_payload = {"meta": {"a": None, "z": True}, "rows": [{"a": 1, "b": 2}]}

    first = _result(first_payload)
    second = _result(second_payload)
    encoded = serialize_probe_result(first)

    assert first.payload_digest == second.payload_digest
    assert encoded == serialize_probe_result(second)
    assert deserialize_probe_result(encoded) == first
    assert encoded == serialize_probe_result(deserialize_probe_result(encoded))


def test_legacy_probe_result_bytes_do_not_gain_provenance_fields() -> None:
    result = _result({"rows": [{"value": "legacy"}]})
    expected = (
        b'{"action_digest":"sha256:a3be46bd45f0f78c13e40bcea815529e50aaa25c40c94dc2005c9a1df3d329fe",'
        b'"artifact_reference":null,"byte_count":29,"completed_at":"2026-07-30T12:00:00.005000Z",'
        b'"contract_name":"probe_result","contract_version":1,"cost":{"bytes":29,"db_probe_ms":4,'
        b'"model_calls":0,"model_tokens":0,"rows":1,"wall_clock_ms":5},"failure_code":null,'
        b'"inline_payload_json":"{\\"rows\\":[{\\"value\\":\\"legacy\\"}]}",'
        b'"invocation_id":"invocation-1","payload_digest":"sha256:c67d87343df9558e4104e9d862534acfec4371650265e70c54a0bb88b4520b81",'
        b'"probe_kind":"inspect_table","revision":0,"row_count":1,"run_id":"run-1",'
        b'"run_incarnation":"incarnation-1","schema_namespace_version":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"started_at":"2026-07-30T12:00:00.000000Z","status":"success",'
        b'"summary":"orders observation","target":{"namespace":"main","schema":null,"table":"orders"},'
        b'"truncated":false}'
    )

    assert result.payload_digest == (
        "sha256:c67d87343df9558e4104e9d862534acfec4371650265e70c54a0bb88b4520b81"
    )
    assert serialize_probe_result(result) == expected.replace(
        b"sha256:a3be46bd45f0f78c13e40bcea815529e50aaa25c40c94dc2005c9a1df3d329fe",
        result.action_digest.encode(),
    )
    assert b"provenance" not in expected


def test_probe_result_is_deeply_immutable_and_revalidation_rejects_bypass() -> None:
    payload = {"rows": [{"value": 1}]}
    result = _result(payload)
    payload["rows"][0]["value"] = 999

    assert '"value":1' in result.inline_payload_json
    with pytest.raises(ValidationError):
        result.summary = "changed"
    bypassed = result.model_copy(update={"byte_count": result.byte_count + 1})
    with pytest.raises(ProbeResultError, match="strict contract"):
        serialize_probe_result(bypassed)


@pytest.mark.parametrize(
    "status",
    [ProbeStatus.FAILED, ProbeStatus.TIMED_OUT, ProbeStatus.CANCELLED],
)
def test_failed_probe_result_remains_typed_and_never_becomes_evidence(
    status: ProbeStatus,
) -> None:
    action = _action()
    result = build_probe_result(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=SCHEMA,
        invocation_id=f"invocation-{status.value}",
        action_digest=action.action_digest,
        probe_kind=action.kind,
        status=status,
        target=action.target,
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=5),
        summary="probe did not complete",
        cost=EvidenceCost(
            wall_clock_ms=5,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=4,
            rows=0,
            bytes=0,
        ),
        row_count=0,
        failure_code="probe_failure",
    )

    assert deserialize_probe_result(serialize_probe_result(result)) == result
    assert probe_result_to_evidence(result, action) is None
    with pytest.raises(ProbeResultError, match="cannot receive payload"):
        build_probe_result(
            run_id=result.run_id,
            run_incarnation=result.run_incarnation,
            revision=result.revision,
            schema_namespace_version=result.schema_namespace_version,
            invocation_id=result.invocation_id,
            action_digest=result.action_digest,
            probe_kind=result.probe_kind,
            status=result.status,
            target=result.target,
            started_at=result.started_at,
            completed_at=result.completed_at,
            summary=result.summary,
            cost=result.cost,
            row_count=result.row_count,
            failure_code=result.failure_code,
            payload={"unexpected": True},
        )


def test_probe_result_rejects_malformed_unknown_nonfinite_and_custom_payloads() -> None:
    result = _result({"rows": [{"value": 1}]})
    decoded = json.loads(serialize_probe_result(result))
    decoded["unknown"] = True
    with pytest.raises(ProbeResultDecodeError):
        deserialize_probe_result(json.dumps(decoded))
    decoded.pop("unknown")
    decoded["status"] = "unknown"
    with pytest.raises(ProbeResultDecodeError):
        deserialize_probe_result(json.dumps(decoded))
    with pytest.raises(ProbeResultDecodeError):
        deserialize_probe_result(b'{"contract_name":"probe_result",')

    action = _action()

    def build_invalid(payload: object) -> ProbeResult:
        return build_probe_result(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            invocation_id="invocation-invalid",
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=NOW,
            completed_at=NOW,
            summary="invalid payload",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=0,
                bytes=0,
            ),
            row_count=0,
            payload=payload,
        )

    with pytest.raises(ProbeResultError, match="finite canonical JSON"):
        build_invalid({"value": float("nan")})
    with pytest.raises(ProbeResultError, match="finite canonical JSON"):
        build_invalid({"value": object()})


def test_probe_artifact_size_and_row_boundaries_do_not_truncate_payload() -> None:
    one_row = {"rows": [{"value": "a"}]}
    two_rows = {"rows": [{"value": "a"}, {"value": "b"}]}
    limits = SerializationLimits(max_state_bytes=20_000, max_inline_rows=1)
    store = _ArtifactStore()

    inline = _result(one_row, limits=limits, store=store)
    external = _result(two_rows, rows=2, limits=limits, store=store)

    assert inline.inline_payload_json is not None
    assert external.inline_payload_json is None
    assert external.artifact_reference is not None
    assert store.read(external.artifact_reference) == canonical_json_bytes(two_rows)
    assert external.truncated is False

    boundary_payload = {"value": "x" * 5_000}
    candidate = _result(boundary_payload)
    exact_size = len(serialize_probe_result(candidate))
    exact = _result(
        boundary_payload,
        limits=SerializationLimits(max_state_bytes=exact_size, max_inline_rows=10),
        store=store,
    )
    below = _result(
        boundary_payload,
        limits=SerializationLimits(max_state_bytes=exact_size - 1, max_inline_rows=10),
        store=store,
    )
    assert exact.inline_payload_json is not None
    assert below.artifact_reference is not None

    large_payload = {"value": "x" * 70_000}
    large = _result(large_payload, store=store)
    assert large.artifact_reference is not None
    assert store.read(large.artifact_reference) == canonical_json_bytes(large_payload)


def test_probe_artifact_tamper_and_wrong_writer_payload_fail_closed() -> None:
    payload = {"rows": [{"value": "a"}, {"value": "b"}]}
    action = _action()
    store = _ArtifactStore()
    result = _result(
        payload,
        action=action,
        rows=2,
        limits=SerializationLimits(max_state_bytes=20_000, max_inline_rows=1),
        store=store,
    )
    reference = result.artifact_reference
    assert reference is not None
    store.content[reference.artifact_id] = (
        store.content[reference.artifact_id][:-1] + b"X"
    )

    with pytest.raises(ProbeArtifactError, match="verification failed"):
        probe_result_to_evidence(result, action, read_artifact=store.read)

    wrong_store = _ArtifactStore()

    def write_wrong(_: bytes) -> ArtifactReference:
        return wrong_store.write(b'{"wrong":true}')

    with pytest.raises(ProbeArtifactError, match="differs"):
        build_probe_result(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            invocation_id="invocation-wrong",
            action_digest=action.action_digest,
            probe_kind=action.kind,
            status=ProbeStatus.SUCCESS,
            target=action.target,
            started_at=NOW,
            completed_at=NOW,
            summary="wrong writer",
            cost=_cost(payload, rows=2),
            row_count=2,
            payload=payload,
            limits=SerializationLimits(max_state_bytes=20_000, max_inline_rows=1),
            write_artifact=write_wrong,
            read_artifact=wrong_store.read,
        )


def test_successful_probe_evidence_is_linked_and_accepted_by_state_reducer() -> None:
    action = _action()
    result = _result({"rows": [{"table": "orders"}]}, action=action, truncated=True)
    evidence = probe_result_to_evidence(result, action)

    assert evidence is not None
    assert evidence.action_digest == action.action_digest
    assert evidence.target == action.target
    assert evidence.revision == 1
    observation = json.loads(evidence.observation)
    provenance = ProbeProvenance.model_validate_json(
        canonical_json_bytes(observation["provenance"])
    )
    assert observation["truncated"] is True
    assert observation["observation_version"] == 1
    assert observation["payload"] == {"rows": [{"table": "orders"}]}
    assert provenance.invocation_id == result.invocation_id
    assert provenance.action_digest == result.action_digest
    assert provenance.target == result.target
    assert provenance.payload_digest == result.payload_digest
    assert evidence.validity_scope is EvidenceValidityScope.SCHEMA_VERSION
    transition = apply_research_transition(_state(), action, evidence=(evidence,))
    assert transition.state.evidence == (evidence,)
    assert result.revision == 0
    with pytest.raises(ValidationError):
        evidence.observation = "changed"


def test_probe_evidence_rejects_action_mismatch_and_reducer_rejects_wrong_target() -> (
    None
):
    action = _action()
    result = _result({"rows": [{"table": "orders"}]}, action=action)
    mismatched_action = _action(detail="columns")
    with pytest.raises(ProbeEvidenceError, match="action_digest"):
        probe_result_to_evidence(result, mismatched_action)

    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    wrong_target = evidence.model_copy(update={"target": _table("customers")})
    with pytest.raises(ResearchTransitionReferenceError, match="target"):
        apply_research_transition(_state(), action, evidence=(wrong_target,))

    wrong_source = evidence.model_copy(update={"source_kind": EvidenceSourceKind.PROBE})
    with pytest.raises(ResearchTransitionReferenceError, match="source"):
        apply_research_transition(_state(), action, evidence=(wrong_source,))


def test_probe_evidence_duplicate_action_and_evidence_are_rejected() -> None:
    action = _action()
    evidence = probe_result_to_evidence(
        _result({"rows": []}, action=action, rows=0), action
    )
    assert evidence is not None
    first = apply_research_transition(_state(), action, evidence=(evidence,)).state

    duplicate_action = _action(action_id="action-1", revision=1)
    with pytest.raises(ResearchTransitionConflictError, match="duplicate action"):
        apply_research_transition(first, duplicate_action)

    new_action = _action(action_id="action-3", revision=1, detail="columns")
    duplicate_evidence = evidence.model_copy(
        update={
            "revision": 2,
            "action_digest": new_action.action_digest,
        }
    )
    with pytest.raises(ResearchTransitionConflictError, match="evidence_id"):
        apply_research_transition(first, new_action, evidence=(duplicate_evidence,))


def test_artifact_backed_evidence_retains_provenance_without_inline_payload() -> None:
    action = _action()
    store = _ArtifactStore()
    result = _result(
        {"rows": [{"value": "a"}, {"value": "b"}]},
        action=action,
        rows=2,
        limits=SerializationLimits(max_state_bytes=20_000, max_inline_rows=1),
        store=store,
    )

    evidence = probe_result_to_evidence(result, action, read_artifact=store.read)

    assert evidence is not None
    observation = json.loads(evidence.observation)
    assert observation["payload"] is None
    assert observation["observation_version"] == 1
    assert observation["storage"] == "artifact"
    assert observation["provenance"]["payload_digest"] == result.payload_digest
    assert observation["provenance"]["invocation_id"] == result.invocation_id


@pytest.mark.parametrize(
    ("kind", "target", "source_kind"),
    [
        (
            ResearchActionKind.PROFILE_COLUMN,
            _column("orders", "status"),
            EvidenceSourceKind.PROFILE,
        ),
        (
            ResearchActionKind.SAMPLE_ROWS,
            _table(),
            EvidenceSourceKind.SAMPLE,
        ),
        (
            ResearchActionKind.SEARCH_VALUE,
            _column("orders", "status"),
            EvidenceSourceKind.VALUE_SEARCH,
        ),
        (
            ResearchActionKind.DISTINCT_VALUES,
            _column("orders", "status"),
            EvidenceSourceKind.VALUE_SEARCH,
        ),
        (
            ResearchActionKind.EXECUTE_PROBE,
            QueryProbeRef(probe_id="probe-1", namespace="main"),
            EvidenceSourceKind.PROBE,
        ),
    ],
)
def test_data_probe_evidence_is_always_run_only(kind, target, source_kind) -> None:
    action = _data_action(kind, target)
    result = _result({"rows": []}, action=action, rows=0)

    evidence = probe_result_to_evidence(result, action)

    assert evidence is not None
    assert evidence.source_kind is source_kind
    assert evidence.validity_scope is EvidenceValidityScope.RUN_ONLY
    assert evidence.data_snapshot_token is None


def test_schema_relationship_probe_uses_declared_f01_edges_without_reading_rows(
    tmp_path: Path,
) -> None:
    runtime, database = _sqlite_schema_runtime(tmp_path, "F01_CONVENTIONAL_STAR")
    target = _table("sales_fact")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 2), ("top_k", 10)),
        suffix="f01-relationships",
    )
    try:
        result = inspect_relationships(
            target,
            10,
            2,
            runtime=runtime,
            budget=budget,
        )
        payload = read_probe_payload(result)

        assert result.status is ProbeStatus.SUCCESS
        assert payload["status"] == "connected"
        assert {edge["relationship_kind"] for edge in payload["relationships"]} == {
            "declared"
        }
        assert {edge["to_table"] for edge in payload["relationships"]} == {
            "main.branch_dim",
            "main.day_dim",
        }
        assert payload["schema_namespace_version"] == result.schema_namespace_version
        assert ledger.load_records(RUN_ID, INCARNATION)[0].result == result
        with __import__("sqlite3").connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM sales_fact").fetchone() == (
                3,
            )
    finally:
        ledger.close()


def test_schema_relationship_probe_keeps_f04_disconnected_without_invention(
    tmp_path: Path,
) -> None:
    runtime, _ = _sqlite_schema_runtime(tmp_path, "F04_MISSING_DECLARED_FK")
    target = _table("ledger")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 2), ("top_k", 10)),
        suffix="f04-disconnected",
    )
    try:
        result = inspect_relationships(target, 10, 2, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "isolated"
        assert payload["relationships"] == []
        assert payload["paths"] == []
        assert payload["disconnected_count"] == 1
        assert payload["disconnected_tables"][0]["table"] == "depot"
    finally:
        ledger.close()


def test_schema_catalog_probe_reports_f05_ambiguity_instead_of_first_match(
    tmp_path: Path,
) -> None:
    runtime, _ = _sqlite_schema_runtime(tmp_path, "F05_AMBIGUOUS_BINDING")
    target = _table("state_value")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="f05-catalog",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "ambiguous"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "member_state_a",
            "member_state_b",
        ]
        assert all(
            item["matched_columns"] == ["state_value"] for item in payload["results"]
        )
    finally:
        ledger.close()


def test_schema_catalog_rrf_single_signal_preserves_legacy_order(
    tmp_path: Path,
) -> None:
    # Query "ord" hits three tables through the name signal only (no
    # description/column matches, no foreign keys). "ordinance" and
    # "orders" both prefix-match (score 3, tied); "border" only
    # substring-matches (score 2). With a single signal in play, RRF must
    # reproduce the legacy max()-based order and ambiguity exactly.
    schema = {
        "ordinance": {"columns": {"id": {"type": "INTEGER"}}},
        "orders": {"columns": {"id": {"type": "INTEGER"}}},
        "border": {"columns": {"id": {"type": "INTEGER"}}},
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("ord")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-single-signal",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "ambiguous"
        assert payload["total_matches"] == 3
        assert [item["table"]["table"] for item in payload["results"]] == [
            "orders",
            "ordinance",
            "border",
        ]
        assert [item["score"] for item in payload["results"]] == [3, 3, 2]
    finally:
        ledger.close()


def test_schema_catalog_rrf_promotes_multi_signal_table(tmp_path: Path) -> None:
    # "region_summary" has the higher single-signal score (name prefix, 3)
    # while "sales_region_ledger" only substring-matches the name (2) but
    # also substring-matches the description (2). Under the legacy
    # max()-based score, region_summary(3) would outrank
    # sales_region_ledger(2). RRF sums one fraction per matching signal, so
    # the two-signal table overtakes the single-signal one.
    schema = {
        "region_summary": {"columns": {"id": {"type": "INTEGER"}}},
        "sales_region_ledger": {
            "columns": {"id": {"type": "INTEGER"}},
            "description": "sales region notes",
        },
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("region")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-multi-signal",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "matched"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "sales_region_ledger",
            "region_summary",
        ]
    finally:
        ledger.close()


def test_schema_catalog_rrf_fk_neighbor_signal(tmp_path: Path) -> None:
    # "customer_account" and "customer_ledger" both prefix-match "customer"
    # (score 3): their lexical RRF is exactly tied, since name is the only
    # matching signal for both. "old_customer_archive" only substring-
    # matches (score 2), so its lexical RRF is strictly lower than the tied
    # pair's -- per the W3-3.1 tie-break design the FK signal can never
    # promote it past either of them (a weaker lexical match may not
    # displace a stronger one). FK adjacency is instead a pure tie-break:
    # among candidates whose lexical RRF is *exactly* equal, "customer_ledger"
    # wins over "customer_account" because it declares no FK of its own but
    # is the target of "old_customer_archive"'s FK, giving it a non-zero
    # tie-break count while "customer_account" has none. The tie being
    # resolved by a strict count difference is what makes the result
    # "matched" rather than "ambiguous".
    schema_with_fk = {
        "customer_account": {"columns": {"id": {"type": "INTEGER"}}},
        "customer_ledger": {"columns": {"id": {"type": "INTEGER"}}},
        "old_customer_archive": {
            "columns": {
                "id": {"type": "INTEGER"},
                "ledger_id": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "archive_ledger_fk",
                        "to_table": "customer_ledger",
                        "column_pairs": [
                            {"from_column": "ledger_id", "to_column": "id"}
                        ],
                    }
                ],
            },
        },
    }
    runtime = _mock_schema_runtime(schema_with_fk)
    target = _table("customer")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-fk-neighbor-with-fk",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "matched"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "customer_ledger",
            "customer_account",
            "old_customer_archive",
        ]
    finally:
        ledger.close()

    # Without any FK at all, "customer_account" and "customer_ledger" have
    # no signal left to break their exact lexical-RRF tie, so the probe
    # must report "ambiguous" instead of picking either one.
    schema_without_fk = {
        "customer_account": {"columns": {"id": {"type": "INTEGER"}}},
        "customer_ledger": {"columns": {"id": {"type": "INTEGER"}}},
    }
    runtime = _mock_schema_runtime(schema_without_fk)
    target = _table("customer")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-fk-neighbor-without-fk",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "ambiguous"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "customer_account",
            "customer_ledger",
        ]
    finally:
        ledger.close()


def test_schema_catalog_fk_tiebreak_never_displaces_lexical_leader(
    tmp_path: Path,
) -> None:
    # Regression for the W3-3.1 code-review blocker: the FK signal used to
    # be summed into the same Reciprocal Rank Fusion as the lexical
    # signals, so a table with a weak lexical match (score 2) but an FK
    # link to a top lexical candidate could outrank -- and, at top_k=1,
    # completely displace -- the single strongest lexical match (score 3).
    # "alpha_master" prefix-matches "alpha" (score 3, the sole lexical
    # leader). "gamma_alpha_hub" and "delta_alpha_node" only substring-match
    # (score 2). "weak_alpha_ref" also only substring-matches (score 2) but
    # declares a foreign key to "gamma_alpha_hub". Under the fixed
    # tie-break design, "alpha_master" must remain first regardless of any
    # FK adjacency among the weaker matches.
    schema = {
        "alpha_master": {"columns": {"id": {"type": "INTEGER"}}},
        "gamma_alpha_hub": {"columns": {"id": {"type": "INTEGER"}}},
        "delta_alpha_node": {"columns": {"id": {"type": "INTEGER"}}},
        "weak_alpha_ref": {
            "columns": {
                "id": {"type": "INTEGER"},
                "hub_id": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "weak_ref_hub_fk",
                        "to_table": "gamma_alpha_hub",
                        "column_pairs": [
                            {"from_column": "hub_id", "to_column": "id"}
                        ],
                    }
                ],
            },
        },
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("alpha")

    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="fk-tiebreak-top-k-10",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "matched"
        assert payload["results"][0]["table"]["table"] == "alpha_master"
        scores = [item["score"] for item in payload["results"]]
        assert scores == sorted(scores, reverse=True)
    finally:
        ledger.close()

    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 1),),
        suffix="fk-tiebreak-top-k-1",
    )
    try:
        result = search_schema_catalog(target, 1, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "matched"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "alpha_master"
        ]
    finally:
        ledger.close()


def test_schema_catalog_lexical_rrf_order_is_non_increasing() -> None:
    # The per-item "score" field is the legacy max()-based signal and is
    # allowed to be non-monotonic across results (see
    # test_schema_catalog_rrf_promotes_multi_signal_table: a two-signal
    # table can legitimately overtake a single, higher-scoring one). What
    # must always hold is that the fused *lexical* RRF value that actually
    # drives the ranking -- computed the same way search_schema_catalog
    # computes it, before any FK tie-break is applied -- is non-increasing
    # across the ranked output.
    candidates = [
        (3, "single_signal_leader", [], ["table"]),
        (2, "multi_signal_table", [], ["table", "table_description"]),
        (2, "single_signal_tail", [], ["table"]),
    ]
    name_scores = {
        "single_signal_leader": 3,
        "multi_signal_table": 2,
        "single_signal_tail": 2,
    }
    description_scores = {"multi_signal_table": 2}
    column_scores: dict[str, int] = {}

    ranked, is_ambiguous = schema_probe_module._rank_catalog_candidates(
        candidates, name_scores, description_scores, column_scores, []
    )

    assert is_ambiguous is False
    assert [name for _, name, _, _ in ranked] == [
        "multi_signal_table",
        "single_signal_leader",
        "single_signal_tail",
    ]

    lexical_rrf = schema_probe_module._reciprocal_rank_fusion(
        *(
            schema_probe_module._dense_rank(
                [(score, name) for name, score in scores.items()]
            )
            for scores in (name_scores, description_scores, column_scores)
        )
    )
    rrf_sequence = [lexical_rrf[name] for _, name, _, _ in ranked]
    assert rrf_sequence == sorted(rrf_sequence, reverse=True)


def test_schema_catalog_rrf_is_deterministic(tmp_path: Path) -> None:
    schema = {
        "region_summary": {"columns": {"id": {"type": "INTEGER"}}},
        "sales_region_ledger": {
            "columns": {"id": {"type": "INTEGER"}},
            "description": "sales region notes",
        },
        "unrelated_widget": {"columns": {"id": {"type": "INTEGER"}}},
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("region")
    payloads = []
    for index in range(2):
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_CATALOG,
            target,
            parameters=(("top_k", 10),),
            suffix=f"rrf-deterministic-{index}",
        )
        try:
            result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
            payloads.append(read_probe_payload(result))
        finally:
            ledger.close()

    assert payloads[0] == payloads[1]


def test_schema_catalog_rrf_ambiguity_uses_exact_fraction_ties(
    tmp_path: Path,
) -> None:
    # Case A: both tables match through the exact same single signal
    # (column name only) with the same score, like the pinned F05 fixture.
    # Their RRF fractions are exactly equal, so the probe must still report
    # "ambiguous".
    tied_schema = {
        "member_state_a": {"columns": {"state_value": {"type": "TEXT"}}},
        "member_state_b": {"columns": {"state_value": {"type": "TEXT"}}},
    }
    runtime = _mock_schema_runtime(tied_schema)
    target = _table("state_value")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-ambiguous-tie",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert payload["status"] == "ambiguous"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "member_state_a",
            "member_state_b",
        ]
    finally:
        ledger.close()

    # Case B: both tables share the same legacy max()-bucket (name prefix,
    # score 3) but "vendor_ledger" also has a weak column-description match.
    # The legacy int-bucket comparison would call this "ambiguous" too, but
    # the extra signal gives vendor_ledger a strictly larger RRF fraction,
    # so the probe must resolve it to "matched" instead.
    distinct_schema = {
        "vendor_summary": {"columns": {"id": {"type": "INTEGER"}}},
        "vendor_ledger": {
            "columns": {
                "id": {
                    "type": "INTEGER",
                    "description": "preferred vendor reference",
                }
            }
        },
    }
    runtime = _mock_schema_runtime(distinct_schema)
    target = _table("vendor")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-ambiguous-distinct",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert payload["status"] == "matched"
        assert [item["table"]["table"] for item in payload["results"]] == [
            "vendor_ledger",
            "vendor_summary",
        ]
    finally:
        ledger.close()


def test_schema_catalog_top_k_and_truncated_semantics_preserved(
    tmp_path: Path,
) -> None:
    schema = {
        "widget_alpha": {"columns": {"id": {"type": "INTEGER"}}},
        "widget_beta": {"columns": {"id": {"type": "INTEGER"}}},
        "widget_gamma": {"columns": {"id": {"type": "INTEGER"}}},
        "widget_delta": {"columns": {"id": {"type": "INTEGER"}}},
        "widget_epsilon": {"columns": {"id": {"type": "INTEGER"}}},
        "unrelated_gadget": {"columns": {"id": {"type": "INTEGER"}}},
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("widget")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 2),),
        suffix="rrf-top-k-small",
    )
    try:
        result = search_schema_catalog(target, 2, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert len(payload["results"]) == 2
        assert payload["total_matches"] == 5
        assert result.truncated is True
    finally:
        ledger.close()

    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_CATALOG,
        target,
        parameters=(("top_k", 10),),
        suffix="rrf-top-k-large",
    )
    try:
        result = search_schema_catalog(target, 10, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert len(payload["results"]) == 5
        assert payload["total_matches"] == 5
        assert result.truncated is False
    finally:
        ledger.close()


def test_schema_table_probe_reports_ambiguous_and_missing_targets(
    tmp_path: Path,
) -> None:
    schema = {
        "left.orders": {"columns": {"id": {"type": "INTEGER"}}},
        "right.orders": {"columns": {"id": {"type": "INTEGER"}}},
    }
    runtime = _mock_schema_runtime(schema)
    for suffix, target, expected in (
        ("ambiguous", _table("orders"), "ambiguous"),
        ("missing", _table("customers"), "missing"),
    ):
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_TABLE,
            target,
            suffix=f"table-{suffix}",
        )
        try:
            result = inspect_table(target, runtime=runtime, budget=budget)
            payload = read_probe_payload(result)
            assert payload["status"] == expected
            assert len(payload["candidates"]) == (2 if expected == "ambiguous" else 0)
        finally:
            ledger.close()


def test_schema_column_probe_returns_only_structural_metadata_and_fresh_evidence(
    tmp_path: Path,
) -> None:
    schema = {
        "main.link": {
            "columns": {
                "id": {
                    "type": "INTEGER",
                    "not_null": True,
                    "default_value": "7",
                    "constraint_type": "PRIMARY KEY",
                    "description": "Stable link key",
                }
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "link_parent",
                        "to_table": "main.parent",
                        "column_pairs": [{"from_column": "id", "to_column": "id"}],
                    }
                ],
            },
        },
        "main.parent": {
            "columns": {"id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}},
            "foreign_keys": {"complete": True, "constraints": []},
        },
        "main.audit": {
            "columns": {"link_id": {"type": "INTEGER"}},
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "audit_link",
                        "to_table": "main.link",
                        "column_pairs": [{"from_column": "link_id", "to_column": "id"}],
                    }
                ],
            },
        },
    }
    runtime = _mock_schema_runtime(schema)

    class SchemaOnlyPlugin:
        def __init__(self) -> None:
            self.calls = []

        def get_capabilities(self, _dsn):
            self.calls.append("get_capabilities")
            return _capabilities()

        def execute_select(self, *_args, **_kwargs):
            raise AssertionError("inspect_column must not read database rows")

    plugin = SchemaOnlyPlugin()
    runtime = replace(runtime, get_plugin=lambda _dsn: plugin)
    target = _column("link", "ID")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_COLUMN,
        target,
        suffix="column-matched",
    )
    try:
        result = inspect_column(target, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert result.status is ProbeStatus.SUCCESS
        assert result.row_count == 1
        assert payload["status"] == "matched"
        assert payload["column"] == {
            "column": "id",
            "table": {
                "namespace": "main",
                "schema": "main",
                "table": "link",
            },
        }
        assert payload["metadata"] == {
            "constraint": "PRIMARY KEY",
            "default": "7",
            "description": "Stable link key",
            "is_primary_key": True,
            "not_null": "True",
            "type": "INTEGER",
        }
        assert [
            edge["constraint_id"] for edge in payload["outgoing_relationships"]
        ] == ["link_parent"]
        assert [
            edge["constraint_id"] for edge in payload["incoming_relationships"]
        ] == ["audit_link"]
        assert plugin.calls == ["get_capabilities"]

        evidence = probe_result_to_evidence(result, budget.action)
        assert evidence is not None
        assert evidence.source_kind is EvidenceSourceKind.SCHEMA
        assert evidence.validity_scope is EvidenceValidityScope.SCHEMA_VERSION
        decision = evaluate_evidence_freshness(
            evidence,
            FreshnessContext(
                evaluated_at=result.completed_at,
                run_id=result.run_id,
                run_incarnation=result.run_incarnation,
                schema_namespace_version=result.schema_namespace_version,
            ),
        )
        assert decision.status is FreshnessStatus.FRESH
    finally:
        ledger.close()


def test_schema_column_probe_reports_table_and_column_resolution_without_guessing(
    tmp_path: Path,
) -> None:
    schema = {
        "left.orders": {
            "columns": {
                "Code": {"type": "TEXT"},
                "code": {"type": "TEXT"},
            }
        },
        "right.orders": {"columns": {"code": {"type": "TEXT"}}},
    }
    runtime = _mock_schema_runtime(schema)
    cases = (
        (
            "table-ambiguous",
            _column("orders", "code"),
            "ambiguous",
            2,
            0,
        ),
        (
            "column-ambiguous",
            ColumnRef(
                table=TableRef(
                    namespace="main",
                    schema="left",
                    table="orders",
                ),
                column="CODE",
            ),
            "ambiguous",
            0,
            2,
        ),
        (
            "column-missing",
            ColumnRef(
                table=TableRef(
                    namespace="main",
                    schema="right",
                    table="orders",
                ),
                column="missing",
            ),
            "missing",
            0,
            0,
        ),
    )
    for suffix, target, status, table_count, column_count in cases:
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_COLUMN,
            target,
            suffix=suffix,
        )
        try:
            result = inspect_column(target, runtime=runtime, budget=budget)
            payload = read_probe_payload(result)
            assert payload["status"] == status
            assert len(payload["candidate_tables"]) == table_count
            assert len(payload["candidate_columns"]) == column_count
        finally:
            ledger.close()


def test_schema_column_probe_treats_dotted_column_as_one_metadata_key(
    tmp_path: Path,
) -> None:
    schema = {
        "main.metrics": {
            "columns": {
                "sales.total": {
                    "type": "DECIMAL",
                    "description": "Precomputed total",
                }
            }
        }
    }
    runtime = _mock_schema_runtime(schema)
    target = _column("metrics", "sales.total")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_COLUMN,
        target,
        suffix="column-dotted-name",
    )
    try:
        result = inspect_column(target, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert payload["status"] == "matched"
        assert payload["column"]["column"] == "sales.total"
        assert payload["metadata"]["type"] == "DECIMAL"
    finally:
        ledger.close()


def test_schema_column_probe_rejects_scope_stale_and_capability_mismatches(
    tmp_path: Path,
) -> None:
    schema = {"main.orders": {"columns": {"id": {"type": "INTEGER"}}}}
    base = _mock_schema_runtime(schema)
    stale_namespace = SchemaNamespace(
        scope=base.scope,
        schema_fingerprint="f" * 64,
    )
    cases = (
        (
            base,
            ColumnRef(
                table=TableRef(namespace="other", schema=None, table="orders"),
                column="id",
            ),
            base.namespace,
            "target_namespace_mismatch",
        ),
        (
            replace(base, namespace=stale_namespace),
            _column("orders", "id"),
            stale_namespace,
            "schema_stale",
        ),
        (
            _mock_schema_runtime(
                schema,
                capabilities=_capabilities(introspection=False),
            ),
            _column("orders", "id"),
            None,
            "schema_introspection_unavailable",
        ),
    )
    for index, (runtime, target, namespace, failure_code) in enumerate(cases):
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            namespace or runtime.namespace,
            ResearchActionKind.INSPECT_COLUMN,
            target,
            suffix=f"column-mismatch-{index}",
        )
        try:
            result = inspect_column(target, runtime=runtime, budget=budget)
            assert result.status is ProbeStatus.FAILED
            assert result.failure_code == failure_code
        finally:
            ledger.close()


def test_schema_column_probe_preserves_proven_composite_fk_order(
    tmp_path: Path,
) -> None:
    schema = {
        "main.parent": {
            "columns": {
                "first": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "second": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
        "main.child": {
            "columns": {
                "parent_second": {"type": "INTEGER"},
                "parent_first": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "child_parent_fk",
                        "to_table": "main.parent",
                        "column_pairs": [
                            {
                                "from_column": "parent_second",
                                "to_column": "second",
                            },
                            {
                                "from_column": "parent_first",
                                "to_column": "first",
                            },
                        ],
                    }
                ],
            },
        },
    }
    target = _column("child", "parent_first")
    supported_runtime = _mock_schema_runtime(schema)
    supported_budget, supported_ledger = _schema_budget_runtime(
        tmp_path,
        supported_runtime.namespace,
        ResearchActionKind.INSPECT_COLUMN,
        target,
        suffix="column-composite-supported",
    )
    try:
        result = inspect_column(
            target,
            runtime=supported_runtime,
            budget=supported_budget,
        )
        payload = read_probe_payload(result)
        assert payload["outgoing_relationships"][0]["column_pairs"] == [
            {"from_column": "parent_second", "to_column": "second"},
            {"from_column": "parent_first", "to_column": "first"},
        ]
    finally:
        supported_ledger.close()

    unverified_runtime = _mock_schema_runtime(
        schema,
        capabilities=replace(
            _capabilities(),
            composite_fk_introspection=Capability.unverified("TEST_UNVERIFIED"),
        ),
    )
    unverified_budget, unverified_ledger = _schema_budget_runtime(
        tmp_path,
        unverified_runtime.namespace,
        ResearchActionKind.INSPECT_COLUMN,
        target,
        suffix="column-composite-unverified",
    )
    try:
        result = inspect_column(
            target,
            runtime=unverified_runtime,
            budget=unverified_budget,
        )
        assert result.status is ProbeStatus.FAILED
        assert result.failure_code == "composite_fk_introspection_unavailable"
    finally:
        unverified_ledger.close()


def test_schema_column_probe_payload_and_digest_are_order_independent(
    tmp_path: Path,
) -> None:
    outgoing = [
        {
            "constraint_id": "center_left",
            "to_table": "main.left_parent",
            "column_pairs": [{"from_column": "id", "to_column": "id"}],
        },
        {
            "constraint_id": "center_right",
            "to_table": "main.right_parent",
            "column_pairs": [{"from_column": "id", "to_column": "id"}],
        },
    ]
    payloads = []
    digests = []
    for suffix, ordered in (
        ("forward", outgoing),
        ("reversed", list(reversed(outgoing))),
    ):
        schema = {
            "main.center": {
                "columns": {"id": {"type": "INTEGER"}},
                "foreign_keys": {"complete": True, "constraints": ordered},
            },
            "main.left_parent": {
                "columns": {"id": {"type": "INTEGER"}},
                "foreign_keys": {"complete": True, "constraints": []},
            },
            "main.right_parent": {
                "columns": {"id": {"type": "INTEGER"}},
                "foreign_keys": {"complete": True, "constraints": []},
            },
        }
        runtime = _mock_schema_runtime(schema)
        target = _column("center", "id")
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_COLUMN,
            target,
            suffix=f"column-order-{suffix}",
        )
        try:
            result = inspect_column(target, runtime=runtime, budget=budget)
            payloads.append(read_probe_payload(result))
            digests.append(result.payload_digest)
        finally:
            ledger.close()

    assert payloads[0] == payloads[1]
    assert digests[0] == digests[1]


def test_schema_column_probe_concurrent_callers_execute_loader_once(
    tmp_path: Path,
) -> None:
    schema = {"main.orders": {"columns": {"id": {"type": "INTEGER"}}}}
    runtime = _mock_schema_runtime(schema)
    loader_calls = 0
    loader_lock = threading.Lock()
    loader_started = threading.Event()
    release_loader = threading.Event()

    class BlockingLoader:
        def load_scoped_schema(self, _schema_info, _dsn, _scope):
            nonlocal loader_calls
            with loader_lock:
                loader_calls += 1
                if loader_calls > 1:
                    release_loader.set()
            loader_started.set()
            assert release_loader.wait(timeout=2)
            return LoadedSchema(schema, runtime.namespace, "mock")

    runtime = replace(runtime, loader=BlockingLoader())
    target = _column("orders", "id")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_COLUMN,
        target,
        suffix="concurrent-column",
    )
    budget = replace(budget, wait=lambda _seconds: release_loader.set())
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(inspect_column, target, runtime=runtime, budget=budget)
            assert loader_started.wait(timeout=2)
            second = pool.submit(inspect_column, target, runtime=runtime, budget=budget)
            results = (first.result(timeout=2), second.result(timeout=2))

        assert loader_calls == 1
        assert results[0] == results[1]
        records = ledger.load_records(RUN_ID, INCARNATION)
        assert len(records) == 1
        assert records[0].result == results[0]
    finally:
        release_loader.set()
        ledger.close()


def test_schema_probe_concurrent_callers_execute_loader_once(tmp_path: Path) -> None:
    schema = {"main.orders": {"columns": {"id": {"type": "INTEGER"}}}}
    runtime = _mock_schema_runtime(schema)
    loader_calls = 0
    loader_lock = threading.Lock()
    loader_started = threading.Event()
    release_loader = threading.Event()

    class BlockingLoader:
        def load_scoped_schema(self, _schema_info, _dsn, _scope):
            nonlocal loader_calls
            with loader_lock:
                loader_calls += 1
                if loader_calls > 1:
                    release_loader.set()
            loader_started.set()
            assert release_loader.wait(timeout=2)
            return LoadedSchema(schema, runtime.namespace, "mock")

    runtime = replace(runtime, loader=BlockingLoader())
    target = _table("orders")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_TABLE,
        target,
        suffix="concurrent-schema",
    )
    budget = replace(budget, wait=lambda _seconds: release_loader.set())
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(inspect_table, target, runtime=runtime, budget=budget)
            assert loader_started.wait(timeout=2)
            second = pool.submit(inspect_table, target, runtime=runtime, budget=budget)
            results = (first.result(timeout=2), second.result(timeout=2))

        assert loader_calls == 1
        assert results[0] == results[1]
        assert ledger.load_records(RUN_ID, INCARNATION)[0].result == results[0]
    finally:
        release_loader.set()
        ledger.close()


def test_schema_relationship_probe_detects_ambiguity_before_top_k_truncation(
    tmp_path: Path,
) -> None:
    schema = {
        "main.root": {
            "columns": {
                "first_target_id": {"type": "INTEGER"},
                "second_target_id": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "root_target_first",
                        "to_table": "main.target",
                        "column_pairs": [
                            {"from_column": "first_target_id", "to_column": "id"}
                        ],
                    },
                    {
                        "constraint_id": "root_target_second",
                        "to_table": "main.target",
                        "column_pairs": [
                            {
                                "from_column": "second_target_id",
                                "to_column": "alternate_id",
                            }
                        ],
                    },
                ],
            },
        },
        "main.target": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "alternate_id": {"type": "INTEGER"},
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("root")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 1), ("top_k", 1)),
        suffix="top-k-ambiguity",
    )
    try:
        result = inspect_relationships(target, 1, 1, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "ambiguous"
        assert payload["paths"][0]["status"] == "ambiguous"
        assert len(payload["paths"][0]["candidate_paths"]) == 1
        assert payload["paths"][0]["preferred_path_signal"] == []
        assert result.truncated is True
    finally:
        ledger.close()


def test_schema_relationship_probe_detects_diamond_ambiguity_per_target(
    tmp_path: Path,
) -> None:
    schema = {
        "main.root": {
            "columns": {
                "left_ref": {"type": "INTEGER"},
                "right_ref": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "root_left",
                        "to_table": "main.left",
                        "column_pairs": [
                            {"from_column": "left_ref", "to_column": "id"}
                        ],
                    },
                    {
                        "constraint_id": "root_right",
                        "to_table": "main.right",
                        "column_pairs": [
                            {"from_column": "right_ref", "to_column": "id"}
                        ],
                    },
                ],
            },
        },
        "main.left": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "target_ref": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "left_target",
                        "to_table": "main.target",
                        "column_pairs": [
                            {"from_column": "target_ref", "to_column": "id"}
                        ],
                    }
                ],
            },
        },
        "main.right": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "target_ref": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "right_target",
                        "to_table": "main.target",
                        "column_pairs": [
                            {"from_column": "target_ref", "to_column": "id"}
                        ],
                    }
                ],
            },
        },
        "main.target": {
            "columns": {"id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}},
            "foreign_keys": {"complete": True, "constraints": []},
        },
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("root")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 2), ("top_k", 1)),
        suffix="diamond-top-k-ambiguity",
    )
    try:
        result = inspect_relationships(target, 1, 2, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)

        assert payload["status"] == "ambiguous"
        assert len(payload["paths"]) == 1
        assert payload["paths"][0]["status"] == "ambiguous"
        assert payload["paths"][0]["table"]["table"] == "target"
        assert len(payload["paths"][0]["candidate_paths"]) == 1
        assert payload["paths"][0]["preferred_path_signal"] == []
        assert result.truncated is True
    finally:
        ledger.close()


def test_schema_relationship_probe_caps_parallel_path_search_at_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constraints = [
        {
            "constraint_id": f"root_target_{index:02d}",
            "to_table": "main.target",
            "column_pairs": [
                {
                    "from_column": f"target_ref_{index:02d}",
                    "to_column": "id",
                }
            ],
        }
        for index in range(60)
    ]
    original_other_table = schema_probe_module._other_table
    traversal_calls = 0

    def count_other_table(edge, node):
        nonlocal traversal_calls
        traversal_calls += 1
        return original_other_table(edge, node)

    monkeypatch.setattr(schema_probe_module, "_other_table", count_other_table)
    candidate_paths = []
    payload_digests = []
    for suffix, ordered_constraints in (
        ("forward", constraints),
        ("reversed", list(reversed(constraints))),
    ):
        schema = {
            "main.root": {
                "columns": {
                    f"target_ref_{index:02d}": {"type": "INTEGER"}
                    for index in range(60)
                },
                "foreign_keys": {
                    "complete": True,
                    "constraints": ordered_constraints,
                },
            },
            "main.target": {
                "columns": {
                    "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}
                },
                "foreign_keys": {"complete": True, "constraints": []},
            },
        }
        runtime = _mock_schema_runtime(schema)
        target = _table("root")
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_RELATIONSHIPS,
            target,
            parameters=(("depth", 1), ("top_k", 50)),
            suffix=f"parallel-path-cap-{suffix}",
            rows=50,
        )
        traversal_calls = 0
        try:
            result = inspect_relationships(
                target,
                50,
                1,
                runtime=runtime,
                budget=budget,
            )
            payload = read_probe_payload(result)

            assert payload["status"] == "ambiguous"
            assert len(payload["relationships"]) == 50
            assert len(payload["paths"]) == 1
            assert len(payload["paths"][0]["candidate_paths"]) == 2
            assert payload["paths"][0]["preferred_path_signal"] == []
            assert result.truncated is True
            assert traversal_calls <= 2 * len(constraints)
            candidate_paths.append(payload["paths"][0]["candidate_paths"])
            payload_digests.append(result.payload_digest)
        finally:
            ledger.close()

    assert candidate_paths[0] == candidate_paths[1]
    assert payload_digests[0] == payload_digests[1]


def test_schema_relationship_probe_reuses_bounded_paths_for_fan_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constraints = [
        {
            "constraint_id": f"root_target_{index:02d}",
            "to_table": f"main.target_{index:02d}",
            "column_pairs": [
                {
                    "from_column": f"target_ref_{index:02d}",
                    "to_column": "id",
                }
            ],
        }
        for index in range(50)
    ]

    def forbid_repeated_path_search(*_args, **_kwargs):
        raise AssertionError("bounded paths must not trigger build_join_path")

    monkeypatch.setattr(
        schema_probe_module,
        "build_join_path",
        forbid_repeated_path_search,
        raising=False,
    )
    payloads = []
    payload_digests = []
    for suffix, ordered_constraints in (
        ("forward", constraints),
        ("reversed", list(reversed(constraints))),
    ):
        schema = {
            "main.root": {
                "columns": {
                    f"target_ref_{index:02d}": {"type": "INTEGER"}
                    for index in range(50)
                },
                "foreign_keys": {
                    "complete": True,
                    "constraints": ordered_constraints,
                },
            },
            **{
                f"main.target_{index:02d}": {
                    "columns": {
                        "id": {
                            "type": "INTEGER",
                            "constraint_type": "PRIMARY KEY",
                        }
                    },
                    "foreign_keys": {"complete": True, "constraints": []},
                }
                for index in range(50)
            },
        }
        runtime = _mock_schema_runtime(schema)
        target = _table("root")
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_RELATIONSHIPS,
            target,
            parameters=(("depth", 1), ("top_k", 50)),
            suffix=f"fan-out-path-reuse-{suffix}",
            rows=50,
        )
        try:
            result = inspect_relationships(
                target,
                50,
                1,
                runtime=runtime,
                budget=budget,
            )
            payload = read_probe_payload(result)

            assert result.status is ProbeStatus.SUCCESS
            assert payload["status"] == "connected"
            assert len(payload["paths"]) == 50
            assert all(
                row["preferred_path_signal"] == row["candidate_paths"][0]
                for row in payload["paths"]
            )
            assert result.truncated is False
            payloads.append(payload)
            payload_digests.append(result.payload_digest)
        finally:
            ledger.close()

    assert payloads[0] == payloads[1]
    assert payload_digests[0] == payload_digests[1]


def test_schema_relationship_probe_preserves_composite_fk_pair_order(
    tmp_path: Path,
) -> None:
    schema = {
        "main.parent": {
            "columns": {
                "first": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "second": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
        "main.child": {
            "columns": {
                "parent_second": {"type": "INTEGER"},
                "parent_first": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "child_parent_fk",
                        "to_table": "main.parent",
                        "column_pairs": [
                            {
                                "from_column": "parent_second",
                                "to_column": "second",
                            },
                            {
                                "from_column": "parent_first",
                                "to_column": "first",
                            },
                        ],
                    }
                ],
            },
        },
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("child")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 1), ("top_k", 5)),
        suffix="composite-order",
    )
    try:
        result = inspect_relationships(target, 5, 1, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert payload["relationships"][0]["column_pairs"] == [
            {"from_column": "parent_second", "to_column": "second"},
            {"from_column": "parent_first", "to_column": "first"},
        ]
    finally:
        ledger.close()


def test_schema_table_probe_sorts_declared_relationships_before_digest(
    tmp_path: Path,
) -> None:
    constraints = [
        {
            "constraint_id": "child_to_left",
            "to_table": "main.left_parent",
            "column_pairs": [{"from_column": "left_ref", "to_column": "id"}],
        },
        {
            "constraint_id": "child_to_right",
            "to_table": "main.right_parent",
            "column_pairs": [{"from_column": "right_ref", "to_column": "id"}],
        },
    ]
    payloads = []
    digests = []
    versions = []
    for suffix, ordered_constraints in (
        ("canonical", constraints),
        ("reversed", list(reversed(constraints))),
    ):
        schema = {
            "main.child": {
                "columns": {
                    "left_ref": {"type": "INTEGER"},
                    "right_ref": {"type": "INTEGER"},
                },
                "foreign_keys": {
                    "complete": True,
                    "constraints": ordered_constraints,
                },
            },
            "main.left_parent": {
                "columns": {
                    "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}
                },
                "foreign_keys": {"complete": True, "constraints": []},
            },
            "main.right_parent": {
                "columns": {
                    "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"}
                },
                "foreign_keys": {"complete": True, "constraints": []},
            },
        }
        runtime = _mock_schema_runtime(schema)
        target = _table("child")
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_TABLE,
            target,
            suffix=f"declared-order-{suffix}",
        )
        try:
            result = inspect_table(target, runtime=runtime, budget=budget)
            payloads.append(read_probe_payload(result)["relationships"])
            digests.append(result.payload_digest)
            versions.append(result.schema_namespace_version)
        finally:
            ledger.close()

    assert payloads[0] == payloads[1]
    assert digests[0] == digests[1]
    assert versions[0] == versions[1]


@pytest.mark.parametrize("_repeat", range(20))
def test_data_probe_f02_search_is_bound_and_run_only_evidence(
    tmp_path: Path, _repeat: int
) -> None:
    runtime = _sqlite_data_runtime(tmp_path, "F02_VERTICAL_EAV")
    target = _column("attribute_kind", "attribute_key")

    def execute(value: str, suffix: str):
        parameters = (("top_k", 5), ("value", value))
        budget, ledger = _data_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.SEARCH_VALUE,
            target,
            parameters=parameters,
            suffix=suffix,
            rows=5,
        )
        try:
            result = _run_supervised(
                lambda: search_value(
                    target,
                    value,
                    5,
                    runtime=runtime,
                    budget=budget,
                )
            )
            assert len(ledger.load_records(RUN_ID, INCARNATION)) == 1
            return result, budget.action
        finally:
            ledger.close()

    matched, action = execute("membership_level", "f02-search")
    injected, _ = execute("membership_level' OR 1=1 --", "f02-injection")

    assert read_probe_payload(matched)["rows"] == [["membership_level"]]
    assert read_probe_payload(injected)["rows"] == []
    evidence = probe_result_to_evidence(matched, action)
    assert evidence is not None
    assert evidence.validity_scope is EvidenceValidityScope.RUN_ONLY
    assert evidence.data_snapshot_token is None


@pytest.mark.parametrize(
    ("value", "expected_row"),
    (("exact", ["exact"]), (None, [None])),
)
def test_search_value_exact_match_is_never_truncated(
    tmp_path: Path,
    value: str | None,
    expected_row: list[str | None],
) -> None:
    database = tmp_path / "search-value.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE probe_values (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.executemany(
            "INSERT INTO probe_values (value) VALUES (?)",
            (("exact",), (None,), ("other",)),
        )
    dsn = f"sqlite://{database}"
    loader = SchemaLoader(tmp_path / "search-value-schema-cache")
    scope = _schema_scope()
    loaded = loader.load_scoped_schema({}, dsn, scope)
    runtime = DataProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=scope,
        namespace=loaded.namespace,
        table_namespace="main",
        deadline=DeadlineBudget.from_duration(30),
    )
    target = _column("probe_values", "value")
    parameters = (("top_k", 1), ("value", value))
    budget, ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.SEARCH_VALUE,
        target,
        parameters=parameters,
        suffix=f"search-value-{value}",
        rows=1,
    )
    try:
        result = _run_supervised(
            lambda: search_value(value=value, target=target, top_k=1, runtime=runtime, budget=budget)
        )

        payload = read_probe_payload(result)
        assert payload["rows"] == [expected_row]
        assert payload["requested_value"] == value
        assert type(payload["requested_value"]) is type(value)
        assert result.truncated is False
    finally:
        ledger.close()


def test_data_probes_treat_dotted_column_as_one_sqlite_identifier(
    tmp_path: Path,
) -> None:
    database = tmp_path / "dotted-column.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE a (id INTEGER PRIMARY KEY, "a.b" TEXT, b TEXT)'
        )
        connection.executemany(
            'INSERT INTO a (id, "a.b", b) VALUES (?, ?, ?)',
            [
                (1, "dot-one", "wrong-one"),
                (2, "dot-two", "wrong-two"),
            ],
        )

    dsn = f"sqlite://{database}"
    loader = SchemaLoader(tmp_path / "dotted-column-schema-cache")
    scope = _schema_scope()
    loaded = loader.load_scoped_schema({}, dsn, scope)
    runtime = DataProbeRuntime(
        loader=loader,
        dsn=dsn,
        scope=scope,
        namespace=loaded.namespace,
        table_namespace="main",
        deadline=DeadlineBudget.from_duration(30),
    )
    target = _column("a", "a.b")
    table = _table("a")
    cases = (
        (
            ResearchActionKind.PROFILE_COLUMN,
            target,
            (),
            "dotted-profile",
            lambda budget: profile_column(target, runtime=runtime, budget=budget),
        ),
        (
            ResearchActionKind.SAMPLE_ROWS,
            table,
            (("column_000", "a.b"), ("limit", 2)),
            "dotted-sample",
            lambda budget: sample_rows(
                table,
                ("a.b",),
                2,
                runtime=runtime,
                budget=budget,
            ),
        ),
        (
            ResearchActionKind.SEARCH_VALUE,
            target,
            (("top_k", 5), ("value", "dot-one")),
            "dotted-search",
            lambda budget: search_value(
                target,
                "dot-one",
                5,
                runtime=runtime,
                budget=budget,
            ),
        ),
        (
            ResearchActionKind.DISTINCT_VALUES,
            target,
            (("top_k", 5),),
            "dotted-distinct",
            lambda budget: get_distinct_values_probe(
                target,
                5,
                runtime=runtime,
                budget=budget,
            ),
        ),
    )
    results: dict[ResearchActionKind, object] = {}
    ledgers = []
    try:
        for kind, action_target, parameters, suffix, execute in cases:
            budget, ledger = _data_budget_runtime(
                tmp_path,
                runtime.namespace,
                kind,
                action_target,
                parameters=parameters,
                suffix=suffix,
                rows={
                    ResearchActionKind.PROFILE_COLUMN: MAX_SAMPLE_ROWS,
                    ResearchActionKind.SAMPLE_ROWS: 2,
                    ResearchActionKind.SEARCH_VALUE: 5,
                    ResearchActionKind.DISTINCT_VALUES: 5,
                }[kind],
            )
            ledgers.append(ledger)
            result = _run_supervised(lambda: execute(budget))
            evidence = probe_result_to_evidence(result, budget.action)
            assert evidence is not None
            assert evidence.target == action_target
            assert evidence.schema_namespace_version == (
                f"sha256:{runtime.namespace.version_key}"
            )
            assert evidence.validity_scope is EvidenceValidityScope.RUN_ONLY
            assert evidence.data_snapshot_token is None
            results[kind] = read_probe_payload(result)
    finally:
        for ledger in ledgers:
            ledger.close()

    assert results[ResearchActionKind.PROFILE_COLUMN]["sampled_profile"][
        "sampled_top_values"
    ] == [
        {"count": 1, "value": "dot-one"},
        {"count": 1, "value": "dot-two"},
    ]
    assert results[ResearchActionKind.SAMPLE_ROWS]["rows"] == [
        ["dot-one"],
        ["dot-two"],
    ]
    assert results[ResearchActionKind.SEARCH_VALUE]["rows"] == [["dot-one"]]
    assert results[ResearchActionKind.DISTINCT_VALUES]["rows"] == [
        ["dot-one"],
        ["dot-two"],
    ]


@pytest.mark.parametrize("_repeat", range(20))
def test_data_probe_f03_profiles_and_samples_opaque_columns(
    tmp_path: Path, _repeat: int
) -> None:
    runtime = _sqlite_data_runtime(tmp_path, "F03_OPAQUE_NAMES")
    sample_target = _table("c31")
    sample_parameters = (
        ("column_000", "k4"),
        ("column_001", "x7"),
        ("limit", 3),
    )
    sample_budget, sample_ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.SAMPLE_ROWS,
        sample_target,
        parameters=sample_parameters,
        suffix="f03-sample",
        rows=3,
    )
    profile_target = _column("c31", "x7")
    profile_budget, profile_ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.PROFILE_COLUMN,
        profile_target,
        suffix="f03-profile",
        rows=MAX_SAMPLE_ROWS,
    )
    try:
        sampled = _run_supervised(
            lambda: sample_rows(
                sample_target,
                ("k4", "x7"),
                3,
                runtime=runtime,
                budget=sample_budget,
            )
        )
        profiled = _run_supervised(
            lambda: profile_column(
                profile_target,
                runtime=runtime,
                budget=profile_budget,
            )
        )

        assert read_probe_payload(sampled)["rows"] == [
            [1, "gold"],
            [2, "silver"],
            [3, "north"],
        ]
        assert read_probe_payload(profiled)["sampled_profile"] == {
            "sample_count": 3,
            "sample_limit": MAX_SAMPLE_ROWS,
            "sampled_cardinality": 3,
            "sampled_max": "silver",
            "sampled_min": "gold",
            "sampled_null_count": 0,
            "sampled_null_ratio": 0.0,
            "sampled_ordering": "single_type",
            "sampled_top_values": [
                {"count": 1, "value": "gold"},
                {"count": 1, "value": "north"},
                {"count": 1, "value": "silver"},
            ],
        }
        assert type(read_probe_payload(sampled)["rows"][0][0]) is int
    finally:
        sample_ledger.close()
        profile_ledger.close()


def test_data_probe_f04_distinct_preserves_integer_cells(tmp_path: Path) -> None:
    runtime = _sqlite_data_runtime(tmp_path, "F04_MISSING_DECLARED_FK")
    target = _column("ledger", "depot_ref")
    parameters = (("top_k", 10),)
    budget, ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.DISTINCT_VALUES,
        target,
        parameters=parameters,
        suffix="f04-distinct",
        rows=10,
    )
    try:
        result = _run_supervised(
            lambda: get_distinct_values_probe(
                target,
                10,
                runtime=runtime,
                budget=budget,
            )
        )

        rows = read_probe_payload(result)["rows"]
        assert rows == [[10], [20]]
        assert all(type(row[0]) is int for row in rows)
    finally:
        ledger.close()


def test_data_probe_f05_keeps_both_ambiguous_value_sources(tmp_path: Path) -> None:
    runtime = _sqlite_data_runtime(tmp_path, "F05_AMBIGUOUS_BINDING")
    evidence = []
    for table in ("member_state_a", "member_state_b"):
        target = _column(table, "state_value")
        parameters = (("top_k", 5), ("value", "gold"))
        budget, ledger = _data_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.SEARCH_VALUE,
            target,
            parameters=parameters,
            suffix=f"f05-{table}",
            rows=5,
        )
        try:
            result = _run_supervised(
                lambda target=target, budget=budget: search_value(
                    target,
                    "gold",
                    5,
                    runtime=runtime,
                    budget=budget,
                )
            )
            record = probe_result_to_evidence(result, budget.action)
            assert record is not None
            evidence.append(record)
        finally:
            ledger.close()

    assert [item.target.table.table for item in evidence] == [
        "member_state_a",
        "member_state_b",
    ]
    assert all(item.source_kind is EvidenceSourceKind.VALUE_SEARCH for item in evidence)


def test_data_sample_without_unique_order_returns_typed_failure(tmp_path: Path) -> None:
    schema = {"main.events": {"columns": {"value": {"type": "TEXT"}}}}
    schema_runtime = _mock_schema_runtime(schema)
    runtime = DataProbeRuntime(
        loader=schema_runtime.loader,
        dsn=schema_runtime.dsn,
        scope=schema_runtime.scope,
        namespace=schema_runtime.namespace,
        table_namespace=schema_runtime.table_namespace,
        deadline=schema_runtime.deadline,
        get_plugin=schema_runtime.get_plugin,
    )
    target = _table("events")
    parameters = (("column_000", "value"), ("limit", 2))
    budget, ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.SAMPLE_ROWS,
        target,
        parameters=parameters,
        suffix="no-order",
        rows=2,
    )
    try:
        result = sample_rows(
            target,
            ("value",),
            2,
            runtime=runtime,
            budget=budget,
        )

        assert result.status is ProbeStatus.FAILED
        assert result.failure_code == "deterministic_order_unavailable"
        assert result.invocation_id == budget.invocation_id
        assert probe_result_to_evidence(result, budget.action) is None
    finally:
        ledger.close()


def test_profile_column_uses_one_bounded_ordered_sample_and_exactly_once_budget(
    tmp_path: Path,
) -> None:
    schema = {
        "main.events": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "value": {"type": "TEXT"},
            }
        }
    }
    plugin = _DataPlugin(
        rows=(
            [(None,)] * 5
            + [(True,)] * 5
            + [(7,)] * 5
            + [("x",)] * 5
            + [(index,) for index in range(1000)]
        ),
        columns=["value"],
    )
    ticks = iter((100, 101))
    runtime = _mock_data_runtime(schema, plugin, monotonic_ns=lambda: next(ticks))
    target = _column("events", "value")
    budget, ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.PROFILE_COLUMN,
        target,
        suffix="bounded-profile",
        rows=MAX_SAMPLE_ROWS,
    )
    try:
        first = profile_column(target, runtime=runtime, budget=budget)
        second = profile_column(target, runtime=runtime, budget=budget)
        payload = read_probe_payload(first)
        execute_calls = [
            call
            for call in plugin.calls
            if isinstance(call, tuple) and call[0] == "execute"
        ]

        assert first == second
        first_evidence = probe_result_to_evidence(first, budget.action)
        second_evidence = probe_result_to_evidence(second, budget.action)
        assert first_evidence is not None
        assert second_evidence is not None
        assert (
            json.loads(first_evidence.observation)["provenance"]
            == json.loads(second_evidence.observation)["provenance"]
        )
        assert len(execute_calls) == 1
        sql = execute_calls[0][1]
        assert sql == (
            f'SELECT "value" FROM "main"."events" ORDER BY "id" LIMIT {MAX_SAMPLE_ROWS}'
        )
        assert "COUNT" not in sql
        assert "*" not in sql
        assert payload["sampled_profile"]["sample_count"] == MAX_SAMPLE_ROWS
        assert payload["sampled_profile"]["sampled_ordering"] == "mixed_types"
        values = [
            item["value"] for item in payload["sampled_profile"]["sampled_top_values"]
        ]
        assert any(value is None for value in values)
        assert any(type(value) is bool for value in values)
        assert any(type(value) is int for value in values)
        assert any(type(value) is str for value in values)
        assert first.cost.db_probe_ms == 1
        assert len(ledger.load_records(RUN_ID, INCARNATION)) == 1
    finally:
        ledger.close()


def test_data_probe_cost_rounds_one_nanosecond_up_to_one_millisecond() -> None:
    cost = _data_probe_cost(100, lambda: 101)

    assert cost.wall_clock_ms == 1
    assert cost.db_probe_ms == 1


def test_execute_research_probe_uses_only_trusted_template_and_typed_cells(
    tmp_path: Path,
) -> None:
    schema = {
        "main.records": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "flag": {"type": "BOOLEAN"},
                "note": {"type": "TEXT"},
            }
        }
    }
    plugin = _DataPlugin(
        rows=[(True, None, 7, "x")],
        columns=["flag", "missing", "number", "text"],
    )
    runtime = _mock_data_runtime(schema, plugin)
    template = ResearchQueryTemplate(
        probe_id="typed-probe",
        namespace="main",
        schema_namespace_version=f"sha256:{runtime.namespace.version_key}",
        sql=(
            "SELECT \"flag\", NULL AS missing, 7 AS number, 'x' AS text "
            'FROM "main"."records" WHERE "id" = ? ORDER BY "id" LIMIT 1'
        ),
        output_columns=("flag", "missing", "number", "text"),
        row_limit=1,
        deterministic=True,
    )
    runtime = replace(runtime, query_templates=(template,))
    target = QueryProbeRef(probe_id="typed-probe", namespace="main")
    parameters = (("parameter_000", 7),)
    budget, ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.EXECUTE_PROBE,
        target,
        parameters=parameters,
        suffix="typed-template",
        rows=1,
    )
    try:
        result = execute_research_probe(
            target,
            (7,),
            runtime=runtime,
            budget=budget,
        )

        row = read_probe_payload(result)["rows"][0]
        assert row == [True, None, 7, "x"]
        assert [type(value) for value in row] == [bool, type(None), int, str]
        bound_calls = [
            call
            for call in plugin.calls
            if isinstance(call, tuple) and call[0] == "execute_bound"
        ]
        assert bound_calls[0][2] == (7,)
        assert "?" in bound_calls[0][1]
    finally:
        ledger.close()


def test_data_probe_parameter_capability_fails_before_connect(tmp_path: Path) -> None:
    capabilities = _capabilities().downgrade(
        parameter_binding=Capability.unverified(
            "TEST_BINDING_UNVERIFIED",
            EnforcementMode.DRIVER,
        )
    )
    schema = {
        "main.records": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "value": {"type": "TEXT"},
            }
        }
    }
    plugin = _DataPlugin(rows=[], columns=["value"], capabilities=capabilities)
    runtime = _mock_data_runtime(schema, plugin)
    target = _column("records", "value")
    parameters = (("top_k", 5), ("value", "needle"))
    budget, ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.SEARCH_VALUE,
        target,
        parameters=parameters,
        suffix="binding-unverified",
        rows=5,
    )
    try:
        result = search_value(
            target,
            "needle",
            5,
            runtime=runtime,
            budget=budget,
        )

        assert result.status is ProbeStatus.FAILED
        assert result.failure_code == "data_probe_capability_unavailable"
        assert not any(
            isinstance(call, tuple) and call[0] == "connect" for call in plugin.calls
        )
    finally:
        ledger.close()


def test_data_probe_enforces_columns_top_k_bytes_and_deadline_bounds(
    tmp_path: Path,
) -> None:
    schema = {
        "main.records": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PK"},
                "value": {"type": "TEXT"},
            }
        }
    }
    plugin = _DataPlugin(rows=[("value",)], columns=["value"])
    runtime = _mock_data_runtime(schema, plugin)

    columns = tuple(f"column_{index}" for index in range(21))
    sample_target = _table("records")
    sample_parameters = tuple(
        (f"column_{index:03d}", column) for index, column in enumerate(columns)
    ) + (("limit", 1),)
    sample_budget, sample_ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.SAMPLE_ROWS,
        sample_target,
        parameters=sample_parameters,
        suffix="columns-bound",
        rows=1,
    )
    distinct_target = _column("records", "value")
    top_k_budget, top_k_ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.DISTINCT_VALUES,
        distinct_target,
        parameters=(("top_k", 51),),
        suffix="top-k-bound",
        rows=51,
    )
    bytes_budget, bytes_ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.DISTINCT_VALUES,
        distinct_target,
        parameters=(("top_k", 5),),
        suffix="bytes-bound",
        rows=5,
        bytes_=1,
    )
    expired = DeadlineBudget(
        deadline_monotonic=0.0,
        deadline_at_ms=0,
        monotonic=lambda: 1.0,
        wall_time=lambda: 1.0,
    )
    deadline_runtime = replace(runtime, deadline=expired)
    deadline_budget, deadline_ledger = _data_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.DISTINCT_VALUES,
        distinct_target,
        parameters=(("top_k", 5),),
        suffix="deadline-bound",
        rows=5,
    )
    try:
        columns_result = sample_rows(
            sample_target,
            columns,
            1,
            runtime=runtime,
            budget=sample_budget,
        )
        top_k_result = get_distinct_values_probe(
            distinct_target,
            51,
            runtime=runtime,
            budget=top_k_budget,
        )
        bytes_result = get_distinct_values_probe(
            distinct_target,
            5,
            runtime=runtime,
            budget=bytes_budget,
        )
        deadline_result = get_distinct_values_probe(
            distinct_target,
            5,
            runtime=deadline_runtime,
            budget=deadline_budget,
        )

        assert columns_result.failure_code == "columns_out_of_bounds"
        assert top_k_result.failure_code == "top_k_out_of_bounds"
        assert bytes_result.failure_code == "data_result_limit_exceeded"
        assert deadline_result.status is ProbeStatus.TIMED_OUT
        assert deadline_result.failure_code == "data_deadline_exceeded"
    finally:
        sample_ledger.close()
        top_k_ledger.close()
        bytes_ledger.close()
        deadline_ledger.close()


@pytest.mark.parametrize(
    "composite_capability",
    (
        Capability.unsupported("TEST_UNSUPPORTED"),
        Capability.unverified("TEST_UNVERIFIED"),
    ),
)
def test_schema_table_probe_rejects_unproven_composite_fk_order(
    tmp_path: Path,
    composite_capability: Capability,
) -> None:
    schema = {
        "main.parent": {
            "columns": {
                "first": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "second": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
            },
            "foreign_keys": {"complete": True, "constraints": []},
        },
        "main.child": {
            "columns": {
                "parent_first": {"type": "INTEGER"},
                "parent_second": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "child_parent_fk",
                        "to_table": "main.parent",
                        "column_pairs": [
                            {"from_column": "parent_first", "to_column": "first"},
                            {"from_column": "parent_second", "to_column": "second"},
                        ],
                    }
                ],
            },
        },
    }
    runtime = _mock_schema_runtime(
        schema,
        capabilities=replace(
            _capabilities(),
            composite_fk_introspection=composite_capability,
        ),
    )
    target = _table("child")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_TABLE,
        target,
        suffix=f"table-composite-{composite_capability.state.value}",
    )
    try:
        result = inspect_table(target, runtime=runtime, budget=budget)
        assert result.status is ProbeStatus.FAILED
        assert result.failure_code == "composite_fk_introspection_unavailable"
        assert read_probe_payload(result) is None
    finally:
        ledger.close()


def test_schema_relationship_probe_exposes_cycles_and_disconnected_components(
    tmp_path: Path,
) -> None:
    schema = {
        "main.a": {
            "columns": {"b_id": {"type": "INTEGER"}},
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "a_to_b",
                        "to_table": "main.b",
                        "column_pairs": [{"from_column": "b_id", "to_column": "id"}],
                    }
                ],
            },
        },
        "main.b": {
            "columns": {
                "id": {"type": "INTEGER", "constraint_type": "PRIMARY KEY"},
                "a_id": {"type": "INTEGER"},
            },
            "foreign_keys": {
                "complete": True,
                "constraints": [
                    {
                        "constraint_id": "b_to_a",
                        "to_table": "main.a",
                        "column_pairs": [{"from_column": "a_id", "to_column": "b_id"}],
                    }
                ],
            },
        },
        "main.c": {
            "columns": {"id": {"type": "INTEGER"}},
            "foreign_keys": {"complete": True, "constraints": []},
        },
    }
    runtime = _mock_schema_runtime(schema)
    target = _table("a")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 3), ("top_k", 10)),
        suffix="cycle",
    )
    try:
        result = inspect_relationships(target, 10, 3, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert payload["status"] == "ambiguous"
        assert payload["paths"][0]["status"] == "ambiguous"
        assert payload["disconnected_tables"][0]["table"] == "c"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("containment", "status", "failure_code"),
    [
        (lambda *_args, **_kwargs: None, ProbeStatus.FAILED, "containment_unavailable"),
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
            ProbeStatus.TIMED_OUT,
            "schema_probe_timed_out",
        ),
    ],
)
def test_schema_containment_failure_is_typed_not_negative_evidence(
    tmp_path: Path,
    containment,
    status: ProbeStatus,
    failure_code: str,
) -> None:
    runtime, _ = _sqlite_schema_runtime(tmp_path, "F01_CONVENTIONAL_STAR")
    runtime = replace(
        runtime,
        containment_probe=containment,
        containment_sample_size=10,
    )
    target = _table("sales_fact")
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.INSPECT_RELATIONSHIPS,
        target,
        parameters=(("depth", 1), ("top_k", 5)),
        suffix=f"containment-{status.value}",
    )
    try:
        result = inspect_relationships(target, 5, 1, runtime=runtime, budget=budget)
        assert result.status is status
        assert result.failure_code == failure_code
        assert result.invocation_id == budget.invocation_id
        assert read_probe_payload(result) is None
    finally:
        ledger.close()


def test_schema_probe_rejects_scope_target_stale_and_capability_mismatches(
    tmp_path: Path,
) -> None:
    schema = {"main.orders": {"columns": {"id": {"type": "INTEGER"}}}}
    base = _mock_schema_runtime(schema)
    stale_namespace = SchemaNamespace(
        scope=base.scope,
        schema_fingerprint="f" * 64,
    )
    cases = (
        (
            replace(base, scope=_schema_scope(access_scope_id="other")),
            _table("orders"),
            base.namespace,
            "schema_scope_mismatch",
        ),
        (
            base,
            TableRef(namespace="other", schema=None, table="orders"),
            base.namespace,
            "target_namespace_mismatch",
        ),
        (
            replace(base, namespace=stale_namespace),
            _table("orders"),
            stale_namespace,
            "schema_stale",
        ),
        (
            _mock_schema_runtime(
                schema,
                capabilities=_capabilities(introspection=False),
            ),
            _table("orders"),
            None,
            "schema_introspection_unavailable",
        ),
    )
    for index, (runtime, target, namespace, failure_code) in enumerate(cases):
        selected_namespace = namespace or runtime.namespace
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            selected_namespace,
            ResearchActionKind.INSPECT_TABLE,
            target,
            suffix=f"mismatch-{index}",
        )
        try:
            result = inspect_table(target, runtime=runtime, budget=budget)
            assert result.status is ProbeStatus.FAILED
            assert result.failure_code == failure_code
        finally:
            ledger.close()


def test_schema_probe_enforces_top_k_depth_and_result_byte_bounds(
    tmp_path: Path,
) -> None:
    runtime = _mock_schema_runtime(
        {"main.orders": {"columns": {"id": {"type": "INTEGER"}}}}
    )
    target = _table("orders")
    cases = (
        (51, 1, 100, 200_000, "top_k_out_of_bounds"),
        (5, 5, 5, 200_000, "depth_out_of_bounds"),
        (5, 1, 5, 1, "schema_result_limit_exceeded"),
    )
    for index, (top_k, depth, rows, bytes_, failure_code) in enumerate(cases):
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_RELATIONSHIPS,
            target,
            parameters=(("depth", depth), ("top_k", top_k)),
            suffix=f"bound-{index}",
            rows=rows,
            bytes_=bytes_,
        )
        try:
            result = inspect_relationships(
                target,
                top_k,
                depth,
                runtime=runtime,
                budget=budget,
            )
            assert result.status is ProbeStatus.FAILED
            assert result.failure_code == failure_code
        finally:
            ledger.close()


def test_schema_evidence_is_runtime_owned_and_version_bound(tmp_path: Path) -> None:
    runtime = _mock_schema_runtime(
        {"main.orders": {"columns": {"id": {"type": "INTEGER"}}}}
    )
    target = DocumentRef(document_id="orders-rule", namespace="main")
    document = SchemaEvidenceDocument(
        document_id=target.document_id,
        namespace="main",
        schema_namespace_version=f"sha256:{runtime.namespace.version_key}",
        source_version="sha256:" + "d" * 64,
        valid_until=NOW + timedelta(days=1),
        title="Orders rule",
        content="Orders are structurally keyed by id.",
        target=_table("orders"),
    )
    runtime = replace(runtime, documents=(document,))
    budget, ledger = _schema_budget_runtime(
        tmp_path,
        runtime.namespace,
        ResearchActionKind.READ_DOCUMENT,
        target,
        suffix="schema-document",
    )
    try:
        result = read_schema_evidence(target, runtime=runtime, budget=budget)
        payload = read_probe_payload(result)
        assert payload["document"]["content"] == document.content
        assert (
            payload["document"]["schema_namespace_version"]
            == result.schema_namespace_version
        )
        evidence = probe_result_to_evidence(result, budget.action)
        assert evidence is not None
        assert evidence.validity_scope is EvidenceValidityScope.SCHEMA_VERSION
        provenance = ProbeProvenance.model_validate_json(
            canonical_json_bytes(json.loads(evidence.observation)["provenance"])
        )
        assert provenance.source_version == document.source_version
        assert provenance.valid_until == document.valid_until
    finally:
        ledger.close()


def test_document_probe_payload_without_source_version_is_rejected() -> None:
    target = DocumentRef(document_id="orders-rule", namespace="main")
    action = _schema_action(ResearchActionKind.READ_DOCUMENT, target)

    with pytest.raises(ProbeResultError, match="source metadata"):
        _result(
            {"document": {"document_id": target.document_id}},
            action=action,
        )


def test_inline_null_payload_round_trips_and_forms_fresh_evidence() -> None:
    action = _action()
    result = _result(None, action=action)

    assert deserialize_probe_result(serialize_probe_result(result)) == result
    evidence = probe_result_to_evidence(result, action)
    assert evidence is not None
    observation = json.loads(evidence.observation)
    assert observation["storage"] == "inline"
    assert observation["payload"] is None
    assert observation["artifact_reference"] is None
    decision = evaluate_evidence_freshness(
        evidence,
        FreshnessContext(
            evaluated_at=result.completed_at,
            run_id=result.run_id,
            run_incarnation=result.run_incarnation,
            schema_namespace_version=result.schema_namespace_version,
        ),
    )
    assert decision.status is FreshnessStatus.FRESH


def test_schema_probe_payload_digest_is_deterministic_across_ddl_order(
    tmp_path: Path,
) -> None:
    digests = []
    versions = []
    for ddl_order in ("canonical", "reversed"):
        runtime, _ = _sqlite_schema_runtime(
            tmp_path,
            "F01_CONVENTIONAL_STAR",
            ddl_order=ddl_order,
        )
        target = _table("sales_fact")
        budget, ledger = _schema_budget_runtime(
            tmp_path,
            runtime.namespace,
            ResearchActionKind.INSPECT_TABLE,
            target,
            suffix=f"digest-{ddl_order}",
        )
        try:
            result = inspect_table(target, runtime=runtime, budget=budget)
            digests.append(result.payload_digest)
            versions.append(result.schema_namespace_version)
        finally:
            ledger.close()

    assert digests[0] == digests[1]
    assert versions[0] == versions[1]
