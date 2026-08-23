"""W6-07 deterministic integration scenarios for the typed SQL solver."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    canonical_binding,
)
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    BudgetState,
    CheckKind,
    CheckStatus,
    DiscriminatorValueBinding,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    PredicateOperator,
    PredicateRef,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchReentryStatus,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SolverStopReason,
    TableRef,
    ColumnRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.provenance import ProbeProvenance
from custom_tools.text_to_sql.adaptive.replay_inputs import (
    ResearchTerminalReplayInput,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageInputError,
    CoverageInputErrorCode,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)
from custom_tools.text_to_sql.adaptive.solver_loop import (
    admit_targeted_reentry,
    finalize_targeted_reentry,
)
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    MissingEvidenceProposal,
    SolverProposalV1,
    SqlCandidateProposal,
)
from custom_tools.text_to_sql.schema_loader import LoadedSchema
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from custom_tools.text_to_sql.validators import load_startup_safety_policy
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedAdmission,
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
)
from workflow.adaptive_research_state_store import AdaptiveResearchStateStore
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.deadline import DeadlineBudget
from workflow.text_to_sql_adaptive_solver import (
    apply_finalizer_checkpoint,
    prepare_finalizer_execution,
    reconcile_known_finalizer,
    run_adaptive_sql_generation,
)


_RUN_ID = "w6-07-run"
_INCARNATION = "w6-07-incarnation"
_GOOD_SQL = "SELECT o.status FROM orders AS o WHERE o.status = 'active'"
_GOOD_SQL_ORDERED = _GOOD_SQL + " ORDER BY o.id"
_MISSING_FILTER_SQL = "SELECT o.status FROM orders AS o"
_UNSAFE_SQL = _GOOD_SQL_ORDERED
_EXPLAIN_FAIL_SQL = _GOOD_SQL.replace("o.status FROM", "o.status, o.status FROM")
_MODEL_LIMIT = 8
_OBSERVED_AT = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


class _SqliteCallLedger:
    """Process-safe call counts kept outside Python object memory."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS calls ("
                "name TEXT PRIMARY KEY, count INTEGER NOT NULL)"
            )

    def add(self, name: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO calls(name, count) VALUES (?, 1) "
                "ON CONFLICT(name) DO UPDATE SET count = count + 1",
                (name,),
            )

    def counts(self) -> Counter[str]:
        with sqlite3.connect(self.path) as connection:
            return Counter(dict(connection.execute("SELECT name, count FROM calls")))


class _ScriptedProposalBoundary:
    def __init__(self, proposals, ledger: _SqliteCallLedger) -> None:
        self._proposals = list(proposals)
        self._ledger = ledger

    async def __call__(self, _state, _requirements) -> SolverProposalV1:
        self._ledger.add("proposal")
        if not self._proposals:
            raise AssertionError("unexpected solver proposal call")
        return self._proposals.pop(0)


def _sql(sql: str) -> SolverProposalV1:
    return SolverProposalV1(
        proposal_version=1,
        proposal=SqlCandidateProposal(proposal_kind="sql_candidate", sql=sql),
    )


def _missing() -> SolverProposalV1:
    return SolverProposalV1(
        proposal_version=1,
        proposal=MissingEvidenceProposal(
            proposal_kind="missing_evidence",
            source_id="status",
            question="Refresh the exact status evidence",
            required_evidence_kind=EvidenceSourceKind.SCHEMA,
            reason="One fresh status-column observation is required",
        ),
    )


def _table() -> TableRef:
    return TableRef(namespace="main", schema=None, table="orders")


def _column() -> ColumnRef:
    return ColumnRef(table=_table(), column="status")


def _budget() -> BudgetState:
    values: dict[str, int] = {}
    for name, initial in (
        ("wall_clock_ms", 60_000),
        ("model_calls", _MODEL_LIMIT),
        ("model_tokens", 100_000),
        ("db_probe_ms", 60_000),
        ("rows", 10_000),
        ("bytes", 1_000_000),
    ):
        values[f"initial_{name}"] = initial
        values[f"used_{name}"] = 0
        values[f"remaining_{name}"] = initial
    return BudgetState(**values)


def _action(
    index: int, *, target: ColumnRef | TableRef | None = None
) -> ResearchAction:
    selected_target = target or _table()
    kind = (
        ResearchActionKind.INSPECT_COLUMN
        if type(selected_target) is ColumnRef
        else ResearchActionKind.INSPECT_TABLE
    )
    parameters = (("revision", index),)
    return ResearchAction(
        action_id=f"research-action-{index}",
        kind=kind,
        hypothesis_id=None,
        target=selected_target,
        parameters=parameters,
        action_digest=canonical_action_digest(
            kind=kind,
            hypothesis_id=None,
            target=selected_target,
            parameters=parameters,
            expected_revision=index,
        ),
        expected_revision=index,
    )


