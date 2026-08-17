"""W5-05 deterministic gate immediately before SQL execution."""

from dataclasses import replace
import importlib
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive import pre_execution_gate as gate_module
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckStatus,
    PredicateOperator,
    SemanticItemKind,
    SemanticItemStatus,
    SolverState,
)
from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
)
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
from custom_tools.text_to_sql.adaptive.pre_execution_gate import (
    PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY,
    PRE_EXECUTION_GATE_RUNTIME_KEY,
    PreExecutionGateReceipt,
    create_pre_execution_gate_capability,
    evaluate_pre_execution_gate_capability,
    release_pre_execution_gate_capture,
    take_pre_execution_gate_receipt,
)
from custom_tools.text_to_sql.adaptive.pre_execution_gate_runtime import (
    INVALID_PRE_EXECUTION_GATE_RUNTIME,
    build_pre_execution_gate_runtime,
)
from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
from custom_tools.text_to_sql.adaptive.solver_protocol import (
    SolverProposalV1,
    SqlCandidateProposal,
)
from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
from tests.text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    RUN_ID,
    _document_binding,
    _document_evidence,
    _state,
)
from text_to_sql_semantic_checks_helpers import (
    POSTGRES_DSN,
    ItemSpec,
    build_case,
)
from workflow.deadline import DeadlineBudget
from workflow.text_to_sql_typed_runtime import (
    TextToSqlTypedRuntime,
    _ADMISSION_CAPABILITY,
    capture_text_to_sql_typed_admission,
)
from workflow._text_to_sql_document_authority import (
    CanonicalSchemaDocumentRegistry,
    empty_schema_document_registry,
)
from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context


VALID_SQL = "SELECT o.status FROM orders o WHERE o.status = 'active'"
MISSING_FILTER_SQL = "SELECT o.status FROM orders o"
INVALID_SCHEMA_SQL = "SELECT o.missing_column FROM orders o WHERE o.status = 'active'"


def _case(sql: str):
    return build_case(
        sql,
        (
            ItemSpec(
                source_id="status",
                kind=SemanticItemKind.FILTER,
                table="orders",
                column="status",
                operator=PredicateOperator.EQ,
                literal="active",
            ),
        ),
    )


def _capability(sql: str):
    case = _case(VALID_SQL)
    return create_pre_execution_gate_capability(
        state=case.state,
        requirements=case.requirements,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        expected_sql=sql,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        deadline=DeadlineBudget.from_duration(10),
        is_cancelled=lambda: False,
    )


def _safe_result() -> dict[str, object]:
    return {
        "is_safe": True,
        "issues": [],
        "advisory_issues": [],
        "safety_status": "safe",
        "llm_audit": "skipped_static_only",
    }


def _unsafe_result() -> dict[str, object]:
    return {
        "is_safe": False,
        "issues": [
            {
                "issue_type": "FORBIDDEN_COMMAND",
                "description": "forbidden",
            }
        ],
        "advisory_issues": [],
        "safety_status": "unsafe",
        "llm_audit": "skipped_static_unsafe",
    }


def _evaluate(capability: object, sql: str = VALID_SQL):
    return evaluate_pre_execution_gate_capability(
        capability,
        expected_run_id=RUN_ID,
        expected_sql=sql,
        safety_policy=None,
    )


def test_gate_passes_only_static_safety_then_semantic(monkeypatch) -> None:
    calls = []

    def safety(sql_query, **kwargs):
        calls.append((sql_query, kwargs))
        return _safe_result()

    monkeypatch.setattr(core, "sql_safety_check", safety)
    receipt = _evaluate(_capability(VALID_SQL))

    assert receipt.allowed is True
    assert receipt.primary_check_id is None
    assert tuple(result.check_kind for result in receipt.check_results) == (
        CheckKind.SAFETY,
        CheckKind.SEMANTIC,
    )
    assert all(result.status is CheckStatus.PASSED for result in receipt.check_results)
    assert receipt.semantic_coverage is not None
    assert receipt.source_coverage_available is True
    assert receipt.semantic_coverage.required_source_ids == ("status",)
    assert any(
        "status" in annotation.source_ids
        for annotation in receipt.semantic_coverage.annotations
    )
    assert calls == [
        (
            VALID_SQL,
            {
                "dsn": POSTGRES_DSN,
                "safety_policy": None,
                "static_only": True,
            },
        )
    ]


