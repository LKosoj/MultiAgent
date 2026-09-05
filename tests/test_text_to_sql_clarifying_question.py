from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.ambiguity import AmbiguityReport
from custom_tools.text_to_sql.adaptive.models import (
    BudgetState,
    ColumnRef,
    EvidenceSourceKind,
    ExpectedResultShape,
    MissingEvidenceRequest,
    QuerySpec,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SolverAction,
    SolverActionKind,
    SolverState,
    SolverStopReason,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.research_loop import ResearchLoopOutcome
from custom_tools.text_to_sql.eval.release_inputs import canonical_runtime_environment
from workflow.enhanced_engine import EnhancedWorkflowEngine
from workflow.models import WorkflowContext
from workflow.text_to_sql_clarifying_question import (
    MAX_CLARIFICATION_OPTIONS,
    MAX_CLARIFICATION_QUESTION_CHARS,
    build_text_to_sql_clarifying_question,
    clarifying_questions_enabled,
)
from workflow.text_to_sql_contract import (
    TextToSqlTerminalResult,
    TextToSqlTerminalStatus,
)


_SCHEMA = "sha256:" + "1" * 64
_ENV = "TEXT_TO_SQL_CLARIFYING_QUESTIONS"


def _query_spec() -> QuerySpec:
    item = SemanticItem(
        source_id="source-1",
        kind=SemanticItemKind.FILTER,
        source_text="private-filter",
        normalized_meaning="private-filter",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    return QuerySpec(
        run_id="run-1",
        run_incarnation="incarnation-1",
        revision=0,
        schema_namespace_version=_SCHEMA,
        query_id="query-1",
        original_text="private-filter",
        semantic_items=(item,),
        requested_output_source_ids=(),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def _budget_state() -> BudgetState:
    return BudgetState(
        initial_wall_clock_ms=0,
        used_wall_clock_ms=0,
        remaining_wall_clock_ms=0,
        initial_model_calls=0,
        used_model_calls=0,
        remaining_model_calls=0,
        initial_model_tokens=0,
        used_model_tokens=0,
        remaining_model_tokens=0,
        initial_db_probe_ms=0,
        used_db_probe_ms=0,
        remaining_db_probe_ms=0,
        initial_rows=0,
        used_rows=0,
        remaining_rows=0,
        initial_bytes=0,
        used_bytes=0,
        remaining_bytes=0,
    )


def _ambiguity() -> AmbiguityReport:
    return AmbiguityReport(
        interpretations=("Первое толкование.", "Второе толкование."),
        citation_evidence_ids=("evidence-1",),
        missing_distinguishing_fact="не указан признак различия.",
    )


def _research_outcome(
    *,
    stop_reason: ResearchStopReason = ResearchStopReason.AMBIGUOUS,
    ambiguity: AmbiguityReport | None = None,
) -> ResearchLoopOutcome:
    query_spec = _query_spec()
    state = ResearchState(
        run_id=query_spec.run_id,
        run_incarnation=query_spec.run_incarnation,
        revision=0,
        schema_namespace_version=query_spec.schema_namespace_version,
        query_spec=query_spec,
        hypotheses=(),
        evidence=(),
        bindings=(),
        join_candidates=(),
        unresolved_items=("source-1",),
        action_history=(),
        budget_state=_budget_state(),
        result_expectations=(),
        stop_reason=stop_reason,
    )
    return ResearchLoopOutcome(
        final_state=state,
        stop_reason=stop_reason,
        affected_source_ids=("source-1",),
        citation_evidence_ids=("evidence-1",) if ambiguity is not None else (),
        ambiguity=ambiguity,
    )


def _solver_state(*, candidate_targets: tuple = ()) -> SolverState:
    query_spec = _query_spec()
    request = MissingEvidenceRequest(
        run_id=query_spec.run_id,
        run_incarnation=query_spec.run_incarnation,
        revision=1,
        schema_namespace_version=query_spec.schema_namespace_version,
        missing_evidence_request_id="request-1",
        source_id="source-1",
        question="Какой из вариантов имелся в виду?",
        candidate_targets=candidate_targets,
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="private missing evidence reason",
    )
    action = SolverAction(
        action_id="action-1",
        kind=SolverActionKind.MISSING_EVIDENCE,
        base_revision=0,
        candidate_id=None,
        missing_evidence_request_id=request.missing_evidence_request_id,
    )
    return SolverState(
        run_id=query_spec.run_id,
        run_incarnation=query_spec.run_incarnation,
        revision=1,
        schema_namespace_version=query_spec.schema_namespace_version,
        query_spec=query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(request,),
        action_history=(action,),
        selected_candidate_id=None,
        stop_reason=SolverStopReason.MISSING_EVIDENCE,
    )


# --- clarifying_questions_enabled ---------------------------------------


def test_clarifying_questions_enabled_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)
    assert clarifying_questions_enabled() is True


def test_clarifying_questions_enabled_respects_flag(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "0")
    assert clarifying_questions_enabled() is False
    monkeypatch.setenv(_ENV, "1")
    assert clarifying_questions_enabled() is True


# --- build_text_to_sql_clarifying_question ------------------------------


def test_build_returns_research_ambiguous_question() -> None:
    ambiguity = _ambiguity()
    outcome = _research_outcome(ambiguity=ambiguity)

    result = build_text_to_sql_clarifying_question(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_AMBIGUOUS",
    )

    assert result is not None
    assert result["kind"] == "research_ambiguous"
    assert ambiguity.missing_distinguishing_fact in result["question"]
    assert result["options"] == list(ambiguity.interpretations)
    assert result["evidence_ids"] == list(ambiguity.citation_evidence_ids)


def test_build_returns_missing_evidence_question() -> None:
    table = TableRef(namespace="ns", schema=None, table="orders")
    column = ColumnRef(table=table, column="status")
    solver_state = _solver_state(candidate_targets=(table, column))

    result = build_text_to_sql_clarifying_question(
        research_outcome=None,
        solver_state=solver_state,
        terminal_status="abstained",
        terminal_reason_code="SCHEMA_CLARIFICATION_REQUIRED",
    )

    assert result is not None
    assert result["kind"] == "missing_evidence"
    assert result["question"] == "Какой из вариантов имелся в виду?"
    assert result["options"] == ["orders", "orders.status"]


# --- clarification caps ---------------------------------------------------


def test_build_caps_options_to_max_clarification_options() -> None:
    interpretations = tuple(f"Толкование номер {i}." for i in range(12))
    ambiguity = AmbiguityReport(
        interpretations=interpretations,
        citation_evidence_ids=("evidence-1",),
        missing_distinguishing_fact="не указан признак различия.",
    )
    outcome = _research_outcome(ambiguity=ambiguity)

    result = build_text_to_sql_clarifying_question(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_AMBIGUOUS",
    )

    assert result is not None
    assert len(interpretations) > MAX_CLARIFICATION_OPTIONS
    assert result["options"] == list(interpretations[:MAX_CLARIFICATION_OPTIONS])
    assert len(result["options"]) == MAX_CLARIFICATION_OPTIONS


def test_build_truncates_each_long_option_to_max_chars_with_ellipsis() -> None:
    long_option = " ".join(["Толкование"] * 200)
    ambiguity = AmbiguityReport(
        interpretations=(long_option, "короткое толкование"),
        citation_evidence_ids=("evidence-1",),
        missing_distinguishing_fact="не указан признак различия.",
    )
    outcome = _research_outcome(ambiguity=ambiguity)

    result = build_text_to_sql_clarifying_question(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_AMBIGUOUS",
    )

    assert result is not None
    assert len(long_option) > MAX_CLARIFICATION_QUESTION_CHARS
    assert len(result["options"][0]) == MAX_CLARIFICATION_QUESTION_CHARS
    assert result["options"][0].endswith("…")
    assert result["options"][1] == "короткое толкование"


def test_build_truncates_long_question_to_max_chars_with_ellipsis() -> None:
    ambiguity = AmbiguityReport(
        interpretations=("Первое толкование.", "Второе толкование."),
        citation_evidence_ids=("evidence-1",),
        missing_distinguishing_fact="x" * 1000,
    )
    outcome = _research_outcome(ambiguity=ambiguity)

    result = build_text_to_sql_clarifying_question(
        research_outcome=outcome,
        solver_state=None,
        terminal_status="abstained",
        terminal_reason_code="RESEARCH_AMBIGUOUS",
    )

    assert result is not None
    question = result["question"]
    assert len(question) == MAX_CLARIFICATION_QUESTION_CHARS
    assert question.endswith("…")


@pytest.mark.parametrize(
    ("terminal_status", "terminal_reason_code", "research_outcome", "solver_state"),
    [
        ("succeeded", "", None, None),
        ("abstained", "DETERMINISTIC_CHECK_REJECTED", None, None),
        ("abstained", "RESEARCH_AMBIGUOUS", None, None),
        ("abstained", "RESEARCH_AMBIGUOUS", "not-an-outcome", None),
        ("abstained", "SCHEMA_CLARIFICATION_REQUIRED", None, None),
        ("abstained", "SCHEMA_CLARIFICATION_REQUIRED", None, "not-a-state"),
    ],
)
def test_build_returns_none_for_inapplicable_states(
    terminal_status: str,
    terminal_reason_code: str,
    research_outcome: object,
    solver_state: object,
) -> None:
    assert (
        build_text_to_sql_clarifying_question(
            research_outcome=research_outcome,
            solver_state=solver_state,
            terminal_status=terminal_status,
            terminal_reason_code=terminal_reason_code,
        )
        is None
    )


def test_build_returns_none_when_solver_stop_reason_is_not_missing_evidence() -> None:
    solver_state = _solver_state()
    solver_state = SolverState(
        run_id=solver_state.run_id,
        run_incarnation=solver_state.run_incarnation,
        revision=solver_state.revision,
        schema_namespace_version=solver_state.schema_namespace_version,
        query_spec=solver_state.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=solver_state.missing_evidence_requests,
        action_history=solver_state.action_history,
        selected_candidate_id=None,
        stop_reason=SolverStopReason.NO_SAFE_CANDIDATE,
    )

    assert (
        build_text_to_sql_clarifying_question(
            research_outcome=None,
            solver_state=solver_state,
            terminal_status="abstained",
            terminal_reason_code="SCHEMA_CLARIFICATION_REQUIRED",
        )
        is None
    )


# --- EnhancedWorkflowEngine._attach_text_to_sql_early_stop_evidence -----


def _engine() -> EnhancedWorkflowEngine:
    return object.__new__(EnhancedWorkflowEngine)


def _ambiguous_terminal_outcome() -> TextToSqlTerminalResult:
    ambiguity = _ambiguity()
    return TextToSqlTerminalResult(
        run_id="run-1",
        status=TextToSqlTerminalStatus.ABSTAINED,
        reason_code="RESEARCH_AMBIGUOUS",
        sql="",
        generated=False,
        approved=False,
        executed=False,
        dry_run=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error=None,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
        ambiguity=ambiguity,
    )


def test_attach_sets_clarification_needed_when_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    outcome = _ambiguous_terminal_outcome()
    runtime = SimpleNamespace(
        verified_research_outcome=_research_outcome(ambiguity=outcome.ambiguity),
        verified_solver_state=None,
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: runtime)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = engine._attach_text_to_sql_early_stop_evidence(
        context, outcome, {"outputs": {}}
    )

    clarification = result["outputs"]["clarification_needed"]
    assert clarification["kind"] == "research_ambiguous"
    assert clarification["options"] == list(outcome.ambiguity.interpretations)


def test_attach_omits_clarification_needed_when_flag_disabled(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "0")
    outcome = _ambiguous_terminal_outcome()
    runtime = SimpleNamespace(
        verified_research_outcome=_research_outcome(ambiguity=outcome.ambiguity),
        verified_solver_state=None,
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: runtime)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = engine._attach_text_to_sql_early_stop_evidence(
        context, outcome, {"outputs": {}}
    )

    assert "clarification_needed" not in result["outputs"]


def test_attach_omits_clarification_needed_for_unrelated_reason_code(
    monkeypatch,
) -> None:
    monkeypatch.setenv(_ENV, "1")
    outcome = TextToSqlTerminalResult(
        run_id="run-1",
        status=TextToSqlTerminalStatus.ABSTAINED,
        reason_code="DETERMINISTIC_CHECK_REJECTED",
        sql="",
        generated=False,
        approved=False,
        executed=False,
        dry_run=False,
        audited=False,
        data=[],
        columns=[],
        rows_affected=0,
        error=None,
        execution={},
        audit={},
        persistence={"status": "not_attempted"},
    )
    runtime = SimpleNamespace(
        verified_research_outcome=None,
        verified_solver_state=None,
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: runtime)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = engine._attach_text_to_sql_early_stop_evidence(
        context, outcome, {"outputs": {}}
    )

    assert "clarification_needed" not in result["outputs"]


def test_attach_omits_clarification_needed_when_runtime_is_absent(monkeypatch) -> None:
    monkeypatch.setenv(_ENV, "1")
    outcome = _ambiguous_terminal_outcome()
    engine = _engine()
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: None)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = engine._attach_text_to_sql_early_stop_evidence(
        context, outcome, {"outputs": {}}
    )

    assert "clarification_needed" not in result["outputs"]


# --- end-to-end benchmark-isolation coverage ------------------------------
#
# The "off" behaviour is wired through three independent places: the env var
# name read by ``clarifying_questions_enabled`` (this module), the key name
# written by ``canonical_runtime_environment`` (release_inputs.py), and the
# key name read back from the runtime-environment mapping in
# public_benchmark_artifacts.py. A rename in any single one of those three
# would desync silently unless a test actually applies the canonical
# environment's real env vars and drives the production code path.


def test_canonical_runtime_environment_disables_clarification_needed(
    monkeypatch,
) -> None:
    environment = canonical_runtime_environment(
        {"model_api_base": "http://model.invalid"}
    )
    assert environment[_ENV] == "0"
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    outcome = _ambiguous_terminal_outcome()
    runtime = SimpleNamespace(
        verified_research_outcome=_research_outcome(ambiguity=outcome.ambiguity),
        verified_solver_state=None,
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: runtime)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = engine._attach_text_to_sql_early_stop_evidence(
        context, outcome, {"outputs": {}}
    )

    assert "clarification_needed" not in result["outputs"]


def test_default_environment_keeps_clarification_needed(monkeypatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)

    outcome = _ambiguous_terminal_outcome()
    runtime = SimpleNamespace(
        verified_research_outcome=_research_outcome(ambiguity=outcome.ambiguity),
        verified_solver_state=None,
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_exact_typed_runtime", lambda _context: runtime)
    context = WorkflowContext(workflow_id="wf-1", session_id="session-1")

    result = engine._attach_text_to_sql_early_stop_evidence(
        context, outcome, {"outputs": {}}
    )

    assert "clarification_needed" in result["outputs"]