def _evidence(
    evidence_id: str,
    *,
    source_kind: EvidenceSourceKind,
    action: ResearchAction,
    schema_version: str,
    revision: int,
    observed_at: datetime,
) -> EvidenceRecord:
    target = _column()
    if source_kind is EvidenceSourceKind.SCHEMA:
        payload: object = {
            "status": "matched",
            "column": target.model_dump(mode="json", by_alias=True),
        }
        probe_kind = ResearchActionKind.INSPECT_COLUMN
        validity = EvidenceValidityScope.SCHEMA_VERSION
        row_count = 0
    else:
        payload = {"columns": ["status"], "rows": [["active"]]}
        probe_kind = ResearchActionKind.SEARCH_VALUE
        validity = EvidenceValidityScope.RUN_ONLY
        row_count = 1
    payload_bytes = canonical_json_bytes(payload)
    provenance = ProbeProvenance(
        provenance_version=1,
        run_id=_RUN_ID,
        run_incarnation=_INCARNATION,
        invocation_id=evidence_id,
        action_digest=action.action_digest,
        probe_kind=probe_kind,
        target=target,
        schema_namespace_version=schema_version,
        payload_digest=canonical_digest(payload),
        started_at=observed_at,
        completed_at=observed_at,
    )
    observation = canonical_json_bytes(
        {
            "artifact_reference": None,
            "byte_count": len(payload_bytes),
            "invocation_id": evidence_id,
            "observation_version": 1,
            "payload": payload,
            "payload_digest": provenance.payload_digest,
            "probe_kind": probe_kind,
            "provenance": provenance,
            "row_count": row_count,
            "storage": "inline",
            "summary": "W6-07 deterministic evidence",
            "truncated": False,
        }
    ).decode("utf-8")
    return EvidenceRecord(
        run_id=_RUN_ID,
        run_incarnation=_INCARNATION,
        revision=revision,
        schema_namespace_version=schema_version,
        evidence_id=evidence_id,
        source_kind=source_kind,
        target=target,
        action_digest=action.action_digest,
        observation=observation,
        validity_scope=validity,
        data_snapshot_token=None,
        observed_at=observed_at,
        strength=1.0,
        created_at=observed_at,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=row_count,
            bytes=len(payload_bytes),
        ),
    )


def _research_state(
    schema_version: str, *, revision: int = 1
) -> ResearchState:
    observed_at = _OBSERVED_AT
    first_action = _action(0)
    schema_evidence = _evidence(
        "evidence-status-schema",
        source_kind=EvidenceSourceKind.SCHEMA,
        action=first_action,
        schema_version=schema_version,
        revision=revision,
        observed_at=observed_at,
    )
    value_evidence = _evidence(
        "evidence-status-value",
        source_kind=EvidenceSourceKind.VALUE_SEARCH,
        action=first_action,
        schema_version=schema_version,
        revision=revision,
        observed_at=observed_at,
    )
    predicate = PredicateRef(
        left=_column(),
        operator=PredicateOperator.EQ,
        right="active",
    )
    binding = canonical_binding(
        DiscriminatorValueBinding(
            binding_id="binding-status",
            source_id="status",
            tables=(_table(),),
            columns=(_column(),),
            predicates=(predicate,),
            join_path=(),
            evidence_ids=(schema_evidence.evidence_id, value_evidence.evidence_id),
            confidence=1.0,
            status=BindingStatus.SUPPORTED,
            validator_rule="w6-07",
            discriminator_column=_column(),
            discriminator_predicate=predicate,
        )
    )
    return ResearchState(
        run_id=_RUN_ID,
        run_incarnation=_INCARNATION,
        revision=revision,
        schema_namespace_version=schema_version,
        query_spec=QuerySpec(
            run_id=_RUN_ID,
            run_incarnation=_INCARNATION,
            revision=0,
            schema_namespace_version=schema_version,
            query_id="w6-07-query",
            original_text="status",
            semantic_items=(
                SemanticItem(
                    source_id="status",
                    kind=SemanticItemKind.FILTER,
                    source_text="status",
                    normalized_meaning="active status",
                    required=True,
                    operator=PredicateOperator.EQ,
                    literal_or_reference="active",
                    status=SemanticItemStatus.RESOLVED,
                    binding_ids=(binding.binding_id,),
                ),
            ),
            requested_output_source_ids=(),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        hypotheses=(),
        evidence=(schema_evidence, value_evidence),
        bindings=(binding,),
        join_candidates=(),
        unresolved_items=(),
        action_history=(first_action,) if revision else (),
        result_expectations=(),
        budget_state=_budget(),
        stop_reason=None,
    )


def test_exact_fresh_search_value_suffices_without_inspect_column() -> None:
    state = _research_state("sha256:" + "a" * 64)
    (binding,) = state.bindings
    (value_evidence,) = (
        evidence
        for evidence in state.evidence
        if evidence.source_kind is EvidenceSourceKind.VALUE_SEARCH
    )
    binding = canonical_binding(
        binding.model_copy(update={"evidence_ids": (value_evidence.evidence_id,)})
    )
    state = state.model_copy(
        update={"bindings": (binding,), "evidence": (value_evidence,)}
    )

    requirements = validate_coverage_inputs(
        state,
        FreshnessContext(
            evaluated_at=_OBSERVED_AT,
            run_id=_RUN_ID,
            run_incarnation=_INCARNATION,
            schema_namespace_version=state.schema_namespace_version,
        ),
        _RUN_ID,
        _INCARNATION,
    )

    assert requirements.selected_bindings == (binding,)


def test_exact_time_physical_predicate_rejects_substitute_at_coverage() -> None:
    state = _research_state("sha256:" + "a" * 64)
    (item,) = state.query_spec.semantic_items
    (binding,) = state.bindings
    substitute = PredicateRef(
        left=binding.discriminator_column,
        operator=PredicateOperator.BETWEEN,
        right=("2024-06-01", "2024-06-30"),
    )
    binding = canonical_binding(
        binding.model_copy(
            update={
                "predicates": (substitute,),
                "discriminator_predicate": substitute,
            }
        )
    )
    item = item.model_copy(
        update={
            "kind": SemanticItemKind.TIME,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": "202406",
            "exact_physical_predicate": True,
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"semantic_items": (item,)}
            ),
            "bindings": (binding,),
        }
    )

    with pytest.raises(CoverageInputError) as raised:
        validate_coverage_inputs(
            state,
            FreshnessContext(
                evaluated_at=_OBSERVED_AT,
                run_id=_RUN_ID,
                run_incarnation=_INCARNATION,
                schema_namespace_version=state.schema_namespace_version,
            ),
            _RUN_ID,
            _INCARNATION,
        )

    assert raised.value.code is CoverageInputErrorCode.QUERY_REQUIREMENT_INCOMPLETE