def test_scalar_subquery_with_physical_formula_binding_reaches_pre_execution_authority(
    monkeypatch,
) -> None:
    sql = (
        "SELECT p.height FROM players AS p "
        "WHERE p.height = (SELECT MAX(height) FROM players) "
        "OR p.height = (SELECT MIN(height) FROM players)"
    )
    case = build_case(
        sql,
        (
            ItemSpec(
                source_id="height-extremes",
                kind=SemanticItemKind.FORMULA,
                table="players",
                column="height",
            ),
        ),
    )
    capability = create_pre_execution_gate_capability(
        state=case.state,
        requirements=case.requirements,
        dsn=POSTGRES_DSN,
        table_namespace="main",
        expected_sql=sql,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        deadline=DeadlineBudget.from_duration(10),
        is_cancelled=lambda: False,
    )
    monkeypatch.setattr(core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result())

    receipt = _evaluate(capability, sql)

    assert receipt.allowed is True
    assert all(result.status is CheckStatus.PASSED for result in receipt.check_results)


def test_missing_filter_is_nonblocking_for_authority_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )

    receipt = _evaluate(_capability(MISSING_FILTER_SQL), MISSING_FILTER_SQL)

    assert receipt.allowed is True
    assert [result.status for result in receipt.check_results] == [
        CheckStatus.PASSED,
        CheckStatus.PASSED,
    ]
    assert receipt.check_results[1].failure_code is None
    assert receipt.primary_check_id is None
    assert receipt.semantic_coverage is not None
    assert receipt.source_coverage_available is True
    assert receipt.semantic_coverage.required_source_ids == ("status",)


def test_unsafe_static_result_short_circuits_semantic(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _unsafe_result()
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("semantic check must not run after safety failure")

    monkeypatch.setattr(gate_module, "evaluate_semantic_authority_checks", forbidden)
    receipt = _evaluate(_capability(VALID_SQL))

    assert receipt.allowed is False
    assert len(receipt.check_results) == 1
    assert receipt.check_results[0].failure_code is CheckFailureCode.SAFETY_REJECTED
    assert receipt.primary_check_id == receipt.check_results[0].check_id
    assert receipt.semantic_coverage is None
    assert receipt.source_coverage_available is False


def test_ast_shape_unsupported_is_typed_primary_before_safety(monkeypatch) -> None:
    sql = "SELECT 1; SELECT 2"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("safety must not run without a mapped candidate")

    monkeypatch.setattr(core, "sql_safety_check", forbidden)
    receipt = _evaluate(_capability(sql), sql)

    assert receipt.allowed is False
    assert receipt.normalized_ast_digest is None
    assert receipt.semantic_coverage is None
    assert receipt.source_coverage_available is False
    assert len(receipt.check_results) == 1
    assert (
        receipt.check_results[0].failure_code
        is CheckFailureCode.AST_SHAPE_UNSUPPORTED
    )
    assert receipt.primary_check_id == receipt.check_results[0].check_id


def test_malformed_safety_result_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: {"is_safe": True}
    )
    receipt = _evaluate(_capability(VALID_SQL))

    assert receipt.allowed is False
    assert receipt.check_results[0].status is CheckStatus.INCONCLUSIVE
    assert receipt.check_results[0].failure_code is CheckFailureCode.CHECK_MALFORMED
    assert receipt.primary_check_id == receipt.check_results[0].check_id
    assert receipt.semantic_coverage is None
    assert receipt.source_coverage_available is False