def _create_fixture(
    root: Path, *, research_revision: int = 1
) -> tuple[str, LoadedSchema, ResearchState]:
    database = root / "orders.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "id INTEGER PRIMARY KEY, status TEXT NOT NULL)"
        )
        if connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            connection.executemany(
                "INSERT INTO orders(id, status) VALUES (?, ?)",
                ((1, "active"), (2, "inactive")),
            )
    schema = {
        "orders": {
            "columns": {
                "id": {"type": "INTEGER"},
                "status": {"type": "TEXT"},
            }
        }
    }
    scope = SchemaScope(1, "tenant", "scope", "connection", True)
    namespace = SchemaNamespace(scope, canonical_schema_fingerprint(schema))
    loaded_schema = LoadedSchema(schema, namespace, "w6-07")
    research = _research_state(
        f"sha256:{namespace.version_key}", revision=research_revision
    )
    return f"sqlite:///{database}", loaded_schema, research


def _runtime(
    root: Path, *, research_revision: int = 1
) -> tuple[TextToSqlTypedRuntime, AdaptiveSolverCheckpointStore]:
    dsn, loaded_schema, research = _create_fixture(
        root, research_revision=research_revision
    )
    database = root / "solver.sqlite"
    store = AdaptiveSolverCheckpointStore(database)
    checkpoint_store = AdaptiveStateStore(database)
    research_store = AdaptiveResearchStateStore(database)
    for revision in range(research.revision):
        key = AdaptiveCheckpointKey(
            research.run_id,
            research.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            revision,
        )
        checkpoint_store.record_planned(
            key,
            expected_revision=None if revision == 0 else revision - 1,
            action={"kind": "historical_planned", "revision": revision},
        )
        checkpoint_store.record_observed(
            key,
            expected_revision=revision,
            action={"kind": "historical_observed", "revision": revision},
        )
    checkpoint_store.record_replayable_terminal(
        AdaptiveCheckpointKey(
            research.run_id,
            research.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            research.revision,
        ),
        expected_revision=(
            None if research.revision == 0 else research.revision - 1
        ),
        action={
            "affected_source_ids": [],
            "citation_evidence_ids": sorted(
                item.evidence_id for item in research.evidence
            ),
            "contract_version": 2,
            "kind": "research_terminal",
            "rejection_signatures": [],
            "reason": "COMPLETE",
            "ambiguity": None,
        },
        replay_input=ResearchTerminalReplayInput(
            freshness_context=FreshnessContext(
                evaluated_at=_OBSERVED_AT,
                run_id=research.run_id,
                run_incarnation=research.run_incarnation,
                schema_namespace_version=research.schema_namespace_version,
            )
        ),
    )
    deadline = DeadlineBudget.from_duration(30)
    scope = loaded_schema.namespace.scope.to_mapping()
    admission = TextToSqlTypedAdmission(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn=dsn,
        schema_scope=scope,
        _capability=_ADMISSION_CAPABILITY,
    )
    runtime = TextToSqlTypedRuntime(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        deadline=deadline,
        query=research.query_spec.original_text,
        dsn=dsn,
        schema_scope=scope,
        research_state_store=research_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=None,
        solver_checkpoint_store=store,
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )
    runtime.loaded_schema = loaded_schema
    runtime.verified_research_state = research
    return runtime, store


def _persist_research_baseline(
    store: AdaptiveSolverCheckpointStore, research_state: ResearchState
) -> None:
    research_store = AdaptiveResearchStateStore(store.db_path)
    try:
        research_store.save_query_spec(research_state.query_spec)
        research_store.save_research_state(
            research_state, expected_previous_revision=None
        )
    finally:
        research_store.close()


def _ids():
    for index in range(1, 100):
        yield f"w6-07-id-{index}"


def _install_tool_boundaries(
    monkeypatch,
    ledger: _SqliteCallLedger,
    *,
    unsafe_sql: str | None = None,
    explain_failure_sql: str | None = None,
) -> None:
    import custom_tools.text_to_sql.adaptive.solver_runner as runner

    def safety(sql, **_kwargs):
        ledger.add("safety")
        if sql == unsafe_sql:
            return {
                "is_safe": False,
                "issues": [
                    {"issue_type": "FORBIDDEN_COMMAND", "description": "blocked"}
                ],
                "advisory_issues": [],
                "safety_status": "unsafe",
                "llm_audit": "skipped_static_unsafe",
            }
        return {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
        }

    def explain(sql, **_kwargs):
        ledger.add("explain")
        if sql == explain_failure_sql:
            return {
                "plan": None,
                "estimated_cost": None,
                "rows_to_scan": None,
                "issues": [{"issue_type": "EXPLAIN_ERROR", "description": "forced"}],
                "profile_name": "w6-07",
                "policy_version": "1",
            }
        return {
            "plan": "SCAN orders",
            "estimated_cost": 1.0,
            "rows_to_scan": 2,
            "issues": [],
            "profile_name": "w6-07",
            "policy_version": "1",
        }

    monkeypatch.setattr(runner.core, "sql_safety_check", safety)
    monkeypatch.setattr(runner.core, "sql_explain", explain)


def _run_generation(
    runtime,
    proposals,
    ledger,
    *,
    reenter=None,
) -> dict[str, object]:
    identifiers = _ids()
    return asyncio.run(
        run_adaptive_sql_generation(
            runtime,
            propose=_ScriptedProposalBoundary(proposals, ledger),
            safety_policy=load_startup_safety_policy(),
            row_limit=10,
            dry_run_only=False,
            table_namespace="main",
            reenter=reenter,
            id_factory=identifiers.__next__,
        )
    )


def _checks_by_candidate(state) -> dict[str, tuple[object, ...]]:
    return {
        candidate.candidate_id: tuple(
            check
            for check in state.check_results
            if check.candidate_id == candidate.candidate_id
        )
        for candidate in state.sql_candidates
    }


def _assert_unique_candidate_digests(checkpoint) -> None:
    assert len(
        {item.normalized_ast_digest for item in checkpoint.state.sql_candidates}
    ) == (len(checkpoint.state.sql_candidates))


def _finalizer_request(sql: str) -> dict[str, object]:
    return {
        "operation": "finalize_text_to_sql_run",
        "sql_query": sql,
        "row_limit": 10,
        "dry_run_only": False,
    }


def _failed_execution_terminal(run_id: str, sql: str) -> dict[str, object]:
    execution = {
        "success": False,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "execution_time_ms": 3,
        "error_message": "database unavailable",
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": sql,
        "applied_row_limit": 10,
    }
    return {
        "run_id": run_id,
        "status": "failed",
        "reason_code": "EXECUTION_FAILED",
        "sql": sql,
        "generated": True,
        "approved": True,
        "executed": True,
        "dry_run": False,
        "audited": True,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "error": "database unavailable",
        "execution": execution,
        "audit": {"status": "logged", "log_id": "audit-1"},
        "persistence": {"status": "not_attempted"},
        "ambiguity": None,
    }


def test_semantic_failure_is_repaired_by_a_second_candidate(
    monkeypatch, tmp_path
) -> None:
    runtime, store = _runtime(tmp_path)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger)

    output = _run_generation(
        runtime,
        (_sql(_MISSING_FILTER_SQL), _sql(_GOOD_SQL)),
        ledger,
    )

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert output["sql"] == _GOOD_SQL
    assert checkpoint is not None
    checks = _checks_by_candidate(checkpoint.state)
    first, second = checkpoint.state.sql_candidates
    assert checks[first.candidate_id][-1].check_kind is CheckKind.SEMANTIC
    assert checks[first.candidate_id][-1].status is CheckStatus.INCONCLUSIVE
    assert tuple(item.check_kind for item in checks[second.candidate_id]) == (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    assert ledger.counts()["proposal"] == 2
    _assert_unique_candidate_digests(checkpoint)


def test_normalized_duplicate_stagnates_without_second_gate_run(
    monkeypatch, tmp_path
) -> None:
    runtime, store = _runtime(tmp_path)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger)
    equivalent = " select o.status from orders o "

    _run_generation(runtime, (_sql(_MISSING_FILTER_SQL), _sql(equivalent)), ledger)

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.state.stop_reason is SolverStopReason.STAGNATED
    assert len(checkpoint.state.sql_candidates) == 1
    assert ledger.counts() == Counter({"proposal": 2, "safety": 1})