def test_semantic_ast_failure_fails_closed_without_coverage(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    monkeypatch.setattr(
        gate_module,
        "build_semantic_ast",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid AST")),
    )

    receipt = _evaluate(_capability(VALID_SQL))

    assert receipt.allowed is False
    assert receipt.semantic_coverage is None
    assert receipt.source_coverage_available is False
    assert tuple(result.check_kind for result in receipt.check_results) == (
        CheckKind.SAFETY,
        CheckKind.SEMANTIC,
    )
    assert receipt.check_results[-1].status is CheckStatus.INCONCLUSIVE
    assert receipt.primary_check_id == receipt.check_results[-1].check_id


def test_forged_wrong_run_and_changed_sql_capabilities_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    capability = _capability(VALID_SQL)

    receipts = (
        _evaluate(replace(capability)),
        evaluate_pre_execution_gate_capability(
            capability,
            expected_run_id="other-run",
            expected_sql=VALID_SQL,
            safety_policy=None,
        ),
        _evaluate(capability, "SELECT o.status FROM orders o WHERE o.status = 'other'"),
    )

    assert all(receipt.allowed is False for receipt in receipts)
    assert all(
        receipt.check_results[0].failure_code is CheckFailureCode.CHECK_INPUT_INVALID
        for receipt in receipts
    )


def test_receipt_rejects_wrong_order_or_duplicate_primary(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    passed = _evaluate(_capability(VALID_SQL))
    payload = passed.model_dump(mode="python")

    with pytest.raises(ValueError):
        PreExecutionGateReceipt.model_validate(
            {**payload, "check_results": tuple(reversed(passed.check_results))}
        )
    with pytest.raises(ValueError):
        PreExecutionGateReceipt.model_validate(
            {
                **payload,
                "allowed": False,
                "primary_check_id": passed.check_results[0].check_id,
            }
        )
    with pytest.raises(ValueError):
        PreExecutionGateReceipt.model_validate({**payload, "semantic_coverage": None})
    with pytest.raises(ValueError):
        PreExecutionGateReceipt.model_validate(
            {**payload, "source_coverage_available": False}
        )

    missing = _evaluate(_capability(MISSING_FILTER_SQL), MISSING_FILTER_SQL)
    with pytest.raises(ValueError):
        PreExecutionGateReceipt.model_validate(
            {
                **missing.model_dump(mode="python"),
                "semantic_coverage": missing.semantic_coverage.model_copy(
                    update={
                        "required_source_ids": (),
                        "evidence_ids": (),
                        "annotations": tuple(
                            annotation.model_copy(
                                update={"source_ids": (), "evidence_ids": ()}
                            )
                            for annotation in missing.semantic_coverage.annotations
                        ),
                    }
                ),
                "source_coverage_available": False,
            }
        )


def test_capability_type_and_registrar_are_not_public_symbols() -> None:
    names = vars(gate_module)
    assert "PreExecutionGateCapability" not in names
    assert not any(
        name.startswith("_register") and "capability" in name for name in names
    )


def _on_runtime(
    *,
    terminal: bool = False,
    deadline: DeadlineBudget | None = None,
) -> TextToSqlTypedRuntime:
    from datetime import UTC, datetime

    from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
    from custom_tools.text_to_sql.adaptive.replay_inputs import ResearchTerminalReplayInput
    from test_text_to_sql_solver_runner import (
        _passed_through,
        _runtime as _solver_runtime,
    )
    from workflow.adaptive_state_store import (
        AdaptiveCheckpointKey,
        AdaptiveLoopKind,
        AdaptiveStateStore,
    )

    _, state, requirements, loaded_schema = _solver_runtime()
    solver_state = SolverState(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=state.revision,
        schema_namespace_version=state.schema_namespace_version,
        query_spec=state.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    ids = iter(("candidate-1", "plan-1", "action-1"))
    solver_state = apply_solver_proposal(
        solver_state,
        SolverProposalV1(
            proposal_version=1,
            proposal=SqlCandidateProposal(
                proposal_kind="sql_candidate",
                sql=VALID_SQL,
            ),
        ),
        base_revision=solver_state.revision,
        dsn="sqlite:///unused.db",
        table_namespace="main",
        requirements=requirements,
        id_factory=lambda: next(ids),
    ).state
    solver_state = _passed_through(
        solver_state,
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    )
    runtime_deadline = deadline or DeadlineBudget.from_duration(10)
    schema_scope = loaded_schema.namespace.scope.to_mapping()
    admission = capture_text_to_sql_typed_admission(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        deadline=runtime_deadline,
        query=state.query_spec.original_text,
        dsn="sqlite:///unused.db",
        schema_scope=schema_scope,
    )
    assert admission is not None
    runtime = TextToSqlTypedRuntime(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        deadline=runtime_deadline,
        query=state.query_spec.original_text,
        dsn="sqlite:///unused.db",
        schema_scope=schema_scope,
        research_state_store=None,
        checkpoint_store=None,
        budget_ledger=None,
        solver_checkpoint_store=None,
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
    )
    runtime.verified_research_state = state
    runtime.verified_solver_state = solver_state
    runtime.verified_solver_candidate_id = solver_state.sql_candidates[-1].candidate_id
    runtime.verified_research_outcome = SimpleNamespace(
        stop_reason=ResearchStopReason.COMPLETE
    )
    runtime.loaded_schema = loaded_schema
    runtime.document_registry = empty_schema_document_registry(
        loaded_schema.namespace.scope,
        loaded_schema.namespace,
    )
    if terminal:
        store = AdaptiveStateStore(":memory:")
        for revision in range(state.revision):
            key = AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                revision,
            )
            store.record_planned(
                key,
                expected_revision=None if revision == 0 else revision - 1,
                action={"kind": "historical_planned", "revision": revision},
            )
            store.record_observed(
                key,
                expected_revision=revision,
                action={"kind": "historical_observed", "revision": revision},
            )
        store.record_replayable_terminal(
            AdaptiveCheckpointKey(
                state.run_id,
                state.run_incarnation,
                AdaptiveLoopKind.RESEARCH,
                state.revision,
            ),
            expected_revision=None if state.revision == 0 else state.revision - 1,
            action={
                "affected_source_ids": [],
                "citation_evidence_ids": [],
                "contract_version": 2,
                "kind": "research_terminal",
                "rejection_signatures": [],
                "reason": "COMPLETE",
                "ambiguity": None,
            },
            replay_input=ResearchTerminalReplayInput(
                freshness_context=FreshnessContext(
                    evaluated_at=datetime.now(UTC),
                    run_id=state.run_id,
                    run_incarnation=state.run_incarnation,
                    schema_namespace_version=state.schema_namespace_version,
                )
            ),
        )
        runtime.checkpoint_store = store
    return runtime


def _release_capture(runtime: TextToSqlTypedRuntime) -> None:
    release_pre_execution_gate_capture(runtime)


def test_runtime_builder_requires_complete_typed_research(monkeypatch) -> None:
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    runtime = _on_runtime(terminal=True)

    capability = build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)
    receipt = evaluate_pre_execution_gate_capability(
        capability,
        expected_run_id=RUN_ID,
        expected_sql=VALID_SQL,
        safety_policy=None,
    )

    assert receipt.allowed is True
    assert take_pre_execution_gate_receipt(runtime) is receipt
    _release_capture(runtime)
    runtime.verified_research_outcome = SimpleNamespace(
        stop_reason=ResearchStopReason.AMBIGUOUS
    )
    assert (
        build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)
        is INVALID_PRE_EXECUTION_GATE_RUNTIME
    )