def test_safety_short_circuit_is_repaired_without_downstream_calls(
    monkeypatch, tmp_path
) -> None:
    runtime, store = _runtime(tmp_path)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger, unsafe_sql=_UNSAFE_SQL)

    output = _run_generation(runtime, (_sql(_UNSAFE_SQL), _sql(_GOOD_SQL)), ledger)

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert output["sql"] == _GOOD_SQL
    assert checkpoint is not None
    first, second = checkpoint.state.sql_candidates
    checks = _checks_by_candidate(checkpoint.state)
    assert tuple(item.check_kind for item in checks[first.candidate_id]) == (
        CheckKind.SAFETY,
    )
    assert checks[first.candidate_id][0].status is CheckStatus.FAILED
    assert checks[second.candidate_id][-1].check_kind is CheckKind.EXPLAIN
    assert ledger.counts()["explain"] == 1


def test_explain_failure_is_repaired_without_finalizing_failed_candidate(
    monkeypatch, tmp_path
) -> None:
    runtime, store = _runtime(tmp_path)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(
        monkeypatch,
        ledger,
        explain_failure_sql=_EXPLAIN_FAIL_SQL,
    )

    output = _run_generation(
        runtime,
        (_sql(_EXPLAIN_FAIL_SQL), _sql(_GOOD_SQL)),
        ledger,
    )

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert output["sql"] == _GOOD_SQL
    assert checkpoint is not None
    checks = _checks_by_candidate(checkpoint.state)
    first, second = checkpoint.state.sql_candidates
    assert tuple(item.check_kind for item in checks[first.candidate_id]) == (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    assert checks[first.candidate_id][-1].status is CheckStatus.FAILED
    assert tuple(item.check_kind for item in checks[second.candidate_id]) == (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    assert all(
        item.status is CheckStatus.PASSED for item in checks[second.candidate_id]
    )
    assert ledger.counts()["explain"] == 2
    assert checkpoint.pending_execution is None


def test_execution_failure_is_sealed_and_replayed_exactly(
    monkeypatch, tmp_path
) -> None:
    runtime, store = _runtime(tmp_path)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger)
    output = _run_generation(runtime, (_sql(_GOOD_SQL),), ledger)
    preparation = prepare_finalizer_execution(
        runtime,
        _finalizer_request(output["sql"]),
        id_factory=lambda: "execution-1",
    )
    assert preparation.reservation is not None
    ledger.add("finalizer")
    reconciled = reconcile_known_finalizer(
        store,
        preparation.reservation,
        preparation.state,
        _failed_execution_terminal(_RUN_ID, output["sql"]),
    )
    expected_bytes = reconciled.terminal.terminal_bytes
    durable = store.load(_RUN_ID, _INCARNATION)
    assert durable is not None
    candidate_id = durable.state.sql_candidates[0].candidate_id
    checks = _checks_by_candidate(durable.state)[candidate_id]
    assert tuple(item.check_kind for item in checks) == (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
        CheckKind.EXECUTION,
    )
    assert all(item.status is CheckStatus.PASSED for item in checks[:4])
    assert checks[-1].status is CheckStatus.FAILED
    assert len(durable.state.execution_results) == 1
    execution_result = durable.state.execution_results[0]
    assert execution_result.candidate_id == candidate_id
    assert execution_result.success is False
    assert durable.state.selected_candidate_id is None
    assert durable.state.stop_reason is SolverStopReason.TOOL_FAILURE

    restarted, reopened = _runtime(tmp_path)
    replay = prepare_finalizer_execution(
        restarted,
        _finalizer_request(output["sql"]),
        id_factory=lambda: (_ for _ in ()).throw(
            AssertionError("terminal replay allocated a second execution ID")
        ),
    )

    assert replay.reservation is None and replay.terminal is not None
    assert canonical_json_bytes(replay.terminal.to_mapping()) == expected_bytes
    assert apply_finalizer_checkpoint(
        restarted, reopened.load(_RUN_ID, _INCARNATION)
    ) == (replay.terminal.to_mapping())
    assert ledger.counts()["finalizer"] == 1


def _fresh_research(research: ResearchState) -> tuple[ResearchState, FreshnessContext]:
    observed_at = _OBSERVED_AT
    action = _action(research.revision, target=_column())
    evidence = _evidence(
        "evidence-status-refresh",
        source_kind=EvidenceSourceKind.SCHEMA,
        action=action,
        schema_version=research.schema_namespace_version,
        revision=research.revision + 1,
        observed_at=observed_at,
    )
    refreshed = ResearchState.model_validate(
        {
            **research.model_dump(mode="python"),
            "revision": research.revision + 1,
            "evidence": (*research.evidence, evidence),
            "action_history": (*research.action_history, action),
        }
    )
    freshness = FreshnessContext(
        evaluated_at=observed_at,
        run_id=_RUN_ID,
        run_incarnation=_INCARNATION,
        schema_namespace_version=research.schema_namespace_version,
    )
    return refreshed, freshness


def test_exact_source_reentry_refreshes_authority_before_repair(
    monkeypatch, tmp_path
) -> None:
    runtime, store = _runtime(tmp_path, research_revision=0)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger)

    async def reenter(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        ledger.add("reentry")
        from workflow._text_to_sql_reentry_recovery import (
            build_prepared_targeted_reentry_commit,
        )

        _persist_research_baseline(store, research_state)
        admitted = admit_targeted_reentry(
            solver_state,
            research_state,
            request_id,
            base_revision=solver_state.revision,
            id_factory=id_factory,
        )
        committed = commit_solver_admission(admitted)
        refreshed, freshness = _fresh_research(research_state)
        requirements = validate_coverage_inputs(
            refreshed,
            freshness,
            _RUN_ID,
            _INCARNATION,
        )
        plan = build_prepared_targeted_reentry_commit(
            run_id=research_state.run_id,
            run_incarnation=research_state.run_incarnation,
            research_reentry_id=admitted.record.research_reentry_id,
            missing_evidence_request_id=admitted.record.missing_evidence_request_id,
            source_id=admitted.record.source_id,
            ordinal=admitted.record.ordinal,
            base_solver_revision=committed.revision,
            solver_admission_digest=canonical_digest(committed),
            store_base_research_revision=research_state.revision,
            store_base_research_digest=canonical_digest(research_state),
            projected_research=research_state,
            projected_research_digest=canonical_digest(research_state),
            action=refreshed.action_history[-1],
            hypotheses=refreshed.hypotheses,
            bindings=refreshed.bindings,
            join_candidates=refreshed.join_candidates,
            invocation_id="w6-07-reentry-invocation",
            reservation_digest=canonical_digest({"test": "reentry-reservation"}),
            policy_digest=canonical_digest({"test": "reentry-policy"}),
            schema_namespace_version=research_state.schema_namespace_version,
        )
        research_store = AdaptiveResearchStateStore(store.db_path)
        try:
            research_store.prepare_targeted_reentry_commit(plan)
            research_store.commit_prepared_targeted_reentry(plan, refreshed)
        finally:
            research_store.close()
        finalized = finalize_targeted_reentry(
            committed,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.COMPLETED,
            base_revision=committed.revision,
            research_state=refreshed,
            freshness_context=freshness,
            requirements=requirements,
        )
        return SimpleNamespace(
            solver_state=finalized.state,
            research_state=refreshed,
            record=finalized.record,
            freshness_context=freshness,
            requirements=requirements,
        )

    output = _run_generation(
        runtime,
        (_missing(), _sql(_GOOD_SQL)),
        ledger,
        reenter=reenter,
    )

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert output["sql"] == _GOOD_SQL
    assert checkpoint is not None
    assert len(checkpoint.state.missing_evidence_requests) == 1
    assert len(checkpoint.state.research_reentries) == 1
    record = checkpoint.state.research_reentries[0]
    assert record.source_id == "status"
    assert record.status is ResearchReentryStatus.COMPLETED
    assert record.evidence_ids == ("evidence-status-refresh",)
    assert ledger.counts()["reentry"] == 1


def test_model_turn_budget_stops_before_ninth_call(monkeypatch, tmp_path) -> None:
    runtime, store = _runtime(tmp_path, research_revision=0)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger)
    proposals = tuple(
        _sql(_MISSING_FILTER_SQL + f" ORDER BY o.id LIMIT {index}")
        for index in range(1, _MODEL_LIMIT + 1)
    )

    _run_generation(runtime, proposals, ledger)

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert checkpoint is not None and checkpoint.terminal is not None
    assert checkpoint.state.stop_reason is SolverStopReason.BUDGET_EXHAUSTED
    assert len(checkpoint.state.sql_candidates) == _MODEL_LIMIT
    assert ledger.counts()["proposal"] == _MODEL_LIMIT