def test_runtime_builder_rejects_missing_loaded_schema_before_terminal_execution(
    monkeypatch,
) -> None:
    events = _terminal_side_effects(monkeypatch)
    runtime = _on_runtime(terminal=True)
    loaded_schema = runtime.loaded_schema
    runtime.loaded_schema = SimpleNamespace(
        schema=loaded_schema.schema,
        namespace=loaded_schema.namespace,
        source=loaded_schema.source,
    )

    capability = build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)

    assert capability is INVALID_PRE_EXECUTION_GATE_RUNTIME
    token = _gate_context(capability)
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["reason_code"] == "DETERMINISTIC_CHECK_REJECTED"
    assert events == []


def test_production_capture_precedes_terminal_execution(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    runtime = _on_runtime(terminal=True)
    real_semantic = gate_module.evaluate_semantic_authority_checks

    def safety(*_args, **_kwargs):
        events.append("safety")
        return _safe_result()

    def semantic(*args, **kwargs):
        events.append("semantic")
        return real_semantic(*args, **kwargs)

    monkeypatch.setattr(core, "sql_safety_check", safety)
    monkeypatch.setattr(gate_module, "evaluate_semantic_authority_checks", semantic)
    capability = build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)
    executor = core.secure_db_executor

    def checked_executor(*args, **kwargs):
        receipt = take_pre_execution_gate_receipt(runtime)
        assert receipt is not None
        assert receipt.allowed is True
        return executor(*args, **kwargs)

    monkeypatch.setattr(core, "secure_db_executor", checked_executor)
    token = _gate_context(capability)
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert events == [
        "safety",
        "semantic",
        "executor",
        "audit",
        "persistence",
    ]
    assert take_pre_execution_gate_receipt(runtime) is None
    _release_capture(runtime)