def test_reentry_budget_terminal_does_not_reopen_solver(monkeypatch, tmp_path) -> None:
    runtime, store = _runtime(tmp_path, research_revision=0)
    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    _install_tool_boundaries(monkeypatch, ledger)

    async def exhausted(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        ledger.add("reentry")
        _persist_research_baseline(store, research_state)
        admitted = admit_targeted_reentry(
            solver_state,
            research_state,
            request_id,
            base_revision=solver_state.revision,
            id_factory=id_factory,
        )
        committed = commit_solver_admission(admitted)
        finalized = finalize_targeted_reentry(
            committed,
            admitted.record.research_reentry_id,
            ResearchReentryStatus.BUDGET_EXHAUSTED,
            base_revision=committed.revision,
        )
        return SimpleNamespace(
            solver_state=finalized.state,
            research_state=research_state,
            record=finalized.record,
        )

    first = _run_generation(runtime, (_missing(),), ledger, reenter=exhausted)
    before = ledger.counts()
    restarted, _ = _runtime(tmp_path, research_revision=0)

    async def unexpected_reentry(*_args, **_kwargs):
        ledger.add("reentry")
        raise AssertionError("sealed budget terminal reopened re-entry")

    replay = _run_generation(
        restarted,
        (),
        ledger,
        reenter=unexpected_reentry,
    )

    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert replay == first
    assert checkpoint is not None
    assert checkpoint.state.stop_reason is SolverStopReason.MISSING_EVIDENCE
    assert checkpoint.state.research_reentries[-1].status is (
        ResearchReentryStatus.BUDGET_EXHAUSTED
    )
    assert ledger.counts() == before


def _spawn_worker(root_text: str, crash_after_action: int | None) -> None:
    root = Path(root_text)
    ledger = _SqliteCallLedger(root / "calls.sqlite")
    runtime, _store = _runtime(root)
    import custom_tools.text_to_sql.adaptive.solver_runner as runner

    def safety(*_args, **_kwargs):
        ledger.add("safety")
        return {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
        }

    def explain(*_args, **_kwargs):
        ledger.add("explain")
        return {
            "plan": "SCAN orders",
            "estimated_cost": 1.0,
            "rows_to_scan": 2,
            "issues": [],
            "profile_name": "w6-07",
            "policy_version": "1",
        }

    runner.core.sql_safety_check = safety
    runner.core.sql_explain = explain
    store = runtime.solver_checkpoint_store
    original_commit = store.commit_non_execution

    def crash_commit(*args, **kwargs):
        checkpoint = original_commit(*args, **kwargs)
        if (
            crash_after_action is not None
            and checkpoint.cursor.next_action_revision == crash_after_action
        ):
            os._exit(86)
        return checkpoint

    store.commit_non_execution = crash_commit
    identifiers = _ids()
    try:
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=_ScriptedProposalBoundary((_sql(_GOOD_SQL),), ledger),
                safety_policy=load_startup_safety_policy(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                id_factory=identifiers.__next__,
            )
        )
    except AssertionError:
        os._exit(87)