def test_capture_conflict_fail_closes_terminal_before_executor(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    runtime = _on_runtime(terminal=True)
    capability = build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)
    monkeypatch.setattr(core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result())
    evaluate_pre_execution_gate_capability(
        capability,
        expected_run_id=RUN_ID,
        expected_sql=VALID_SQL,
        safety_policy=None,
    )
    monkeypatch.setattr(core, "sql_safety_check", lambda *_args, **_kwargs: _unsafe_result())
    token = _gate_context(capability)
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["reason_code"] == "DETERMINISTIC_CHECK_REJECTED"
    assert events == []
    receipt = take_pre_execution_gate_receipt(runtime)
    assert receipt is not None
    assert receipt.allowed is False
    _release_capture(runtime)


def test_schema_rejection_blocks_terminal_before_executor_and_semantic(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    runtime = _on_runtime(terminal=True)
    capability = build_pre_execution_gate_runtime(runtime, sql_query=INVALID_SCHEMA_SQL)
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    monkeypatch.setattr(
        gate_module,
        "evaluate_semantic_authority_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("semantic authority must not run after schema rejection")
        ),
    )
    token = _gate_context(capability)
    try:
        result = _finalize(INVALID_SCHEMA_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["reason_code"] == "DETERMINISTIC_CHECK_REJECTED"
    assert events == []
    receipt = take_pre_execution_gate_receipt(runtime)
    assert receipt is not None
    assert tuple(item.check_kind for item in receipt.check_results) == (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
    )
    assert receipt.check_results[-1].failure_code is CheckFailureCode.SCHEMA_REJECTED
    _release_capture(runtime)


def test_runtime_builder_rejects_missing_durable_terminal_authority() -> None:
    runtime = _on_runtime()

    assert (
        build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)
        is INVALID_PRE_EXECUTION_GATE_RUNTIME
    )