def _spawn_reentry_worker(root_text: str, crash_on_admission: bool) -> None:
    root = Path(root_text)
    ledger = _SqliteCallLedger(root / "calls.sqlite")
    runtime, store = _runtime(root, research_revision=0)
    proposals = () if store.load(_RUN_ID, _INCARNATION) is not None else (_missing(),)

    async def reenter(
        solver_state,
        research_state,
        request_id,
        *,
        commit_solver_admission,
        id_factory,
        **_kwargs,
    ):
        ledger.add("reentry")
        if not crash_on_admission:
            raise AssertionError("durable re-entry admission was invoked again")
        admitted = admit_targeted_reentry(
            solver_state,
            research_state,
            request_id,
            base_revision=solver_state.revision,
            id_factory=id_factory,
        )
        _persist_research_baseline(store, research_state)
        commit_solver_admission(admitted)
        os._exit(86)

    identifiers = _ids()
    try:
        asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=_ScriptedProposalBoundary(proposals, ledger),
                safety_policy=load_startup_safety_policy(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                reenter=reenter,
                id_factory=identifiers.__next__,
            )
        )
    except AssertionError:
        os._exit(87)


def _spawn_finalizer_worker(root_text: str, crash_window: str | None) -> None:
    root = Path(root_text)
    ledger = _SqliteCallLedger(root / "calls.sqlite")
    runtime, store = _runtime(root)
    import custom_tools.text_to_sql.adaptive.solver_runner as runner

    def safety(*_args, **_kwargs):
        ledger.add("safety")
        return {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
        }

    def explain(*_args, **_kwargs):
        ledger.add("explain")
        return {
            "plan": "SCAN orders",
            "estimated_cost": 1.0,
            "rows_to_scan": 2,
            "issues": [],
            "profile_name": "w6-07",
            "policy_version": "1",
        }

    runner.core.sql_safety_check = safety
    runner.core.sql_explain = explain
    proposals = (
        () if store.load(_RUN_ID, _INCARNATION) is not None else (_sql(_GOOD_SQL),)
    )
    identifiers = _ids()
    try:
        output = asyncio.run(
            run_adaptive_sql_generation(
                runtime,
                propose=_ScriptedProposalBoundary(proposals, ledger),
                safety_policy=load_startup_safety_policy(),
                row_limit=10,
                dry_run_only=False,
                table_namespace="main",
                id_factory=identifiers.__next__,
            )
        )
        if runtime.verified_solver_terminal is not None:
            return
        preparation = prepare_finalizer_execution(
            runtime,
            _finalizer_request(output["sql"]),
            id_factory=lambda: "spawn-execution-1",
        )
        if preparation.reservation is None:
            raise AssertionError("fresh finalizer preparation lacked a reservation")
        ledger.add("finalizer")
        if crash_window == "reservation":
            os._exit(86)
        reconciled = reconcile_known_finalizer(
            store,
            preparation.reservation,
            preparation.state,
            _failed_execution_terminal(_RUN_ID, output["sql"]),
        )
        if crash_window == "known_reconciliation":
            os._exit(86)
        apply_finalizer_checkpoint(runtime, reconciled)
    except AssertionError:
        os._exit(87)


def _join_process(process, expected_exitcode: int) -> None:
    process.join(20)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)
    assert not process.is_alive()
    assert not timed_out, "child process timed out"
    assert process.exitcode == expected_exitcode


@pytest.mark.parametrize("crash_after_action", range(1, 6))
def test_spawn_resume_does_not_repeat_durable_candidate_or_gate(
    tmp_path, crash_after_action
) -> None:
    context = multiprocessing.get_context("spawn")
    first = context.Process(
        target=_spawn_worker,
        args=(str(tmp_path), crash_after_action),
    )
    first.start()
    _join_process(first, 86)

    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    expected_before_resume = Counter({"proposal": 1})
    if crash_after_action >= 2:
        expected_before_resume["safety"] = 1
    if crash_after_action >= 5:
        expected_before_resume["explain"] = 1
    assert ledger.counts() == expected_before_resume

    second = context.Process(target=_spawn_worker, args=(str(tmp_path), None))
    second.start()
    _join_process(second, 0)

    assert ledger.counts() == Counter({"proposal": 1, "safety": 1, "explain": 1})
    _, store = _runtime(tmp_path)
    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert checkpoint is not None
    assert checkpoint.cursor.next_action_revision == 5
    assert len(checkpoint.state.check_results) == 4
    _assert_unique_candidate_digests(checkpoint)


def test_spawn_resume_terminalizes_durable_reentry_admission_once(tmp_path) -> None:
    class StuckProcess:
        def __init__(self) -> None:
            self.alive = True
            self.exitcode = None
            self.calls = []

        def join(self, timeout) -> None:
            self.calls.append(("join", timeout))

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.calls.append("terminate")

        def kill(self) -> None:
            self.calls.append("kill")
            self.alive = False
            self.exitcode = -9

    stuck = StuckProcess()
    with pytest.raises(AssertionError, match="child process timed out"):
        _join_process(stuck, 0)
    assert stuck.calls == [
        ("join", 20),
        "terminate",
        ("join", 1),
        "kill",
        ("join", 1),
    ]
    assert not stuck.is_alive()

    context = multiprocessing.get_context("spawn")
    first = context.Process(
        target=_spawn_reentry_worker,
        args=(str(tmp_path), True),
    )
    first.start()
    _join_process(first, 86)

    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    assert ledger.counts() == Counter({"proposal": 1, "reentry": 1})

    second = context.Process(
        target=_spawn_reentry_worker,
        args=(str(tmp_path), False),
    )
    second.start()
    _join_process(second, 0)

    assert ledger.counts() == Counter({"proposal": 1, "reentry": 1})
    _, store = _runtime(tmp_path, research_revision=0)
    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert checkpoint is not None and checkpoint.terminal is not None
    assert len(checkpoint.state.research_reentries) == 1
    assert checkpoint.state.research_reentries[0].status is (
        ResearchReentryStatus.PROTOCOL_FAILURE
    )


@pytest.mark.parametrize(
    ("crash_window", "reason_code"),
    (
        ("reservation", "EXECUTION_UNKNOWN"),
        ("known_reconciliation", "EXECUTION_FAILED"),
    ),
)
def test_spawn_resume_never_repeats_finalizer(
    tmp_path, crash_window, reason_code
) -> None:
    context = multiprocessing.get_context("spawn")
    first = context.Process(
        target=_spawn_finalizer_worker,
        args=(str(tmp_path), crash_window),
    )
    first.start()
    _join_process(first, 86)

    ledger = _SqliteCallLedger(tmp_path / "calls.sqlite")
    assert ledger.counts() == Counter(
        {"proposal": 1, "safety": 1, "explain": 1, "finalizer": 1}
    )

    second = context.Process(
        target=_spawn_finalizer_worker,
        args=(str(tmp_path), None),
    )
    second.start()
    _join_process(second, 0)

    counts = ledger.counts()
    assert counts == Counter({"proposal": 1, "safety": 1, "explain": 1, "finalizer": 1})
    _, store = _runtime(tmp_path)
    checkpoint = store.load(_RUN_ID, _INCARNATION)
    assert checkpoint is not None and checkpoint.terminal is not None
    terminal = json.loads(checkpoint.terminal.terminal_bytes)
    assert terminal["reason_code"] == reason_code