@pytest.mark.parametrize(
    ("availability", "source_version"),
    (
        (DocumentSourceAvailability.AVAILABLE, "v2"),
        (DocumentSourceAvailability.REMOVED, None),
        (DocumentSourceAvailability.UNAVAILABLE, None),
    ),
)
def test_runtime_builder_rejects_changed_or_unavailable_document_authority(
    availability,
    source_version,
) -> None:
    runtime = _on_runtime()
    state = _state(
        bindings=(_document_binding("source-a", "binding-a", "document-evidence"),),
        evidence=(_document_evidence("document-evidence"),),
        item_specs=(("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),),
    )
    runtime.verified_research_state = state
    namespace = runtime.loaded_schema.namespace
    document = SchemaEvidenceDocument(
        document_id="coverage-document",
        namespace="main",
        schema_namespace_version=f"sha256:{namespace.version_key}",
        source_version="v1",
        title="coverage",
        content="coverage",
        target=None,
    )
    runtime.document_registry = CanonicalSchemaDocumentRegistry(
        namespace.scope,
        namespace,
        (document,),
        lambda ids: tuple(
            DocumentSourceState(
                document_id=document_id,
                availability=availability,
                source_version=source_version,
            )
            for document_id in ids
        ),
    )

    assert (
        build_pre_execution_gate_runtime(runtime, sql_query=VALID_SQL)
        is INVALID_PRE_EXECUTION_GATE_RUNTIME
    )


def _terminal_side_effects(monkeypatch):
    events = []

    def executor(sql_query, **kwargs):
        events.append("executor")
        return {
            "success": True,
            "data": [["active"]],
            "columns": ["status"],
            "rows_affected": 1,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": sql_query,
            "applied_row_limit": kwargs["row_limit"],
        }

    def audit(_entry):
        events.append("audit")
        return {"status": "logged", "log_id": "audit-1"}

    def persist(**_kwargs):
        events.append("persistence")
        return {"status": "saved", "filename": "query.md", "path": "/tmp/query.md"}

    monkeypatch.setattr(core, "secure_db_executor", executor)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)
    return events


def _finalize(sql: str):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    return terminal.finalize_text_to_sql_run(
        sql,
        "status active",
        POSTGRES_DSN,
        10,
        False,
        "session-1",
        RUN_ID,
    )


def _gate_context(capability: object | None = None):
    values = {PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY: True}
    if capability is not None:
        values[PRE_EXECUTION_GATE_RUNTIME_KEY] = capability
    return set_tool_runtime_context(values)


def test_typed_gate_allows_deterministic_pass(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    token = _gate_context(_capability(VALID_SQL))
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert result["approved"] is True
    assert events == ["executor", "audit", "persistence"]


def test_legacy_execution_freshness_metadata_does_not_block_executor(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    token = set_tool_runtime_context(
        {
            PRE_EXECUTION_GATE_REQUIRED_RUNTIME_KEY: True,
            PRE_EXECUTION_GATE_RUNTIME_KEY: _capability(VALID_SQL),
            "_text_to_sql_execution_freshness": object(),
        }
    )
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert events == ["executor", "audit", "persistence"]


def test_typed_gate_allows_missing_filter_for_post_execution_review(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    monkeypatch.setattr(
        core, "sql_safety_check", lambda *_args, **_kwargs: _safe_result()
    )
    token = _gate_context(_capability(MISSING_FILTER_SQL))
    try:
        result = _finalize(MISSING_FILTER_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert result["approved"] is True
    assert events == ["executor", "audit", "persistence"]


@pytest.mark.parametrize("capability", [None, object()])
def test_required_on_gate_missing_or_invalid_fails_closed(
    monkeypatch,
    capability,
) -> None:
    events = _terminal_side_effects(monkeypatch)
    token = _gate_context(capability)
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "abstained"
    assert result["reason_code"] == "DETERMINISTIC_CHECK_REJECTED"
    assert events == []


def test_safety_semantic_then_single_executor_order(monkeypatch) -> None:
    events = _terminal_side_effects(monkeypatch)
    real_semantic = gate_module.evaluate_semantic_authority_checks

    def safety(*_args, **_kwargs):
        events.append("safety")
        return _safe_result()

    def semantic(*args, **kwargs):
        events.append("semantic")
        return real_semantic(*args, **kwargs)

    monkeypatch.setattr(core, "sql_safety_check", safety)
    monkeypatch.setattr(gate_module, "evaluate_semantic_authority_checks", semantic)
    token = _gate_context(_capability(VALID_SQL))
    try:
        result = _finalize(VALID_SQL)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert events == [
        "safety",
        "semantic",
        "executor",
        "audit",
        "persistence",
    ]
