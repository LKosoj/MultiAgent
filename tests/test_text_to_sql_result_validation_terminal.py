"""RED terminal boundary for post-execution result contradictions."""

from __future__ import annotations

import importlib
import json

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    CheckFailureCode,
    DerivedExpressionBinding,
    DocumentRef,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    ExpressionRef,
    PhysicalColumnBinding,
    PredicateRef,
    PredicateOperator,
    ResearchActionKind,
    ResultExpectation,
    ResultExpectationKind,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.result_validation import (
    RESULT_VALIDATION_RUNTIME_KEY,
    create_result_validation_capability,
)
from custom_tools.text_to_sql.adaptive.result_review import (
    RESULT_REVIEW_REQUIRED_RUNTIME_KEY,
    RESULT_REVIEW_RUNTIME_KEY,
    create_result_review_arbitration_capability,
    create_result_review_capability,
    evaluate_result_review_arbitration_capability,
    evaluate_result_review_capability,
    ResultReviewReceipt,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from test_text_to_sql_result_expectations import _action_and_evidence, _column, _state_for
from text_to_sql_semantic_coverage_helpers import (
    _column as _coverage_column,
    _document_evidence,
    _schema_evidence,
    _state as _coverage_state,
)
from text_to_sql_semantic_checks_helpers import (
    ItemSpec,
    POSTGRES_DSN,
    build_state,
    inner_join,
)
from tool_runtime_context import reset_tool_runtime_context, set_tool_runtime_context
from workflow.deadline import WorkflowDeadlineExceeded


SQL = "SELECT o.status FROM orders o"


def _case():
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True", "is_primary_key": False},
        },
        evidence_id="terminal-result-validation-not-null",
    )
    expectation = ResultExpectation(
        source_id="source-1",
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
        column=column,
    )
    state = _state_for(action, evidence)
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"requested_output_source_ids": ("source-1",)}
            )
        }
    )
    state = state.model_validate(
        {
            **state.model_dump(mode="python"),
            "result_expectations": (expectation,),
        }
    )
    freshness = FreshnessContext(
        evaluated_at=evidence.observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    parsed = parse_sql_candidate(SQL, POSTGRES_DSN, "terminal-candidate")
    candidate = SqlCandidate(
        candidate_id="terminal-candidate",
        sql=SQL,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    capability = create_result_validation_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
    )
    return state, requirements, candidate, capability


def _executor_result(data):
    return {
        "success": True,
        "data": data,
        "columns": ["status"],
        "rows_affected": len(data),
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": SQL,
        "applied_row_limit": 10,
    }


def _terminal_side_effects(monkeypatch, data, *, persistence_allowed):
    calls = []
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    monkeypatch.setattr(terminal, "_pre_execution_gate_allowed", lambda **_kwargs: True)

    def executor(sql_query, **_kwargs):
        assert sql_query == SQL
        calls.append("executor")
        return _executor_result(data)

    def audit(_entry):
        calls.append("audit")
        return {"status": "logged", "log_id": "terminal-audit"}

    def persist(**_kwargs):
        if not persistence_allowed:
            pytest.fail("result contradiction must not persist successful SQL")
        calls.append("persistence")
        return {"status": "saved", "filename": "query.md", "path": "/tmp/query.md"}

    monkeypatch.setattr(core, "secure_db_executor", executor)
    monkeypatch.setattr(core, "audit_logger", audit)
    monkeypatch.setattr(core, "save_successful_sql", persist)
    return calls


def _finalize(run_id):
    terminal = importlib.import_module("custom_tools.text_to_sql.core._terminal")
    return terminal.finalize_text_to_sql_run(
        SQL,
        "order status",
        POSTGRES_DSN,
        10,
        False,
        "terminal-session",
        run_id,
    )


def test_result_validation_terminal_capability_is_explicit() -> None:
    assert RESULT_VALIDATION_RUNTIME_KEY == "text_to_sql_result_validation"
    assert callable(create_result_validation_capability)


def test_terminal_returns_contradiction_receipt_without_persistence(monkeypatch) -> None:
    state, requirements, candidate, capability = _case()
    calls = _terminal_side_effects(monkeypatch, [[None]], persistence_allowed=False)
    token = set_tool_runtime_context({RESULT_VALIDATION_RUNTIME_KEY: capability})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_contradiction"
    assert result["run_id"] == state.run_id
    assert result["run_incarnation"] == state.run_incarnation
    assert result["research_state_revision"] == state.revision
    assert result["candidate_id"] == candidate.candidate_id
    assert result["normalized_ast_digest"] == candidate.normalized_ast_digest
    assert result["requirements_digest"] == requirements.requirements_digest
    assert result["finding"]["expectation"]["source_id"] == "source-1"
    assert result["finding"]["output_index"] == 0
    assert result["execution"] == _executor_result([[None]])
    assert calls == ["executor", "audit"]


def test_terminal_persists_when_result_has_no_contradiction(monkeypatch) -> None:
    state, _, _, capability = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=True)
    token = set_tool_runtime_context({RESULT_VALIDATION_RUNTIME_KEY: capability})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert calls == ["executor", "audit", "persistence"]


def test_terminal_returns_model_review_receipt_without_persistence(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    prompts: list[str] = []

    def reviewer(prompt: str) -> str:
        prompts.append(prompt)
        return '{"status":"contradicted","reason":"result conflicts with trusted evidence","source_id":"source-1"}'

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=("Return the winning alternative label, not an inner entity.",),
        model=reviewer,
    )
    token = set_tool_runtime_context(
        {
            RESULT_VALIDATION_RUNTIME_KEY: validator,
            RESULT_REVIEW_RUNTIME_KEY: review,
        }
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "contradicted"
    assert result["candidate_id"] == candidate.candidate_id
    assert result["execution"] == _executor_result([["paid"]])
    assert calls == ["executor", "audit"]
    assert len(prompts) == 1
    assert "benchmark" not in prompts[0].lower()
    assert "function" not in prompts[0].lower()
    prompt = json.loads(prompts[0])
    assert prompt["documents"] == [
        "Return the winning alternative label, not an inner entity."
    ]
    assert (
        "check the exact answer form and projection requested by the question and documents"
        in prompt["instruction"].lower()
    )
    assert (
        "status must be exactly one of consistent, contradicted, ambiguous"
        in prompt["instruction"]
    )
    assert "winning alternative label or role" in prompt["instruction"].lower()
    assert "use min and max respectively as their exact result labels" in prompt[
        "instruction"
    ].lower()
    assert "row multiplication or surprising result magnitude alone" in prompt[
        "instruction"
    ].lower()
    assert "independently establishes the required result grain" in prompt[
        "instruction"
    ].lower()
    assert "ast and data prove that the sql violates it" in prompt["instruction"].lower()
    assert (
        "complete a documented shorthand by adding a required reference input"
        in prompt["instruction"].lower()
    )


def test_result_review_prompts_for_grain_and_allows_single_observation() -> None:
    state, requirements, _, _ = _case()
    sql = (
        "SELECT o.status AS entity, o.status AS period, o.status AS metric "
        "FROM orders o ORDER BY o.status ASC LIMIT 1"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-grain-review")
    candidate = SqlCandidate(
        candidate_id="terminal-grain-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    period_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Which entity had the lowest metric over the reporting year?"
                    )
                }
            )
        }
    )
    period_requirements = validate_coverage_inputs(
        period_state,
        freshness,
        period_state.run_id,
        period_state.run_incarnation,
    )

    prompts: list[dict[str, object]] = []

    def unresolved_computation(prompt: str) -> str:
        payload = json.loads(prompt)
        prompts.append(payload)
        assert payload["question"] == "Which entity had the lowest metric over the reporting year?"
        assert not payload["ast"]["aggregates"]
        assert not payload["ast"]["groupings"]
        assert payload["evidence"]
        assert payload["documents"] == [
            "Each entity has one raw observation row for every month of the reporting year."
        ]
        assert payload["columns"] == ["entity", "period", "metric"]
        assert payload["data"] == [["entity-a", "period-1", 4]]
        instruction = payload["instruction"]
        grain_rule = "First determine the requested result grain."
        general_rule = "do not add an aggregation solely"
        if (
            "cannot be consistent" not in instruction
            or "multiple subperiod rows for each entity" not in instruction
            or grain_rule not in instruction
            or instruction.index(grain_rule) > instruction.index(general_rule)
        ):
            return json.dumps({"status": "consistent", "reason": "raw row accepted"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the SQL selects one subperiod row instead of the entity-period value",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=period_state,
        requirements=period_requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Each entity has one raw observation row for every month of the reporting year.",
        ),
        model=unresolved_computation,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=period_state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["entity-a", "period-1", 4]]),
            "columns": ["entity", "period", "metric"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert "entity-level computation over a period" in prompts[0]["instruction"]
    assert "extremal raw observation" in prompts[0]["instruction"]
    assert "cannot be consistent" in prompts[0]["instruction"]
    assert prompts[0]["instruction"].startswith("First determine the requested result grain.")

    single_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Which single entity-time record has the lowest observed metric?"
                    )
                }
            )
        }
    )
    single_requirements = validate_coverage_inputs(
        single_state,
        freshness,
        single_state.run_id,
        single_state.run_incarnation,
    )

    single_prompts: list[dict[str, object]] = []

    def single_observation(prompt: str) -> str:
        payload = json.loads(prompt)
        single_prompts.append(payload)
        assert payload["question"] == (
            "Which single entity-time record has the lowest observed metric?"
        )
        assert not payload["ast"]["aggregates"]
        assert not payload["ast"]["groupings"]
        assert payload["documents"] == [
            "Each entity has one raw observation row for every month of the reporting year."
        ]
        if "single record/entity-time extremum may be consistent" not in payload["instruction"]:
            return json.dumps(
                {
                    "status": "ambiguous",
                    "reason": "raw observation is not allowed",
                    "source_id": "source-1",
                }
            )
        return json.dumps({"status": "consistent", "reason": "one row is requested"})

    single_review = create_result_review_capability(
        state=single_state,
        requirements=single_requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Each entity has one raw observation row for every month of the reporting year.",
        ),
        model=single_observation,
    )
    single_receipt = evaluate_result_review_capability(
        single_review,
        expected_run_id=single_state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["entity-a", "period-1", 4]]),
            "columns": ["entity", "period", "metric"],
            "sql_query": sql,
        },
    )

    assert single_receipt.verdict == "consistent"
    assert "single record/entity-time extremum may be consistent" in single_prompts[0]["instruction"]


def test_result_review_rejects_ratio_multiplied_by_one_to_many_join() -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "What percentage of unique accounts have the requested status?"
                    )
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT CAST(SUM(CASE WHEN o.status = 'active' THEN 1 ELSE 0 END) AS REAL) "
        "/ COUNT(*) FROM orders o JOIN orders event ON event.status = o.status"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-ratio-grain-review")
    candidate = SqlCandidate(
        candidate_id="terminal-ratio-grain-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        generic_start = instruction.index("for a ratio or percentage over entities")
        generic_end = instruction.index(
            "treat the entity population explicitly named", generic_start
        )
        generic_ratio_rule = instruction[generic_start:generic_end]
        exact_formula_exception = (
            "apply the following deduplication rule unless all of these facts hold: a required "
            "formula exists, a trusted document explicitly specifies the exact operation and "
            "counting unit, and the ast follows it"
        )
        if not all(
            clause in instruction
            for clause in (
                "ratio or percentage over entities",
                "one-to-many join",
                "same entity identity in both numerator and denominator",
                "trusted schema or evidence confirms",
                "alternative endpoint rows",
                "entity-relationship pair once",
            )
        ) or exact_formula_exception not in generic_ratio_rule:
            return json.dumps(
                {"status": "consistent", "reason": "the aggregate executed successfully"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the join counts repeated account rows instead of unique accounts",
                "source_id": "source-1",
                "row_grain_requirement": "deduplicate_entity",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Accounts are identified by account_id; one account may have several events.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[0.5]]),
            "columns": ["percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert receipt.row_grain_requirement == "deduplicate_entity"


@pytest.mark.parametrize(
    ("payload", "normalizes"),
    (
        (
            {
                "status": "contradicted",
                "reason": "preserve the qualifying rows",
                "source_id": "source-1",
                "repair_kind": "preserve_qualifying_rows",
                "row_grain_requirement": "preserve_qualifying_rows",
                "repair_binding_id": None,
            },
            True,
        ),
        (
            {
                "status": "contradicted",
                "reason": "conflicting legacy row grain",
                "source_id": "source-1",
                "repair_kind": "preserve_qualifying_rows",
                "row_grain_requirement": "deduplicate_entity",
                "repair_binding_id": None,
            },
            False,
        ),
        (
            {
                "status": "contradicted",
                "reason": "legacy row grain cannot select a binding",
                "source_id": "source-1",
                "repair_kind": "preserve_qualifying_rows",
                "row_grain_requirement": None,
                "repair_binding_id": "binding-1",
            },
            False,
        ),
        (
            {
                "status": "consistent",
                "reason": "consistent result cannot carry row grain",
                "source_id": None,
                "repair_kind": "preserve_qualifying_rows",
                "row_grain_requirement": None,
                "repair_binding_id": None,
            },
            False,
        ),
        (
            {
                "status": "contradicted",
                "reason": "unknown repair kind",
                "source_id": "source-1",
                "repair_kind": "other_repair",
                "row_grain_requirement": None,
                "repair_binding_id": None,
            },
            False,
        ),
    ),
    ids=(
        "matching_legacy_row_grain",
        "conflicting_row_grain",
        "binding_repair",
        "consistent_cannot_carry_row_grain",
        "other_kind",
    ),
)
def test_result_review_normalizes_only_compatible_legacy_row_grain_repair_kind(
    payload, normalizes
) -> None:
    result_review = importlib.import_module(
        "custom_tools.text_to_sql.adaptive.result_review"
    )

    if not normalizes:
        with pytest.raises(ValueError):
            result_review._parse_response(json.dumps(payload))
        return

    response = result_review._parse_response(json.dumps(payload))

    assert response.repair_kind is None
    assert response.row_grain_requirement == "preserve_qualifying_rows"
    assert response.repair_binding_id is None


@pytest.mark.parametrize(
    ("has_exact_document", "ast_follows", "expected_verdict"),
    (
        (True, True, "consistent"),
        (False, True, "contradicted"),
        (True, False, "contradicted"),
    ),
    ids=("all_formula_facts", "formula_without_exact_document", "formula_document_ast_mismatch"),
)
def test_result_review_applies_ratio_dedup_exception_only_when_all_formula_facts_hold(
    has_exact_document: bool,
    ast_follows: bool,
    expected_verdict: str,
) -> None:
    state, _, _, _ = _case()
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "eligible event ratio",
            "normalized_meaning": "SUM(CASE eligible event)/SUM(CASE eligible event)",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the documented eligible event ratio.",
                    "semantic_items": (formula,),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    sql = (
        "SELECT SUM(CASE WHEN e.status = 'eligible' THEN 1 ELSE 0 END) / "
        "SUM(CASE WHEN e.status = 'eligible' THEN 1 ELSE 0 END) "
        "FROM orders e JOIN orders v ON v.status = e.status"
        if ast_follows
        else "SELECT COUNT(*) / COUNT(*) FROM orders e JOIN orders v ON v.status = e.status"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-exact-ratio-formula")
    candidate = SqlCandidate(
        candidate_id="terminal-exact-ratio-formula",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"].lower()
        generic_start = instruction.index("for a ratio or percentage over entities")
        generic_end = instruction.index(
            "treat the entity population explicitly named", generic_start
        )
        generic_ratio_rule = instruction[generic_start:generic_end]
        exact_formula_exception = (
            "apply the following deduplication rule unless all of these facts hold: a required "
            "formula exists, a trusted document explicitly specifies the exact operation and "
            "counting unit, and the ast follows it"
        )
        exact_document = "sum(case eligible event)/sum(case eligible event)" in str(
            payload["documents"]
        ).lower()
        ast_follows_document = (
            "sum(case when e.status = 'eligible' then 1 else 0 end) / "
            "sum(case when e.status = 'eligible' then 1 else 0 end)"
            in payload["sql"].lower()
        )
        if (
            exact_formula_exception not in generic_ratio_rule
            or payload["query_spec"]["semantic_items"][0]["kind"] != "formula"
            or not payload["ast"]["aggregates"]
        ):
            return json.dumps(
                {"status": "contradicted", "reason": "formula was replaced", "source_id": "source-1"}
            )
        if exact_document and ast_follows_document:
            return json.dumps({"status": "consistent", "reason": "exact formula is preserved"})
        return json.dumps(
            {"status": "contradicted", "reason": "generic deduplication applies", "source_id": "source-1"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Compute exactly SUM(CASE eligible event)/SUM(CASE eligible event); each term counts event rows."
            if has_exact_document
            else "Event records may repeat.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[0.5]]),
            "columns": ["ratio"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == expected_verdict


@pytest.mark.parametrize(
    (
        "semantic_kind",
        "has_exact_operation",
        "has_exact_document",
        "has_counting_unit",
        "ast_follows",
        "expected_verdict",
    ),
    (
        (SemanticItemKind.METRIC, True, True, True, True, "consistent"),
        (SemanticItemKind.FORMULA, True, True, True, True, "consistent"),
        (SemanticItemKind.METRIC, False, True, True, True, "contradicted"),
        (SemanticItemKind.METRIC, True, False, True, True, "contradicted"),
        (SemanticItemKind.METRIC, True, True, False, True, "contradicted"),
        (SemanticItemKind.METRIC, True, True, True, False, "contradicted"),
    ),
    ids=(
        "metric_exact_formula",
        "formula_exact_formula",
        "metric_without_exact_operation",
        "metric_without_exact_document",
        "metric_without_counting_unit",
        "metric_ast_mismatch",
    ),
)
def test_result_review_applies_exact_aggregate_multiset_authority(
    semantic_kind: SemanticItemKind,
    has_exact_operation: bool,
    has_exact_document: bool,
    has_counting_unit: bool,
    ast_follows: bool,
    expected_verdict: str,
) -> None:
    state, _, _, _ = _case()
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": semantic_kind,
            "source_text": "qualifying joined-row aggregate",
            "normalized_meaning": (
                "SUM(amount)/COUNT(record_id)"
                if has_exact_operation
                else "aggregate recorded amount"
            ),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the documented qualifying joined-row aggregate.",
                    "semantic_items": (formula,),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    exact_sql = (
        "SELECT SUM(e.amount) / COUNT(e.record_id) "
        "FROM orders e JOIN orders related ON related.status = e.status"
    )
    sql = (
        exact_sql
        if ast_follows
        else "SELECT AVG(e.amount) FROM orders e JOIN orders related ON related.status = e.status"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-exact-aggregate-multiset")
    candidate = SqlCandidate(
        candidate_id="terminal-exact-aggregate-multiset",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"].lower()
        required_exact_item = (
            payload["query_spec"]["semantic_items"][0]["kind"] in {"metric", "formula"}
            and payload["query_spec"]["semantic_items"][0]["normalized_meaning"]
            == "SUM(amount)/COUNT(record_id)"
        )
        ast_follows_exact_formula = {
            aggregate["function"] for aggregate in payload["ast"]["aggregates"]
        } == {"sum", "count"} and not any(
            aggregate["distinct"] for aggregate in payload["ast"]["aggregates"]
        )
        if (
            "a required metric or formula and a trusted document explicitly specify the exact "
            "avg or sum/count formula and its counting unit" not in instruction
            or
            "explicitly specify the exact avg or sum/count formula and its counting unit"
            not in instruction
            or "preserve the qualifying join-row multiset" not in instruction
            or "does not authorize distinct, a unique subquery, or exists" not in instruction
            or "sum(amount)/count(record_id)" not in str(payload["documents"]).lower()
            or "each qualifying joined row is one record unit" not in str(payload["documents"]).lower()
            or not required_exact_item
            or not ast_follows_exact_formula
            or "sum(e.amount) / count(e.record_id)" not in payload["sql"].lower()
            or "distinct" in payload["sql"].lower()
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the formula must count each entity once",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "exact row multiset is preserved"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            (
                "Compute exactly SUM(amount)/COUNT(record_id); "
                "each qualifying joined row is one record unit."
                if has_exact_document and has_counting_unit
                else (
                    "Compute exactly SUM(amount)/COUNT(record_id)."
                    if has_exact_document
                    else "Aggregate qualifying records."
                )
            ),
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[2.0]]),
            "columns": ["aggregate"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == expected_verdict


def test_result_review_keeps_grain_null_for_exact_aggregate_scope_contradiction() -> None:
    state, _, _, _ = _case()
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "qualifying record proportion",
            "normalized_meaning": "DIVIDE(SUM(amount WHERE qualifying), COUNT(record_id))",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the documented qualifying record proportion.",
                    "semantic_items": (formula,),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    sql = (
        "SELECT SUM(e.amount) / COUNT(e.record_id) "
        "FROM orders e JOIN orders related ON related.status = e.status"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-exact-scope")
    candidate = SqlCandidate(
        candidate_id="terminal-exact-scope",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"].lower()
        required_rule = (
            "where a required formula and trusted document explicitly specify aggregate "
            "operations and arguments, you may return contradicted for an incorrect scope "
            "or operation order, but row_grain_requirement must remain null unless the "
            "question, queryspec, or exact formula explicitly requires unique, distinct, "
            "or entity-once counting"
        )
        if (
            required_rule not in instruction
            or payload["query_spec"]["semantic_items"][0]["kind"] != "formula"
            or "divide(sum(amount where qualifying), count(record_id))"
            not in str(payload["documents"]).lower()
            or {item["function"] for item in payload["ast"]["aggregates"]}
            != {"sum", "count"}
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "join multiplicity requires distinct records",
                    "source_id": "source-1",
                    "row_grain_requirement": "deduplicate_entity",
                }
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the documented qualifying condition is absent from the sum",
                "source_id": "source-1",
                "row_grain_requirement": None,
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Compute DIVIDE(SUM(amount WHERE qualifying), COUNT(record_id)); "
            "each qualifying joined row is one record unit.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[2.0]]),
            "columns": ["proportion"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert receipt.row_grain_requirement is None


def test_result_review_rejects_related_rows_as_named_entity_population() -> None:
    account_event_join = inner_join("accounts", "id", "status_events", "account_id")
    state = build_state(
        (
            ItemSpec(
                source_id="account-population",
                kind=SemanticItemKind.DIMENSION,
                table="accounts",
                column="id",
            ),
            ItemSpec(
                source_id="active-status",
                kind=SemanticItemKind.FILTER,
                table="status_events",
                column="status",
                operator=PredicateOperator.EQ,
                literal="active",
                join_path=(account_event_join,),
            ),
        ),
        shape=ExpectedResultShape.SCALAR,
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "What percentage of all accounts have an active status event?"
                    )
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT CAST(SUM(CASE WHEN e.status = 'active' THEN 1 ELSE 0 END) AS REAL) "
        "/ COUNT(*) FROM status_events e"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-related-row-ratio")
    candidate = SqlCandidate(
        candidate_id="terminal-related-row-ratio",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split())
        has_rule = all(
            clause in instruction
            for clause in (
                "entity population explicitly named by the question",
                "table that stores a qualifying attribute",
                "relationship back to the named base entity",
            )
        )
        has_population_binding = any(
            binding["source_id"] == "account-population"
            and binding["physical_column"]["table"]["table"] == "accounts"
            for binding in payload["bindings"]
        )
        has_related_filter = any(
            binding["source_id"] == "active-status" and binding["join_path"]
            for binding in payload["bindings"]
        )
        sql_uses_only_related_rows = (
            "status_events" in payload["sql"] and "accounts" not in payload["sql"]
        )
        if not (
            has_rule
            and has_population_binding
            and has_related_filter
            and sql_uses_only_related_rows
        ):
            return json.dumps(
                {"status": "consistent", "reason": "the percentage executed"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the SQL counts status-event rows instead of all accounts",
                "source_id": "account-population",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        schema={
            "accounts": "one row per account",
            "status_events": "status events related to accounts",
        },
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[0.5]]),
            "columns": ["percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "account-population"


def test_result_review_does_not_require_both_alternative_endpoints_for_one_output() -> None:
    join_path = (inner_join("items", "id", "relations", "left_id"),)
    state = build_state(
        (
            ItemSpec(
                source_id="shared-category",
                kind=SemanticItemKind.DIMENSION,
                table="items",
                column="category",
                join_path=join_path,
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List the shared categories of the selected relationships.",
                    "requested_output_source_ids": ("shared-category",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT DISTINCT i.category FROM items i "
        "INNER JOIN relations r ON i.id = r.left_id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-alternative-endpoint")
    candidate = SqlCandidate(
        candidate_id="terminal-alternative-endpoint",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"].lower()
        relationship_columns = payload["schema"]["main.relations"]["columns"]
        if not all(
            clause in instruction
            for clause in (
                "trusted schema or evidence confirms",
                "alternative endpoints are directional representations",
                "one confirmed endpoint is sufficient",
                "one requested shared attribute",
                "do not require the other endpoint to be joined or projected",
                "explicitly request endpoint-specific or both-role output",
            )
        ) or not (
            payload["query_spec"]["requested_output_source_ids"]
            == ["shared-category"]
            and payload["columns"] == ["category"]
            and "r.left_id" in payload["sql"]
            and "r.right_id" not in payload["sql"]
            and set(relationship_columns) == {"left_id", "right_id"}
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the other endpoint was not projected",
                    "source_id": "shared-category",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the requested output is complete"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "The relationship exposes left_id and right_id as alternative directional "
            "endpoints for the same shared attribute.",
        ),
        model=reviewer,
        schema={
            "main.items": {"columns": {"id": {}, "category": {}}},
            "main.relations": {"columns": {"left_id": {}, "right_id": {}}},
        },
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["retail"]]),
            "columns": ["category"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


def test_result_review_rejects_counted_entity_multiplied_by_join() -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "How many accounts have the requested event?"}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT COUNT(a.account_id) FROM accounts a "
        "JOIN account_events e ON e.account_id = a.account_id "
        "WHERE e.event_type = 'requested'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-count-grain-review")
    candidate = SqlCandidate(
        candidate_id="terminal-count-grain-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        if not all(
            clause in instruction
            for clause in (
                "count of base entities",
                "one-to-many join repeats an entity",
                "count each entity identity once",
            )
        ):
            return json.dumps(
                {"status": "consistent", "reason": "the aggregate executed successfully"}
            )
        return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the join counts each event row instead of each account",
                    "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "account_id identifies an account; one account may have multiple events.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[6]]),
            "columns": ["account_count"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"


def test_result_review_allows_explicit_count_of_detail_rows() -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "How many matching event rows are there?"}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT COUNT(e.event_id) FROM accounts a "
        "JOIN account_events e ON e.account_id = a.account_id "
        "WHERE e.event_type = 'requested'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-detail-count-review")
    candidate = SqlCandidate(
        candidate_id="terminal-detail-count-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        if "question requests joined or detail rows" not in instruction:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "detail rows were incorrectly deduplicated",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the requested detail rows are counted"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("Each event_id identifies one requested detail row.",),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[6]]),
            "columns": ["event_count"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None


def test_result_review_does_not_invent_aggregation_for_scalar_answer() -> None:
    state, _, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "For this entity, is the condition true?",
                    "expected_result_shape": ExpectedResultShape.SCALAR,
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    parsed = parse_sql_candidate(SQL, POSTGRES_DSN, "terminal-scalar-row-scope-review")
    candidate = SqlCandidate(
        candidate_id="terminal-scalar-row-scope-review",
        sql=SQL,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    prompts: list[dict[str, object]] = []

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        prompts.append(payload)
        instruction = payload["instruction"]
        final_cardinality_rule = (
            "final mandatory cardinality rule: expected_result_shape never constrains "
            "the number of returned rows. never return contradicted or ambiguous merely "
            "because execution returned multiple rows; when no independent trusted conflict "
            "exists, return consistent."
        )
        if (
            "scalar or yes/no answer form alone does not prove a single-row result"
            not in instruction
            or "does not authorize aggregation" not in instruction
            or "multiple returned rows do not by themselves make the answer ambiguous"
            not in instruction.lower()
            or "singular grammar does not require a tie-break, limit or one-row result"
            not in instruction.lower()
            or "the sql follows all required bindings" not in instruction.lower()
            or "the question and documents specify no tie-break or limit"
            not in instruction.lower()
            or "preserve all matches" not in instruction.lower()
                or final_cardinality_rule not in instruction.lower()
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "multiple rows require aggregation",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the original row scope is preserved"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"], ["closed"]]),
    )

    assert receipt.verdict == "consistent"
    assert len(prompts) == 1


def test_result_review_does_not_infer_period_from_historical_storage() -> None:
    state, requirements, candidate, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the status of the order with the greatest identifier."
                }
            )
        }
    )
    sql = "SELECT o.status FROM orders o ORDER BY o.id DESC LIMIT 1"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, candidate.candidate_id)
    candidate = candidate.model_copy(
        update={"sql": sql, "normalized_ast_digest": parsed.candidate_digest}
    )

    prompts: list[str] = []

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        prompts.append(instruction)
        historical_context_is_present = (
            "historical" in payload["schema"]["main.orders"]["description"]
            and payload["data"] == [["open"], ["closed"]]
            and "period" not in payload["question"].lower()
            and "snapshot" not in payload["question"].lower()
            and "aggregate" not in payload["question"].lower()
            and "ORDER BY o.id DESC LIMIT 1" in payload["sql"]
        )
        if historical_context_is_present and not all(
            clause in instruction
            for clause in (
                "Do not infer a period or aggregation solely because the selected attribute is stored in multiple historical rows.",
                "When the question specifies no period, snapshot, or aggregation",
                "preserve its requested ordering and limit",
            )
        ):
            return json.dumps(
                {
                    "status": "ambiguous",
                    "reason": "historical rows require an unspecified aggregate",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "no temporal computation was requested"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        schema={
            "main.orders": {
                "description": "Historical order observations; one order may have multiple rows.",
                "columns": {
                    "id": {"description": "Order identifier"},
                    "status": {"description": "Status recorded for this observation"},
                },
            }
        },
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["open"], ["closed"]]),
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"
    assert len(prompts) == 1


def test_result_review_does_not_treat_aggregate_silence_as_formula_conflict() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-total",)
    )
    entity_item, total_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the total qualifying amount.",
                    "semantic_items": (
                        entity_item,
                        total_item.model_copy(
                            update={
                                "kind": SemanticItemKind.FORMULA,
                                "source_text": "qualifying amount formula",
                                "normalized_meaning": "qualifying amount",
                            }
                        ),
                    ),
                }
            )
        }
    )
    sql = "SELECT SUM(i.amount) AS total FROM items i"
    required_rule = (
        "Silence about a named aggregate in a trusted formula is not positive "
        "contradiction to an aggregate already used by an otherwise correct required FORMULA."
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if (
            required_rule not in payload["instruction"]
            or {aggregate["function"] for aggregate in payload["ast"]["aggregates"]}
            != {"sum"}
            or payload["documents"] != ["Qualifying entries contribute their amount."]
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the unnamed aggregate is forbidden",
                    "source_id": "projection-total",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "aggregate silence is not a positive conflict",
            }
        )

    receipt = _projection_review(
        state,
        sql,
        reviewer,
        documents=("Qualifying entries contribute their amount.",),
        execution_data=[[12]],
        execution_columns=["total"],
    )

    assert receipt.verdict == "consistent"


def test_result_review_distinguishes_tied_winners_from_all_ranked_groups() -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "Which region has the highest total sales?"}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT o.status FROM orders o "
        "GROUP BY o.status ORDER BY SUM(o.amount) DESC"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-group-extremum-review")
    candidate = SqlCandidate(
        candidate_id="terminal-group-extremum-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        if (
            "return every group tied at the requested extreme" not in instruction
            or "exclude groups whose aggregate is not at that extreme" not in instruction
            or "does not by itself require limit 1" not in instruction
        ):
            return json.dumps(
                {"status": "consistent", "reason": "all groups are ordered"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the query includes groups below the requested maximum",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution=_executor_result([["north"], ["south"], ["west"]]),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"


def test_result_review_rejects_aggregate_for_dimension_only_output() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity",)
    )
    sql = "SELECT MAX(i.id) AS id FROM items i"

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            payload["ast"]["aggregates"]
            and "requested outputs are only DIMENSION items" in instruction
            and "aggregate projection or GROUP BY" in instruction
            and "Use root DISTINCT only when the question or QuerySpec explicitly requests "
            "unique or distinct, or trusted evidence proves the entire root projection is "
            "one-to-one at the required result grain, for example because the projected entity "
            "identity is unique; otherwise preserve all qualifying rows" in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the requested label does not require aggregation",
                    "source_id": "projection-entity",
                }
            )
        return json.dumps({"status": "consistent", "reason": "aggregate accepted"})

    receipt = _projection_review(
        state,
        sql,
        reviewer,
        execution_data=[[1]],
        execution_columns=["id"],
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "projection-entity"
    assert receipt.deterministic_failure_code is None


def test_result_review_rejects_aggregate_for_dimension_output_with_filter_formula() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity",)
    )
    entity_item, condition_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        entity_item,
                        condition_item.model_copy(
                            update={
                                "kind": SemanticItemKind.FORMULA,
                                "source_text": "label condition",
                                "normalized_meaning": "label satisfies its condition",
                            }
                        ),
                    )
                }
            )
        }
    )
    sql = "SELECT MAX(i.id) AS id FROM items i WHERE i.amount > 0"

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            payload["ast"]["aggregates"]
            and "explicitly requires that aggregate projection or GROUP BY" in instruction
            and "FORMULA used only as a filter or condition does not authorize root "
            "aggregation or grouping" in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the filtering condition does not require aggregation",
                    "source_id": "projection-entity",
                }
            )
        return json.dumps({"status": "consistent", "reason": "aggregate accepted"})

    receipt = _projection_review(
        state,
        sql,
        reviewer,
        execution_data=[[1]],
        execution_columns=["id"],
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "projection-entity"
    assert receipt.deterministic_failure_code is None


def test_result_review_preserves_document_defined_row_role() -> None:
    state, requirements, candidate, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        required_clauses = (
            "trusted document defines a row role through a physical representation",
            "do not replace it with a conventional domain interpretation",
            "do not add an aggregation solely to force those matches into one row",
        )
        if (
            not all(clause in instruction for clause in required_clauses)
            or instruction.index(required_clauses[0]) > instruction.index("use only this trusted context")
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the role should use a conventional domain interpretation",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the documented physical role and original row scope are preserved",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(
            "A designated record is identified by a signed storage token.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"], ["closed"]]),
    )

    assert receipt.verdict == "consistent"


@pytest.mark.parametrize(
    ("documents", "expected_verdict"),
    (
        ((), "consistent"),
        (("A registered member is exactly a row whose status equals active.",), "contradicted"),
    ),
)
def test_result_review_does_not_invent_discriminator_from_role_name(
    documents: tuple[str, ...],
    expected_verdict: str,
) -> None:
    state, requirements, candidate, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "List registered members."}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"].lower()
        assert payload["question"] == "List registered members."
        required_clauses = (
            "entity or relationship role name alone does not authorize",
            "exact discriminator predicate",
            "question, a trusted document, or an already selected binding explicitly requires",
        )
        if not all(clause in instruction for clause in required_clauses):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the role name implies an additional discriminator filter",
                    "source_id": "source-1",
                }
            )
        if payload["documents"]:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the trusted document explicitly requires the exact status predicate",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "no exact discriminator predicate is explicitly required",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=documents,
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"]]),
    )

    assert receipt.verdict == expected_verdict


@pytest.mark.parametrize(
    ("document", "sql", "expected_verdict"),
    (
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            "consistent",
        ),
        (
            "The exact physical predicate requires state_code equals 'stored-disabled'.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            "contradicted",
        ),
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-disabled'",
            "contradicted",
        ),
    ),
)
def test_result_review_keeps_confirmed_discriminator_representation_despite_alias(
    document: str, sql: str, expected_verdict: str
) -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="record-status",
                kind=SemanticItemKind.FILTER,
                table="records",
                column="state_code",
                operator=PredicateOperator.EQ,
                literal="stored-enabled",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "List enabled records."}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-confirmed-discriminator")
    candidate = SqlCandidate(
        candidate_id="terminal-confirmed-discriminator",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        binding = next(
            item
            for item in payload["bindings"]
            if item["source_id"] == "record-status"
        )
        binding_is_exact = (
            binding["kind"] == "discriminator_value"
            and binding["status"] == "supported"
            and binding["discriminator_predicate"]["right"] == "stored-enabled"
        )
        ast = json.dumps(payload["ast"], sort_keys=True)
        ast_follows = (
            '"kind": "eq"' in ast
            and '"name", "state_code"' in ast
            and '"value", "stored-enabled"' in ast
        )
        instruction = payload["instruction"].lower()
        required_rule = (
            "a selected supported discriminator_value with an exact physical predicate that "
            "the ast follows confirms the stored physical representation; conceptual or "
            "document aliases alone do not contradict it."
        )
        explicit_predicate = "requires state_code equals 'stored-disabled'" in str(
            payload["documents"]
        ).lower()
        exception_rule = (
            "only an explicit queryspec or document exact physical predicate naming a "
            "physical column, operator, and stored literal"
        )
        if not binding_is_exact or required_rule not in instruction:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the document alias must replace the stored representation",
                    "source_id": "record-status",
                }
            )
        if not ast_follows:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the AST does not follow the confirmed physical predicate",
                    "source_id": "record-status",
                }
            )
        if explicit_predicate:
            if exception_rule not in instruction:
                return json.dumps(
                    {
                        "status": "consistent",
                        "reason": "the explicit physical predicate was ignored",
                    }
                )
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the exact physical predicate differs",
                    "source_id": "record-status",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the confirmed physical representation is preserved",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(document,),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={**_executor_result([["stored-enabled"]]), "sql_query": sql},
    )

    assert receipt.verdict == expected_verdict


def test_result_review_does_not_let_shape_hint_override_documented_row_scope() -> None:
    state, _, candidate, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"expected_result_shape": ExpectedResultShape.SCALAR}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        required_clauses = (
            "treat that definition as the qualifying row scope",
            "do not infer an entity or period grain",
            "expected_result_shape is only an answer-format hint",
            "singular grammar does not require a tie-break, limit or one-row result",
            "preserve all matches unless the question or trusted context explicitly requires",
        )
        if (
            not all(clause in instruction for clause in required_clauses)
            or any(
                instruction.index(clause) > instruction.index("use only this trusted context")
                for clause in required_clauses
            )
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the scalar shape hint requires an entity-level aggregate",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the documented row scope is preserved without an invented aggregate",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=("A selected record is defined by an encoded storage pattern.",),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"], ["closed"]]),
    )

    assert receipt.verdict == "consistent"


def test_result_review_does_not_infer_entity_grain_from_display_attribute() -> None:
    state, requirements, _, _ = _case()
    sql = (
        "SELECT o.status FROM orders o "
        "GROUP BY o.status HAVING COUNT(*) > 2"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-display-grain-review")
    candidate = SqlCandidate(
        candidate_id="terminal-display-grain-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )

    prompts: list[str] = []

    def review_display_grain(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        prompts.append(instruction)
        if not all(
            clause in instruction
            for clause in (
                "display attribute is not proof of the entity grain",
                "return consistent only when trusted context",
                "return contradicted when trusted context",
                "otherwise return ambiguous",
            )
        ):
            return json.dumps({"status": "consistent", "reason": "label treated as entity"})
        return json.dumps(
            {
                "status": "ambiguous",
                "reason": "the display attribute is not proven unique per entity",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=review_display_grain,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["active"]]),
            "columns": ["status"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "ambiguous"
    assert receipt.source_id == "source-1"
    assert len(prompts) == 1


def test_result_review_rejects_computation_that_differs_from_document() -> None:
    state, requirements, candidate, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )

    def review_formula(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        required_clauses = (
            "explicitly specifies the exact computation",
            "adds, removes or reorders an aggregation",
            "do not request more schema or data evidence",
            "must be null when the selected physical bindings are correct and only the computation differs",
            "target one supplied input binding used by that formula",
        )
        if not all(clause in instruction for clause in required_clauses):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the documented computation differs",
                    "source_id": "source-1",
                    "repair_kind": "semantic_binding_mismatch",
                }
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the SQL omits the documented aggregation",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(
            "The required computation is MAX(status), not the direct status value.",
        ),
        model=review_formula,
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["active"]]),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.repair_kind is None


def test_result_review_preserves_computation_explicitly_required_by_document() -> None:
    state, requirements, _, _ = _case()
    sql = "SELECT COUNT(o.status) AS status FROM orders o"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "document-formula-candidate")
    candidate = SqlCandidate(
        candidate_id="document-formula-candidate",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )

    def review_formula(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        required_clauses = (
            "exactly follows a computation explicitly specified by a trusted document",
            "must not replace that computation with an inferred business interpretation",
        )
        if not all(clause in instruction for clause in required_clauses):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "a different aggregation seems more natural",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the SQL preserves the documented computation",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("Count the qualifying observations exactly as written.",),
        model=review_formula,
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={**_executor_result([[1]]), "sql_query": sql},
    )

    assert receipt.verdict == "consistent"


def test_result_review_requires_grouping_dimension_in_its_semantic_role() -> None:
    period = _coverage_column("usage", "period")
    amount = _coverage_column("usage", "amount")
    period_evidence = _schema_evidence("period-evidence", period)
    amount_evidence = _schema_evidence("amount-evidence", amount)
    formula_rule = "Monthly usage is the sum of raw observations in each calendar month."
    formula_evidence = _document_evidence(
        "monthly-formula-evidence", content=formula_rule
    )
    period_binding = PhysicalColumnBinding(
        binding_id="period-binding",
        source_id="period-source",
        tables=(period.table,),
        columns=(period,),
        predicates=(),
        join_path=(),
        evidence_ids=(period_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=period,
    )
    amount_binding = PhysicalColumnBinding(
        binding_id="amount-binding",
        source_id="amount-source",
        tables=(amount.table,),
        columns=(amount,),
        predicates=(),
        join_path=(),
        evidence_ids=(amount_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=amount,
    )
    formula_binding = DerivedExpressionBinding(
        binding_id="monthly-formula-binding",
        source_id="monthly-formula-source",
        tables=(period.table,),
        columns=(period, amount),
        predicates=(),
        join_path=(),
        evidence_ids=(
            period_evidence.evidence_id,
            amount_evidence.evidence_id,
            formula_evidence.evidence_id,
        ),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        expression=ExpressionRef(
            expression_id="monthly-formula-expression",
            expression="MAX(amount) over raw observations",
        ),
        rule_excerpt=formula_rule,
        input_columns=(period, amount),
    )
    state = _coverage_state(
        item_specs=(
            (
                "period-source",
                True,
                SemanticItemStatus.RESOLVED,
                (period_binding.binding_id,),
            ),
            (
                "amount-source",
                True,
                SemanticItemStatus.RESOLVED,
                (amount_binding.binding_id,),
            ),
        ),
        bindings=(period_binding, amount_binding, formula_binding),
        evidence=(period_evidence, amount_evidence, formula_evidence),
    )
    semantic_items = (
        state.query_spec.semantic_items[0].model_copy(
            update={
                "kind": SemanticItemKind.DIMENSION,
                "source_text": "monthly grouping",
                "normalized_meaning": "group usage by calendar month",
            }
        ),
        state.query_spec.semantic_items[1].model_copy(
            update={
                "kind": SemanticItemKind.METRIC,
                "source_text": "highest monthly usage",
                "normalized_meaning": "highest total usage among calendar months",
            }
        ),
        SemanticItem(
            source_id="monthly-formula-source",
            kind=SemanticItemKind.FORMULA,
            source_text="highest monthly usage",
            normalized_meaning="maximum of totals computed for each calendar month",
            required=True,
            operator=None,
            literal_or_reference=None,
            status=SemanticItemStatus.RESOLVED,
            binding_ids=(formula_binding.binding_id,),
        ),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "What was the highest monthly usage in 2024?",
                    "semantic_items": semantic_items,
                    "requested_output_source_ids": ("monthly-formula-source",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    sql = (
        "SELECT MAX(u.amount) AS highest_monthly_usage FROM usage u "
        "WHERE SUBSTRING(u.period, 1, 4) = '2024'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "missing-month-grouping")
    assert parsed.aggregates
    assert not parsed.groupings
    candidate = SqlCandidate(
        candidate_id="missing-month-grouping",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        assert any(
            item.get("kind") == "derived_expression"
            and item["expression"]["expression"] == "MAX(amount) over raw observations"
            for item in payload["bindings"]
        )
        if (
            "required grouping dimension" not in payload["instruction"]
            or "Do not infer that a metric is already aggregated" not in payload["instruction"]
            or "derived binding is a model hypothesis copied" not in payload["instruction"]
        ):
            return json.dumps({"status": "consistent", "reason": "columns are present"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the required monthly grouping is absent",
                "source_id": "period-source",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("Each row is usage for one account in one calendar month.",),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[900]]),
            "columns": ["highest_monthly_usage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "period-source"


def test_result_review_checks_nested_computation_order() -> None:
    state, requirements, _, _ = _case()
    sql = (
        "WITH per_entity AS ("
        "SELECT o.status AS entity, SUM(LENGTH(o.status)) AS total_length "
        "FROM orders o GROUP BY o.status"
        ") SELECT MIN(total_length) AS answer FROM per_entity"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-operation-order-review")
    candidate = SqlCandidate(
        candidate_id="terminal-operation-order-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    ordered_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "What is the average length among records with the smallest length?"
                    )
                }
            )
        }
    )
    ordered_requirements = validate_coverage_inputs(
        ordered_state,
        freshness,
        ordered_state.run_id,
        ordered_state.run_incarnation,
    )

    prompts: list[dict[str, object]] = []

    def operation_order_review(prompt: str) -> str:
        payload = json.loads(prompt)
        prompts.append(payload)
        assert payload["question"] == (
            "What is the average length among records with the smallest length?"
        )
        assert len(payload["ast"]["aggregates"]) >= 2
        assert payload["documents"] == [
            "First select records at the minimum length, then average those records."
        ]
        if (
            "order and scope of nested operations may change their meaning"
            not in payload["instruction"]
            or "only when trusted context proves the mismatch"
            not in payload["instruction"]
        ):
            return json.dumps(
                {"status": "consistent", "reason": "the same operations are present"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the candidate sums per entity before selecting the minimum",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=ordered_state,
        requirements=ordered_requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "First select records at the minimum length, then average those records.",
        ),
        model=operation_order_review,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=ordered_state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[4]]),
            "columns": ["answer"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert (
        "order and scope of nested operations may change their meaning"
        in prompts[0]["instruction"]
    )
    assert "only when trusted context proves the mismatch" in prompts[0]["instruction"]


def test_result_review_allows_empty_result_for_exact_filter() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="output-status",
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="status",
            ),
            ItemSpec(
                source_id="status-filter",
                kind=SemanticItemKind.FILTER,
                table="orders",
                column="status",
                operator=PredicateOperator.EQ,
                literal="missing",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List order statuses equal to missing.",
                    "requested_output_source_ids": ("output-status",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )

    def empty_filter_review(prompt: str) -> str:
        payload = json.loads(prompt)
        assert payload["ast"]["predicates"]
        assert payload["data"] == []
        filter_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "status-filter"
        )
        assert filter_binding["kind"] == "discriminator_value"
        assert filter_binding["discriminator_predicate"]["right"] == "missing"
        exact_filter = "o.status = 'missing'" in payload["sql"]
        if (
            not exact_filter
            or "An empty result does not by itself contradict"
            not in payload["instruction"]
            or "auxiliary probe over a different physical column" not in payload["instruction"]
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the SQL predicate differs from the confirmed filter",
                    "source_id": "status-filter",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the exact filter has no matching rows"}
        )

    def evaluate(sql: str, candidate_id: str) -> ResultReviewReceipt:
        parsed = parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)
        candidate = SqlCandidate(
            candidate_id=candidate_id,
            sql=sql,
            normalized_ast_digest=parsed.candidate_digest,
            revision=state.revision,
        )
        review = create_result_review_capability(
            state=state,
            requirements=requirements,
            freshness_context=freshness,
            candidate=candidate,
            parsed_ast=parsed,
            documents=(),
            model=empty_filter_review,
        )
        return evaluate_result_review_capability(
            review,
            expected_run_id=state.run_id,
            expected_sql=sql,
            execution={
                **_executor_result([]),
                "sql_query": sql,
            },
        )

    exact = evaluate(
        "SELECT o.status FROM orders o WHERE o.status = 'missing'",
        "terminal-empty-filter-review",
    )
    mismatch = evaluate(
        "SELECT o.status FROM orders o WHERE o.status = 'other'",
        "terminal-empty-filter-mismatch-review",
    )

    assert exact.verdict == "consistent"
    assert mismatch.verdict == "contradicted"
    assert mismatch.source_id == "status-filter"


def test_result_review_checks_fixed_positions_against_confirmed_input_format() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="average-output",
                kind=SemanticItemKind.METRIC,
                table="records",
                column="amount",
            ),
            ItemSpec(
                source_id="month-formula",
                kind=SemanticItemKind.FORMULA,
                table="records",
                column="recorded_at",
            ),
        ),
        shape=ExpectedResultShape.SCALAR,
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the average amount for records in May.",
                    "requested_output_source_ids": ("average-output",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split())
        incompatible = "SUBSTRING(r.recorded_at, 5, 2)" in payload["sql"]
        has_rule = (
            "compare each fixed-position expression in the AST with the confirmed "
            "physical representation of its supplied input binding"
            in instruction
            and "even when execution returned NULL or no rows" in instruction
            and "does not make NULL or an empty result contradictory when the "
            "expression is compatible"
            in instruction
        )
        if incompatible and has_rule:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the fixed positions select a separator",
                    "source_id": "month-formula",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the expression matches the format"}
        )

    def evaluate(sql: str, candidate_id: str) -> ResultReviewReceipt:
        parsed = parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)
        candidate = SqlCandidate(
            candidate_id=candidate_id,
            sql=sql,
            normalized_ast_digest=parsed.candidate_digest,
            revision=state.revision,
        )
        review = create_result_review_capability(
            state=state,
            requirements=requirements,
            freshness_context=freshness,
            candidate=candidate,
            parsed_ast=parsed,
            documents=("records.recorded_at uses YYYY-MM-DD.",),
            model=reviewer,
        )
        return evaluate_result_review_capability(
            review,
            expected_run_id=state.run_id,
            expected_sql=sql,
            execution={
                **_executor_result([[None]]),
                "columns": ["average_amount"],
                "sql_query": sql,
            },
        )

    wrong = evaluate(
        "SELECT AVG(r.amount) AS average_amount FROM records r "
        "WHERE SUBSTRING(r.recorded_at, 5, 2) = '05'",
        "terminal-wrong-fixed-position",
    )
    compatible = evaluate(
        "SELECT AVG(r.amount) AS average_amount FROM records r "
        "WHERE SUBSTRING(r.recorded_at, 6, 2) = '05'",
        "terminal-compatible-fixed-position",
    )

    assert wrong.verdict == "contradicted"
    assert wrong.source_id == "month-formula"
    assert compatible.verdict == "consistent"


def test_result_review_rejects_raw_date_against_dimensionless_age_threshold() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="age-filter",
                kind=SemanticItemKind.TIME,
                table="records",
                column="birth_date",
            ),
        )
    )
    temporal_item = state.query_spec.semantic_items[0].model_copy(
        update={
            "source_text": "age below eighteen years",
            "normalized_meaning": "age in years below eighteen",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Which records are younger than eighteen years?",
                    "semantic_items": (temporal_item,),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    sql = "SELECT r.id FROM records r WHERE r.birth_date < 18"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-raw-date-age")
    candidate = SqlCandidate(
        candidate_id="terminal-raw-date-age",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "r.birth_date < 18" in payload["sql"]
            and "A raw DATE or TIME column cannot be directly compared with a dimensionless "
            "numeric threshold." in instruction
            and "SUPPORTED binding proves source authority, not transformation correctness." in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the age threshold needs an explicit temporal transformation",
                    "source_id": "age-filter",
                }
            )
        return json.dumps({"status": "consistent", "reason": "the source binding is supported"})

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("records.birth_date is a DATE source.",),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["record-a"]]),
            "columns": ["id"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "age-filter"
    assert receipt.repair_kind is None


def test_result_review_rejects_birth_year_extraction_as_age_at_event() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="age-filter",
                kind=SemanticItemKind.TIME,
                table="records",
                column="birth_date",
            ),
            ItemSpec(
                source_id="event-reference",
                kind=SemanticItemKind.TIME,
                table="records",
                column="event_at",
            ),
        )
    )
    birth_binding, event_binding = state.bindings
    assert isinstance(birth_binding, PhysicalColumnBinding)
    assert isinstance(event_binding, PhysicalColumnBinding)
    age_rule = _document_evidence(
        "age-rule-evidence",
        content="Use EXTRACT(YEAR FROM birth_date) < threshold for age at an event.",
    )
    age_binding = DerivedExpressionBinding(
        binding_id=birth_binding.binding_id,
        source_id="age-filter",
        tables=(birth_binding.physical_column.table,),
        columns=(birth_binding.physical_column,),
        predicates=(),
        join_path=(),
        evidence_ids=(*birth_binding.evidence_ids, age_rule.evidence_id),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        expression=ExpressionRef(
            expression_id="age-at-event-expression",
            expression="EXTRACT(YEAR FROM birth_date) < threshold",
        ),
        rule_excerpt="EXTRACT(YEAR FROM birth_date) < threshold",
        input_columns=(birth_binding.physical_column,),
    )
    age_item, event_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "bindings": (age_binding, event_binding),
            "evidence": (*state.evidence, age_rule),
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Which records were younger than twenty-three at their event?",
                    "semantic_items": (
                        age_item.model_copy(
                            update={
                                "source_text": "age at event",
                                "normalized_meaning": (
                                    "year extracted from birth date below threshold"
                                ),
                            }
                        ),
                        event_item.model_copy(
                            update={
                                "source_text": "event date",
                                "normalized_meaning": "reference date for the event",
                            }
                        ),
                    ),
                }
            ),
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "EXTRACT(YEAR FROM r.birth_date) < 23" in payload["sql"]
            and "Extracting only the year from one birth or date source is not age or duration."
            in instruction
            and "A matching expression, binding, or document cannot override this."
            in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "birth year alone does not compute age at the event",
                    "source_id": "age-filter",
                }
            )
        if "r.birth_date > r.event_at - INTERVAL '23 years'" in payload["sql"]:
            if (
                "For a required age or duration semantic role, a candidate AST using "
                "confirmed birth and event/reference inputs or a confirmed full-date "
                "computation may complete a narrower one-input calendar shorthand in "
                "normalized meaning, expression_claim, or document"
                in instruction
                and "A stale one-input binding or shorthand alone does not contradict it"
                in instruction
            ):
                return json.dumps(
                    {
                        "status": "consistent",
                        "reason": "the age computation uses the event reference date",
                    }
                )
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the expression differs from the narrower shorthand",
                    "source_id": "age-filter",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the selected expression is supported"}
        )

    def evaluate(sql: str, candidate_id: str) -> ResultReviewReceipt:
        parsed = parse_sql_candidate(sql, POSTGRES_DSN, candidate_id)
        candidate = SqlCandidate(
            candidate_id=candidate_id,
            sql=sql,
            normalized_ast_digest=parsed.candidate_digest,
            revision=state.revision,
        )
        review = create_result_review_capability(
            state=state,
            requirements=requirements,
            freshness_context=freshness,
            candidate=candidate,
            parsed_ast=parsed,
            documents=("Use EXTRACT(YEAR FROM birth_date) < threshold for age at an event.",),
            model=reviewer,
        )
        return evaluate_result_review_capability(
            review,
            expected_run_id=state.run_id,
            expected_sql=sql,
            execution={
                **_executor_result([["record-a"]]),
                "columns": ["id"],
                "sql_query": sql,
            },
        )

    one_input = evaluate(
        "SELECT r.id FROM records r WHERE EXTRACT(YEAR FROM r.birth_date) < 23",
        "terminal-birth-year-age",
    )
    multi_input = evaluate(
        "SELECT r.id FROM records r "
        "WHERE r.birth_date > r.event_at - INTERVAL '23 years'",
        "terminal-age-at-event",
    )

    assert one_input.verdict == "contradicted"
    assert one_input.source_id == "age-filter"
    assert one_input.repair_kind is None
    assert multi_input.verdict == "consistent"


def test_result_review_temporal_prompt_preserves_valid_boundaries_and_transforms() -> None:
    state, requirements, candidate, _ = _case()
    prompts: list[str] = []

    def reviewer(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"status": "consistent", "reason": "the comparison is explicit"})

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["paid"]]),
    )

    assert receipt.verdict == "consistent"
    instruction = json.loads(prompts[0])["instruction"]
    assert (
        "Do not reject a full-date-to-full-date boundary, timestamp-to-timestamp boundary, "
        "dialect-supported calendar-part extraction when that calendar part is requested, or "
        "explicit age or duration computation."
        in instruction
    )


def test_result_review_omits_arbitrary_probe_rows() -> None:
    state, requirements, candidate, _ = _case()
    marker = "unrelated-alternative-calculation"
    probe_evidence = state.evidence[0].model_copy(
        update={
            "source_kind": EvidenceSourceKind.PROBE,
            "observation": json.dumps({"marker": marker}),
            "validity_scope": EvidenceValidityScope.RUN_ONLY,
            "data_snapshot_token": None,
        }
    )
    state = state.model_copy(update={"evidence": (probe_evidence,)})

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if marker in json.dumps(payload["evidence"]):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "an auxiliary probe suggests another calculation",
                    "source_id": "source-1",
                }
            )
        return json.dumps({"status": "consistent", "reason": "result matches"})

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=probe_evidence.observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["paid"]]),
    )

    assert receipt.verdict == "consistent"


def test_result_review_does_not_treat_related_proxy_as_requested_attribute() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="legal-status",
                kind=SemanticItemKind.DIMENSION,
                table="accounts",
                column="preferred_language",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "What is the account holder's legal status?",
                    "requested_output_source_ids": ("legal-status",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = "SELECT a.preferred_language FROM accounts a"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-related-proxy-review")
    candidate = SqlCandidate(
        candidate_id="terminal-related-proxy-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def related_proxy_review(prompt: str) -> str:
        payload = json.loads(prompt)
        assert payload["question"] == "What is the account holder's legal status?"
        assert payload["columns"] == ["preferred_language"]
        assert payload["data"] == [["English"]]
        if "A related or correlated attribute is not the requested attribute" not in payload[
            "instruction"
        ]:
            return json.dumps(
                {
                    "status": "consistent",
                    "reason": "preferred language suggests a legal status",
                }
            )
        return json.dumps(
            {
                "status": "ambiguous",
                "reason": "preferred language does not prove the holder's legal status",
                "source_id": "legal-status",
                "repair_kind": "semantic_binding_mismatch",
                "repair_binding_id": payload["bindings"][0]["binding_id"],
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "accounts.preferred_language stores the holder's communication preference.",
        ),
        model=related_proxy_review,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["English"]]),
            "columns": ["preferred_language"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "ambiguous"
    assert receipt.source_id == "legal-status"
    assert receipt.repair_kind == "semantic_binding_mismatch"
    assert receipt.repair_binding_id == requirements.selected_bindings[0].binding_id


def test_result_review_rejects_event_value_for_requested_entity_attribute() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="ordinary-label",
                kind=SemanticItemKind.DIMENSION,
                table="entities",
                column="label",
            ),
            ItemSpec(
                source_id="entity-owner",
                kind=SemanticItemKind.DIMENSION,
                table="entities",
                column="id",
            ),
            ItemSpec(
                source_id="entity-code",
                kind=SemanticItemKind.DIMENSION,
                table="event_entries",
                column="code",
                join_path=(
                    inner_join("event_entries", "entity_id", "entities", "id"),
                ),
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "What is the named entity's code?",
                    "requested_output_source_ids": ("entity-code", "ordinary-label"),
                    "semantic_items": tuple(
                        item.model_copy(
                            update={
                                "source_text": (
                                    "ordinary label"
                                    if item.source_id == "ordinary-label"
                                    else (
                                        "named entity"
                                        if item.source_id == "entity-owner"
                                        else "named entity code"
                                    )
                                ),
                                "normalized_meaning": (
                                    "ordinary label"
                                    if item.source_id == "ordinary-label"
                                    else (
                                        "named entity"
                                        if item.source_id == "entity-owner"
                                        else "code of the named entity"
                                    )
                                ),
                                "owner_source_id": (
                                    "entity-owner"
                                    if item.source_id == "entity-code"
                                    else None
                                ),
                            }
                        )
                        for item in state.query_spec.semantic_items
                    ),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT n.label, e.code FROM event_entries e "
        "JOIN entities n ON e.entity_id = n.id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-event-code-review")
    candidate = SqlCandidate(
        candidate_id="terminal-event-code-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(_prompt: str) -> str:
        pytest.fail("exact owner mismatch must not call the model reviewer")

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
        schema={
            "main.event_entries": {
                "description": "Individual event records.",
                "columns": {"code": {"description": "Code."}},
            },
            "main.entities": {
                "description": "Permanent entity attributes.",
                "columns": {"code": {"description": "Code."}},
            },
        },
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["L-1", "E-7"]]),
            "columns": ["label", "code"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "entity-code"
    assert receipt.repair_kind == "semantic_binding_mismatch"
    assert receipt.repair_binding_id == requirements.selected_bindings[0].binding_id


def test_result_review_keeps_entity_value_when_event_has_same_attribute() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="entity-code",
                kind=SemanticItemKind.DIMENSION,
                table="entities",
                column="code",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "What is the named entity's code?",
                    "requested_output_source_ids": ("entity-code",),
                    "semantic_items": tuple(
                        item.model_copy(
                            update={
                                "source_text": "named entity code",
                                "normalized_meaning": "code of the named entity",
                            }
                        )
                        for item in state.query_spec.semantic_items
                    ),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = "SELECT e.code FROM entities e JOIN event_entries x ON x.entity_id = e.id"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-entity-code-review")
    candidate = SqlCandidate(
        candidate_id="terminal-entity-code-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        owner_checklist = (
            "Final mandatory owner checklist, which takes precedence over earlier row-local and "
            "full-label rules: (1) Determine the owner of every requested output "
            "from the question and normalized meaning. (2) Determine the owner table from table "
            "identity and description; a column description need not repeat the owner. (3) If a "
            "same-named event or record column is selected instead of the direct column of the named "
            "entity, return contradicted with semantic_binding_mismatch. (4) Conversely, do not reject "
            "a direct named-entity column because an event or record has a same-named column."
        )
        if (
            not instruction.endswith(owner_checklist)
            or "Before applying any result-grain or qualifying-row rule, resolve explicit requested "
            "attribute ownership." in instruction
            or "For explicit named-entity ownership, semantic_binding_mismatch requires" in instruction
            or "When an output is requested as an attribute of a named entity, preserve the output "
            "described in the qualifying row scope." in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the event has a code column too",
                    "source_id": "entity-code",
                    "repair_kind": "semantic_binding_mismatch",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the selected code belongs to the entity"}
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
        schema={
            "main.entities": {
                "description": "Permanent entity attributes.",
                "columns": {"code": {"description": "Code."}},
            },
            "main.event_entries": {
                "description": "Individual event records.",
                "columns": {"code": {"description": "Code."}},
            },
        },
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["P-9"]]),
            "columns": ["code"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


def test_result_review_rejects_partial_in_scope_label() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="activity-name",
                kind=SemanticItemKind.DIMENSION,
                table="activity_rows",
                column="display_label",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List the names for qualifying activities.",
                    "requested_output_source_ids": ("activity-name",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT a.display_label FROM activity_rows a "
        "INNER JOIN activity_reports r ON a.report_id = r.id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-in-scope-label-review")
    candidate = SqlCandidate(
        candidate_id="terminal-in-scope-label-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    schema = {
        "main.activity_rows": {
            "description": "Qualifying activity records.",
            "columns": {
                "display_label": {
                    "description": "Nullable partial label shown for that activity."
                },
            },
        },
        "main.activity_reports": {
            "description": "Reports for qualifying activities.",
            "columns": {
                "reported_name": {
                    "description": "Full official name for the row supplying the required condition."
                },
            },
        },
    }

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "Before returning consistent for each requested DIMENSION label, compare the selected "
            "label against label columns on relations already used by the candidate AST"
            not in instruction
            or "The alternative must be a semantically matching full requested label, not merely "
            "any full label in a joined relation" not in instruction
            or "Do not repair a NULL or partial selected output by filtering out qualifying rows "
            "when a semantically matching full or official label exists on a relation already used "
            "by the candidate AST" not in instruction
            or "a label explicitly described as full or official for the row supplying a required "
            "condition or formula is the row-local output" not in instruction
            or "Do not name an alternative as a replacement unless its trusted description explicitly "
            "establishes a full or official matching label for the same qualifying row; a generic "
            "entity name is not enough" not in instruction
            or "another label is full for the same qualifying rows, return contradicted targeting "
            "the supplied binding" not in instruction
            or instruction.index("Before returning consistent for each requested DIMENSION label")
            > instruction.index("Return only JSON object")
            or payload.get("schema") != schema
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the selected partial label should be filtered out",
                    "source_id": "activity-name",
                    "repair_kind": None,
                    "repair_binding_id": None,
                }
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the selected label is partial activity data, not the full reported name",
                "source_id": "activity-name",
                "repair_kind": "semantic_binding_mismatch",
                "repair_binding_id": payload["bindings"][0]["binding_id"],
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
        schema=schema,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["Short label"]]),
            "columns": ["display_label"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "activity-name"
    assert receipt.repair_kind == "semantic_binding_mismatch"
    assert receipt.repair_binding_id == requirements.selected_bindings[0].binding_id


def test_result_review_rejects_reference_for_requested_human_name() -> None:
    path = (inner_join("people", "id", "visits", "person_id"),)
    state = build_state(
        (
            ItemSpec(
                source_id="person-name",
                kind=SemanticItemKind.DIMENSION,
                table="people",
                column="reference",
                join_path=path,
            ),
            ItemSpec(
                source_id="profile-url",
                kind=SemanticItemKind.DIMENSION,
                table="people",
                column="profile_url",
                join_path=path,
            ),
            ItemSpec(
                source_id="qualifying-visit",
                kind=SemanticItemKind.FILTER,
                table="visits",
                column="is_active",
                operator=PredicateOperator.EQ,
                literal=True,
                join_path=path,
            ),
        )
    ).model_copy(
        update={
            "query_spec": build_state(
                (
                    ItemSpec("person-name", SemanticItemKind.DIMENSION, "people", "reference"),
                    ItemSpec("profile-url", SemanticItemKind.DIMENSION, "people", "profile_url"),
                    ItemSpec("qualifying-visit", SemanticItemKind.FILTER, "visits", "is_active", operator=PredicateOperator.EQ, literal=True),
                )
            ).query_spec.model_copy(
                update={
                    "original_text": "List the human-readable names and profile links for active visits.",
                    "requested_output_source_ids": ("person-name", "profile-url"),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(state, freshness, state.run_id, state.run_incarnation)
    sql = "SELECT p.reference, p.profile_url FROM people p INNER JOIN visits v ON p.id = v.person_id WHERE v.is_active = TRUE"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-human-name-review")
    candidate = SqlCandidate(candidate_id="terminal-human-name-review", sql=sql, normalized_ast_digest=parsed.candidate_digest, revision=state.revision)
    schema = {"main.people": {"description": "People.", "columns": {"reference": {"description": "Technical reference name."}, "given_name": {"description": "Human-readable given-name component."}, "family_name": {"description": "Human-readable family-name component."}, "profile_url": {"description": "Profile link."}}}, "main.visits": {"description": "Qualifying visits.", "columns": {"person_id": {"description": "Person reference."}, "is_active": {"description": "Active flag."}}}}
    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if "requested human-readable name" not in payload["instruction"]:
            return json.dumps({"status": "consistent", "reason": "reference accepted"})
        return json.dumps({"status": "contradicted", "reason": "reference is not the requested human-readable name", "source_id": "person-name", "repair_kind": "semantic_binding_mismatch", "repair_binding_id": payload["bindings"][0]["binding_id"]})
    receipt = evaluate_result_review_capability(create_result_review_capability(state=state, requirements=requirements, freshness_context=freshness, candidate=candidate, parsed_ast=parsed, documents=(), model=reviewer, schema=schema), expected_run_id=state.run_id, expected_sql=sql, execution={**_executor_result([["ref-7", "https://example.test/profile"]]), "columns": ["reference", "profile_url"], "sql_query": sql})
    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "person-name"
    assert receipt.repair_kind == "semantic_binding_mismatch"
    assert receipt.repair_binding_id == requirements.selected_bindings[0].binding_id


@pytest.mark.parametrize(
    ("question", "expected_verdict"),
    (
        ("Return the account cumulative amount.", "contradicted"),
        ("Which account has the highest amount?", "contradicted"),
        ("Which account has the highest transaction amount?", "consistent"),
    ),
)
def test_result_review_distinguishes_summary_measure_from_detail_measure_without_ranking(
    question: str, expected_verdict: str
) -> None:
    state = build_state(
        (ItemSpec("account-amount", SemanticItemKind.METRIC, "ledger_entries", "amount"),)
    ).model_copy(
        update={
            "query_spec": build_state(
                (ItemSpec("account-amount", SemanticItemKind.METRIC, "ledger_entries", "amount"),)
            ).query_spec.model_copy(
                update={"original_text": question, "requested_output_source_ids": ("account-amount",)}
            )
        }
    )
    freshness = FreshnessContext(evaluated_at=state.evidence[0].observed_at, run_id=state.run_id, run_incarnation=state.run_incarnation, schema_namespace_version=state.schema_namespace_version)
    requirements = validate_coverage_inputs(state, freshness, state.run_id, state.run_incarnation)
    sql = "SELECT e.amount FROM ledger_entries e"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-summary-measure-review")
    candidate = SqlCandidate(candidate_id="terminal-summary-measure-review", sql=sql, normalized_ast_digest=parsed.candidate_digest, revision=state.revision)
    schema = {"main.ledger_entries": {"description": "Individual transaction details.", "columns": {"amount": {"description": "Amount for one transaction detail."}}}, "main.account_standings": {"description": "Cumulative account standings.", "columns": {"amount": {"description": "Cumulative amount for account ranking."}}}}
    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "When an event or detail measure is selected for a requested summary, standing, or cumulative "
            "measure"
            not in instruction
        ):
            return json.dumps({"status": "consistent", "reason": "detail accepted"})
        explicit_operation = "transaction" in payload["question"].lower()
        if "cumulative" in payload["question"].lower() or (
            "highest" in payload["question"].lower()
            and not explicit_operation
            and "When a request asks which entity has the minimum or maximum of a measure, "
            "that measure must have the entity's grain"
            in instruction
        ) or (
            explicit_operation
            and "Do not apply this when the question or QuerySpec explicitly requests the event or operation."
            not in instruction
        ):
            return json.dumps({"status": "contradicted", "reason": "detail measure conflicts with requested cumulative standing", "source_id": "account-amount", "repair_kind": "semantic_binding_mismatch", "repair_binding_id": payload["bindings"][0]["binding_id"]})
        return json.dumps({"status": "consistent", "reason": "explicit transaction request permits detail"})
    receipt = evaluate_result_review_capability(create_result_review_capability(state=state, requirements=requirements, freshness_context=freshness, candidate=candidate, parsed_ast=parsed, documents=(), model=reviewer, schema=schema), expected_run_id=state.run_id, expected_sql=sql, execution={**_executor_result([[4]]), "columns": ["amount"], "sql_query": sql})
    assert receipt.verdict == expected_verdict
    if expected_verdict == "contradicted":
        assert receipt.repair_kind == "semantic_binding_mismatch"
        assert receipt.repair_binding_id == requirements.selected_bindings[0].binding_id


def test_result_review_does_not_impose_output_only_join_type() -> None:
    join_path = (inner_join("organizations", "id", "ratings", "organization_id"),)
    state = build_state(
        (
            ItemSpec(
                source_id="qualified-organizations",
                kind=SemanticItemKind.FILTER,
                table="organizations",
                column="is_active",
                operator=PredicateOperator.EQ,
                literal=True,
            ),
            ItemSpec(
                source_id="rating",
                kind=SemanticItemKind.METRIC,
                table="ratings",
                column="score",
                join_path=join_path,
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List ratings for active organizations.",
                    "requested_output_source_ids": ("rating",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT r.score FROM organizations o "
        "INNER JOIN ratings r ON o.id = r.organization_id "
        "WHERE o.is_active = TRUE"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-output-only-join-review")
    candidate = SqlCandidate(
        candidate_id="terminal-output-only-join-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    schema = {
        "main.organizations": {
            "columns": {"is_active": {"description": "Qualifies organization rows."}}
        },
        "main.ratings": {
            "columns": {"score": {"description": "Requested rating value."}}
        },
    }
    rule = (
        "Inspect join type and direction before returning consistent. When all row conditions "
        "are on A and B only supplies a requested output, an INNER JOIN or reversed LEFT JOIN "
        "can discard qualifying A rows: return contradicted targeting B's supplied binding with "
        "repair_kind semantic_binding_mismatch. A required requested output, including a METRIC, "
        "requires returning B's column, not a matching B row. NULL in a matched B row does not "
        "prove that absence of a B row is preserved. This does not apply when B participates in "
        "row qualification or the question explicitly requires a matching or nonempty B value."
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if rule not in payload["instruction"] or payload.get("schema") != schema:
            return json.dumps({"status": "consistent", "reason": "join preserves rows"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "inner join drops active organizations without ratings",
                "source_id": "rating",
                "repair_kind": "semantic_binding_mismatch",
                "repair_binding_id": payload["bindings"][1]["binding_id"],
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
        schema=schema,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={**_executor_result([[4.5]]), "columns": ["score"], "sql_query": sql},
    )

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None
    assert receipt.repair_kind is None
    assert receipt.repair_binding_id is None


def test_result_review_clears_binding_without_semantic_repair() -> None:
    state, requirements, candidate, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "the selected output does not match the request",
                "source_id": "source-1",
                "repair_kind": None,
                "repair_binding_id": requirements.selected_bindings[0].binding_id,
            }
        ),
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"]]),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert receipt.repair_kind is None
    assert receipt.repair_binding_id is None


def test_result_review_canonicalizes_semantic_repair_binding() -> None:
    state, requirements, candidate, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "the selected output does not match the request",
                "source_id": "source-1",
                "repair_kind": "semantic_binding_mismatch",
                "repair_binding_id": "different-binding",
            }
        ),
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"]]),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert receipt.repair_kind == "semantic_binding_mismatch"
    assert receipt.repair_binding_id == requirements.selected_bindings[0].binding_id


def test_result_review_clears_predicate_authority_from_semantic_repair() -> None:
    state, requirements, candidate, _ = _case()
    predicate = PredicateRef(
        left=_column(),
        operator=PredicateOperator.EQ,
        right="active",
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "the selected output does not match the request",
                "source_id": "source-1",
                "repair_kind": "semantic_binding_mismatch",
                "repair_binding_id": requirements.selected_bindings[0].binding_id,
                "predicate_authority": predicate.model_dump(mode="json"),
            }
        ),
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"]]),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.repair_kind == "semantic_binding_mismatch"
    assert receipt.predicate_authority is None
    with pytest.raises(ValueError, match="predicate authority requires"):
        ResultReviewReceipt(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            research_state_revision=state.revision,
            candidate_id=candidate.candidate_id,
            normalized_ast_digest=candidate.normalized_ast_digest,
            requirements_digest=requirements.requirements_digest,
            source_id="source-1",
            evidence_id="terminal-result-validation-not-null",
            verdict="contradicted",
            reason="invalid mixed repair",
            execution=_executor_result([["open"]]),
            deterministic_failure_code=None,
            repair_kind="semantic_binding_mismatch",
            repair_binding_id=requirements.selected_bindings[0].binding_id,
            predicate_authority=predicate,
        )


def test_result_review_prompt_keeps_qualifying_row_output_without_canonical_request() -> None:
    state, requirements, candidate, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "List the statuses recorded for qualifying orders."}
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    prompts: list[str] = []
    sql = (
        "SELECT o.status FROM orders o "
        "INNER JOIN entities e ON o.entity_id = e.id "
        "WHERE e.is_active = TRUE"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-external-master-review")
    candidate = SqlCandidate(
        candidate_id="terminal-external-master-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    schema = {
        "main.orders": {
            "columns": {
                "status": {"description": "Status recorded on that order."},
                "entity_id": {"description": "Entity for that order."},
            }
        },
        "main.entities": {
            "columns": {
                "id": {"description": "Entity identity."},
                "is_active": {"description": "Whether the entity is active."},
                "current_status": {
                    "description": "Current canonical master status of the entity."
                },
            }
        },
    }

    def reviewer(prompt: str) -> str:
        prompts.append(prompt)
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "Exclude external current, canonical, persistent, or master labels unless the "
            "question explicitly requests them or trusted schema or documents prove equivalence "
            "at the qualifying row scope" not in instruction
            or "Do not repair a NULL or partial selected output by filtering out qualifying rows "
            "when a semantically matching full or official label exists on a relation already used "
            "by the candidate AST" not in instruction
            or "The alternative must be a semantically matching full requested label, not merely "
            "any full label in a joined relation" not in instruction
            or payload.get("schema") != schema
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "external master label replaced the row-local output",
                    "source_id": "source-1",
                    "repair_kind": "semantic_binding_mismatch",
                    "repair_binding_id": payload["bindings"][0]["binding_id"],
                }
            )
        return json.dumps({"status": "consistent", "reason": "row value is requested"})

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=reviewer,
        schema=schema,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={**_executor_result([["open"]]), "sql_query": sql},
    )

    assert receipt.verdict == "consistent"
    assert "is the row-local output." in json.loads(prompts[0])["instruction"]


def test_result_review_does_not_reopen_supported_relationship_without_contradiction() -> None:
    join_path = (
        inner_join("projects", "id", "assignments", "project_id"),
        inner_join("assignments", "member_id", "members", "id"),
    )
    state = build_state(
        (
            ItemSpec(
                source_id="responsible-member",
                kind=SemanticItemKind.DIMENSION,
                table="members",
                column="name",
                join_path=join_path,
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Which members are responsible through the recorded project assignments?",
                    "requested_output_source_ids": ("responsible-member",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "SELECT m.name FROM projects p "
        "JOIN assignments a ON a.project_id = p.id "
        "JOIN members m ON m.id = a.member_id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-supported-relationship")
    candidate = SqlCandidate(
        candidate_id="terminal-supported-relationship",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def relationship_review(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        if "does not by itself contradict a selected supported relationship" not in instruction:
            return json.dumps(
                {
                    "status": "ambiguous",
                    "reason": "the schema text does not repeat the business wording",
                    "source_id": "responsible-member",
                    "repair_kind": "semantic_binding_mismatch",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the SQL follows the supported relationship and no trusted fact contradicts it",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "assignments.project_id references projects.id; "
            "assignments.member_id references members.id.",
        ),
        model=relationship_review,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["Alex"]]),
            "columns": ["name"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None
    assert receipt.repair_kind is None


def test_result_review_marks_exact_document_column_mismatch_as_binding_repair() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="registration-date",
                kind=SemanticItemKind.DIMENSION,
                table="accounts",
                column="updated_at",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "When was the account registered?",
                    "requested_output_source_ids": ("registration-date",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = "SELECT a.updated_at FROM accounts a"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-document-column-mismatch")
    candidate = SqlCandidate(
        candidate_id="terminal-document-column-mismatch",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def exact_document_review(prompt: str) -> str:
        payload = json.loads(prompt)
        if "explicitly maps a required semantic item" not in payload["instruction"]:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the document maps registration date to accounts.created_at",
                    "source_id": "registration-date",
                    "repair_kind": None,
                }
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the document maps registration date to accounts.created_at",
                "source_id": "registration-date",
                "repair_kind": "semantic_binding_mismatch",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("The registration date is stored in accounts.created_at.",),
        model=exact_document_review,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["2024-01-02"]]),
            "columns": ["updated_at"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "registration-date"
    assert receipt.repair_kind == "semantic_binding_mismatch"


def test_result_review_keeps_same_normalized_selected_column() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="organization-charter-number",
                kind=SemanticItemKind.DIMENSION,
                table="organizations",
                column="charter_number",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List the charter numbers of organizations.",
                    "requested_output_source_ids": ("organization-charter-number",),
                }
            )
        }
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = "SELECT o.charter_number FROM organizations o"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-normalized-column")
    candidate = SqlCandidate(
        candidate_id="terminal-normalized-column",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def normalized_column_review(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        binding = payload["bindings"][0]
        if (
            "same normalized table and column" not in instruction
            or "unqualified table and main.table are the same" not in instruction
            or "positive trusted fact" not in instruction
            or binding["physical_column"]["table"]["table"] != "organizations"
            or binding["physical_column"]["column"] != "charter_number"
            or "main.organizations" not in payload["schema"]
            or not payload["evidence"]
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the reviewer incorrectly treats the selected column as different",
                    "source_id": "organization-charter-number",
                    "repair_kind": "semantic_binding_mismatch",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the selected binding, trusted evidence and AST use the same column",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "main.organizations.charter_number stores the charter number for an organization.",
        ),
        model=normalized_column_review,
        schema={
            "main.organizations": {
                "columns": {
                    "charter_number": {
                        "description": "Recorded charter number for an organization."
                    }
                }
            }
        },
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["C-17"]]),
            "columns": ["charter_number"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"
    assert receipt.repair_kind is None


def test_result_review_rejects_unrequested_auxiliary_projection() -> None:
    state, _, _, _ = _case()
    sql = (
        "SELECT o.status AS account_id, SUM(o.status) AS total_usage "
        "FROM orders o GROUP BY o.status ORDER BY total_usage ASC LIMIT 1"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-projection-review")
    candidate = SqlCandidate(
        candidate_id="terminal-projection-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    account_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={"original_text": "Which account had the least total usage in 2024?"}
            )
        }
    )
    account_requirements = validate_coverage_inputs(
        account_state,
        freshness,
        account_state.run_id,
        account_state.run_incarnation,
    )

    def unrequested_auxiliary_output(prompt: str) -> str:
        payload = json.loads(prompt)
        assert payload["question"] == "Which account had the least total usage in 2024?"
        assert payload["ast"]["aggregates"]
        assert payload["ast"]["groupings"]
        assert payload["columns"] == ["account_id", "total_usage"]
        assert payload["data"] == [["account-a", 4]]
        if (
            "auxiliary computation solely because it is needed for ordering or grouping"
            not in payload["instruction"]
            or "technical physical key used only for JOIN, GROUP BY, ORDER BY, window partition, or dedup"
            not in payload["instruction"]
        ):
            return json.dumps({"status": "consistent", "reason": "extra output accepted"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the question does not request the auxiliary total",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=account_state,
        requirements=account_requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=unrequested_auxiliary_output,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=account_state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["account-a", 4]]),
            "columns": ["account_id", "total_usage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"

    requested_state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Which account had the least total usage, and what was that total?"
                    )
                }
            )
        }
    )
    requested_requirements = validate_coverage_inputs(
        requested_state,
        freshness,
        requested_state.run_id,
        requested_state.run_incarnation,
    )

    def requested_auxiliary_output(prompt: str) -> str:
        payload = json.loads(prompt)
        assert payload["question"] == (
            "Which account had the least total usage, and what was that total?"
        )
        assert payload["ast"]["aggregates"]
        assert payload["ast"]["groupings"]
        if "unless the question or documents explicitly request it" not in payload["instruction"]:
            return json.dumps(
                {
                    "status": "ambiguous",
                    "reason": "the requested total is not allowed",
                    "source_id": "source-1",
                }
            )
        return json.dumps({"status": "consistent", "reason": "both outputs are requested"})

    requested_review = create_result_review_capability(
        state=requested_state,
        requirements=requested_requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=requested_auxiliary_output,
    )
    requested_receipt = evaluate_result_review_capability(
        requested_review,
        expected_run_id=requested_state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["account-a", 4]]),
            "columns": ["account_id", "total_usage"],
            "sql_query": sql,
        },
    )

    assert requested_receipt.verdict == "consistent"


def _projection_review_state(*, requested_output_source_ids: tuple[str, ...]):
    entity = _coverage_column("items", "id")
    total = _coverage_column("items", "amount")
    entity_evidence = _schema_evidence("projection-entity-evidence", entity)
    total_evidence = _schema_evidence("projection-total-evidence", total)
    entity_binding = PhysicalColumnBinding(
        binding_id="projection-entity-binding",
        source_id="projection-entity",
        tables=(entity.table,),
        columns=(entity,),
        predicates=(),
        join_path=(),
        evidence_ids=(entity_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=entity,
    )
    total_binding = PhysicalColumnBinding(
        binding_id="projection-total-binding",
        source_id="projection-total",
        tables=(total.table,),
        columns=(total,),
        predicates=(),
        join_path=(),
        evidence_ids=(total_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=total,
    )
    state = _coverage_state(
        item_specs=(
            (
                "projection-entity",
                True,
                SemanticItemStatus.RESOLVED,
                (entity_binding.binding_id,),
            ),
            (
                "projection-total",
                True,
                SemanticItemStatus.RESOLVED,
                (total_binding.binding_id,),
            ),
        ),
        bindings=(entity_binding, total_binding),
        evidence=(entity_evidence, total_evidence),
    )
    query_spec = state.query_spec.model_copy(
        update={"requested_output_source_ids": requested_output_source_ids}
    )
    return state.model_copy(update={"query_spec": query_spec})


def _projection_review(
    state,
    sql: str,
    model,
    *,
    document_sources=(),
    documents=(),
    execution_data=None,
    execution_columns=None,
    dsn=POSTGRES_DSN,
):
    parsed = parse_sql_candidate(sql, dsn, "terminal-output-role")
    candidate = SqlCandidate(
        candidate_id="terminal-output-role",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=document_sources,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    capability = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=documents,
        model=model,
    )
    return evaluate_result_review_capability(
        capability,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            "success": True,
            "data": [[1, 10]] if execution_data is None else execution_data,
            "columns": ["id", "total"] if execution_columns is None else execution_columns,
            "rows_affected": 1,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": sql,
            "applied_row_limit": 10,
        },
    )


def _exact_arithmetic_projection_review_state():
    label = _coverage_column("records", "label")
    recorded_at = _coverage_column("records", "recorded_at")
    label_evidence = _schema_evidence("arithmetic-label-evidence", label)
    recorded_at_evidence = _schema_evidence(
        "arithmetic-recorded-at-evidence", recorded_at
    )
    label_binding = PhysicalColumnBinding(
        binding_id="arithmetic-label-binding",
        source_id="arithmetic-label",
        tables=(label.table,),
        columns=(label,),
        predicates=(),
        join_path=(),
        evidence_ids=(label_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=label,
    )
    formula_binding = PhysicalColumnBinding(
        binding_id="arithmetic-formula-binding",
        source_id="arithmetic-formula",
        tables=(recorded_at.table,),
        columns=(recorded_at,),
        predicates=(),
        join_path=(),
        evidence_ids=(recorded_at_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=recorded_at,
    )
    state = _coverage_state(
        item_specs=(
            (
                "arithmetic-label",
                True,
                SemanticItemStatus.RESOLVED,
                (label_binding.binding_id,),
            ),
            (
                "arithmetic-formula",
                True,
                SemanticItemStatus.RESOLVED,
                (formula_binding.binding_id,),
            ),
        ),
        bindings=(label_binding, formula_binding),
        evidence=(label_evidence, recorded_at_evidence),
    )
    label_item, formula_item = state.query_spec.semantic_items
    return state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        label_item,
                        formula_item.model_copy(
                            update={
                                "kind": SemanticItemKind.FORMULA,
                                "source_text": "elapsed interval",
                                "normalized_meaning": "CURRENT_TIMESTAMP - recorded_at",
                            }
                        ),
                    ),
                    "requested_output_source_ids": ("arithmetic-formula",),
                }
            )
        }
    )


def _formula_with_input_projection_review_state():
    state = _exact_arithmetic_projection_review_state()
    label_binding, recorded_at_binding = state.bindings
    recorded_at_binding = recorded_at_binding.model_copy(
        update={
            "binding_id": "arithmetic-recorded-at-binding",
            "source_id": "arithmetic-recorded-at",
        }
    )
    formula_evidence = _document_evidence(
        "arithmetic-formula-evidence",
        content="The exact formula is CURRENT_TIMESTAMP - recorded_at.",
    )
    formula_binding = DerivedExpressionBinding(
        binding_id="arithmetic-derived-formula-binding",
        source_id="arithmetic-formula",
        tables=recorded_at_binding.tables,
        columns=recorded_at_binding.columns,
        predicates=(),
        join_path=(),
        evidence_ids=(
            recorded_at_binding.evidence_ids[0],
            formula_evidence.evidence_id,
        ),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        expression=ExpressionRef(
            expression_id="arithmetic-derived-expression",
            expression="CURRENT_TIMESTAMP - recorded_at",
        ),
        rule_excerpt="The exact formula is CURRENT_TIMESTAMP - recorded_at.",
        input_columns=recorded_at_binding.columns,
    )
    label_item, recorded_at_item = state.query_spec.semantic_items
    return state.model_copy(
        update={
            "bindings": (label_binding, recorded_at_binding, formula_binding),
            "evidence": (*state.evidence, formula_evidence),
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        label_item,
                        recorded_at_item.model_copy(
                            update={
                                "source_id": "arithmetic-recorded-at",
                                "source_text": "recorded timestamp",
                                "normalized_meaning": "recorded timestamp",
                                "kind": SemanticItemKind.DIMENSION,
                                "binding_ids": (recorded_at_binding.binding_id,),
                            }
                        ),
                        SemanticItem(
                            source_id="arithmetic-formula",
                            kind=SemanticItemKind.FORMULA,
                            source_text="elapsed interval",
                            normalized_meaning="CURRENT_TIMESTAMP - recorded_at",
                            required=True,
                            operator=None,
                            literal_or_reference=None,
                            status=SemanticItemStatus.RESOLVED,
                            binding_ids=(formula_binding.binding_id,),
                        ),
                    ),
                    "requested_output_source_ids": ("arithmetic-formula",),
                }
            ),
        }
    )


def test_result_review_rejects_requested_formula_reduced_to_direct_input_column() -> None:
    state = _exact_arithmetic_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "model accepted input"})

    receipt = _projection_review(
        state,
        "WITH projected AS (SELECT r.recorded_at FROM records r) "
        "SELECT projected.recorded_at FROM projected",
        model,
        execution_data=[["2026-09-09T00:00:00"]],
        execution_columns=["recorded_at"],
    )

    assert receipt.verdict == "contradicted"
    assert (
        receipt.deterministic_failure_code
        is CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
    )
    assert calls == 0


def test_result_review_keeps_computed_formula_behind_projection_alias() -> None:
    state = _exact_arithmetic_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula is computed"})

    receipt = _projection_review(
        state,
        "WITH projected AS ("
        "SELECT CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r"
        ") SELECT projected.elapsed FROM projected",
        model,
        execution_data=[[1]],
        execution_columns=["elapsed"],
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def _trusted_exact_formula_review_state(expression: str):
    state = _formula_with_input_projection_review_state()
    label_binding, recorded_at_binding, formula_binding = state.bindings
    formula_binding = formula_binding.model_copy(
        update={
            "expression": ExpressionRef(
                expression_id="trusted-exact-formula-expression",
                expression=expression,
            ),
            "rule_excerpt": expression,
            "validator_rule": "semantic-certificate:v1:derived_expression",
        }
    )
    formula_item = state.query_spec.semantic_items[2].model_copy(
        update={
            "exact_formula_binding_id": formula_binding.binding_id,
            "normalized_meaning": expression,
        }
    )
    return state.model_copy(
        update={
            "bindings": (label_binding, recorded_at_binding, formula_binding),
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        *state.query_spec.semantic_items[:2],
                        formula_item,
                    )
                }
            ),
        }
    )


def _requested_year_formula_review_state():
    state = _trusted_exact_formula_review_state(
        "YEAR(CURRENT_TIMESTAMP) - YEAR(recorded_at)"
    )
    return state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "requested_output_source_ids": (
                        "arithmetic-formula",
                        "arithmetic-label",
                    )
                }
            )
        }
    )


@pytest.mark.parametrize(
    ("sql", "dsn"),
    (
        (
            "SELECT r.label, YEAR(CURRENT_TIMESTAMP) - YEAR(r.recorded_at) "
            "AS elapsed FROM records r",
            "sqlite:///:memory:",
        ),
        (
            "SELECT r.label, "
            "CAST(strftime('%Y', CURRENT_TIMESTAMP) AS INTEGER) - "
            "CAST(strftime('%Y', r.recorded_at) AS INTEGER) AS elapsed "
            "FROM records r",
            "sqlite:///:memory:",
        ),
    ),
    ids=("literal_year", "sqlite_strftime_year"),
)
def test_result_review_keeps_requested_year_formula_with_requested_dimension(
    sql: str, dsn: str
) -> None:
    state = _requested_year_formula_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula retained"})

    receipt = _projection_review(
        state,
        sql,
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
        dsn=dsn,
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT r.label, "
        "CAST(strftime('%m', CURRENT_TIMESTAMP) AS INTEGER) - "
        "CAST(strftime('%Y', r.recorded_at) AS INTEGER) AS elapsed FROM records r",
        "SELECT r.label, "
        "CAST(strftime('%Y', CURRENT_TIMESTAMP) AS INTEGER) - "
        "CAST(strftime('%Y', r.label) AS INTEGER) AS elapsed FROM records r",
        "SELECT r.label, "
        "CAST(strftime('%Y', r.recorded_at) AS INTEGER) - "
        "CAST(strftime('%Y', CURRENT_TIMESTAMP) AS INTEGER) AS elapsed FROM records r",
    ),
    ids=("month", "other_input", "reverse_order"),
)
def test_result_review_rejects_nonmatching_sqlite_year_formula(
    sql: str,
) -> None:
    state = _requested_year_formula_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula retained"})

    receipt = _projection_review(
        state,
        sql,
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
        dsn="sqlite:///:memory:",
    )

    assert receipt.verdict == "contradicted"
    assert (
        receipt.deterministic_failure_code
        is CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
    )
    assert calls == 0


@pytest.mark.parametrize(
    "sql",
    (
        "SELECT r.label, 1 AS elapsed FROM records r",
        "SELECT r.label, YEAR(CURRENT_TIMESTAMP) - YEAR(r.recorded_at) AS elapsed, "
        "YEAR(CURRENT_TIMESTAMP) - YEAR(r.recorded_at) AS repeated_elapsed "
        "FROM records r",
    ),
    ids=("no_formula_annotation", "multiple_formula_annotations"),
)
def test_result_review_requires_exactly_one_formula_annotated_root_projection(
    sql: str,
) -> None:
    state = _requested_year_formula_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula retained"})

    receipt = _projection_review(
        state,
        sql,
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
        dsn="sqlite:///:memory:",
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "arithmetic-formula"
    assert (
        receipt.deterministic_failure_code
        is CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
    )
    assert calls == 0


def _exact_extremum_formula_review_state(expression: str = "MAX(score)"):
    label = _coverage_column("records", "label")
    score = _coverage_column("records", "score")
    other_score = _coverage_column("other_records", "score")
    label_evidence = _schema_evidence("extremum-label-evidence", label)
    score_evidence = _schema_evidence("extremum-score-evidence", score)
    other_score_evidence = _schema_evidence("extremum-other-score-evidence", other_score)
    formula_evidence = _document_evidence(
        "extremum-formula-evidence",
        content=f"The exact formula is {expression}.",
    )
    label_binding = PhysicalColumnBinding(
        binding_id="extremum-label-binding",
        source_id="extremum-label",
        tables=(label.table,),
        columns=(label,),
        predicates=(),
        join_path=(),
        evidence_ids=(label_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=label,
    )
    score_binding = PhysicalColumnBinding(
        binding_id="extremum-score-binding",
        source_id="extremum-score",
        tables=(score.table,),
        columns=(score,),
        predicates=(),
        join_path=(),
        evidence_ids=(score_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=score,
    )
    other_score_binding = PhysicalColumnBinding(
        binding_id="extremum-other-score-binding",
        source_id="extremum-other-score",
        tables=(other_score.table,),
        columns=(other_score,),
        predicates=(),
        join_path=(),
        evidence_ids=(other_score_evidence.evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=other_score,
    )
    formula_binding = DerivedExpressionBinding(
        binding_id="extremum-formula-binding",
        source_id="extremum-formula",
        tables=(score.table,),
        columns=(score,),
        predicates=(),
        join_path=(),
        evidence_ids=(score_evidence.evidence_id, formula_evidence.evidence_id),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="semantic-certificate:v1:derived_expression",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        expression=ExpressionRef(
            expression_id="extremum-formula-expression", expression=expression
        ),
        rule_excerpt=f"The exact formula is {expression}.",
        input_columns=(score,),
    )
    state = _coverage_state(
        item_specs=(
            ("extremum-label", True, SemanticItemStatus.RESOLVED, (label_binding.binding_id,)),
            ("extremum-score", False, SemanticItemStatus.RESOLVED, (score_binding.binding_id,)),
            (
                "extremum-other-score",
                False,
                SemanticItemStatus.RESOLVED,
                (other_score_binding.binding_id,),
            ),
            ("extremum-formula", True, SemanticItemStatus.RESOLVED, (formula_binding.binding_id,)),
        ),
        bindings=(label_binding, score_binding, other_score_binding, formula_binding),
        evidence=(
            label_evidence,
            score_evidence,
            other_score_evidence,
            formula_evidence,
        ),
    )
    label_item, score_item, other_score_item, formula_item = state.query_spec.semantic_items
    return state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        label_item,
                        score_item,
                        other_score_item,
                        formula_item.model_copy(
                            update={
                                "kind": SemanticItemKind.FORMULA,
                                "source_text": "highest score",
                                "normalized_meaning": expression,
                                "exact_formula_binding_id": formula_binding.binding_id,
                            }
                        ),
                    ),
                    "requested_output_source_ids": (
                        "extremum-formula",
                        "extremum-label",
                    ),
                }
            )
        }
    )


@pytest.mark.parametrize(
    ("trusted_expression", "sql", "expected_verdict", "expected_calls"),
    (
        (
            "MAX(score)",
            "SELECT r.label, r.score FROM records r WHERE r.score = "
            "(SELECT MAX(s.score) FROM records s)",
            "consistent",
            1,
        ),
        (
            "MAX(score)",
            "SELECT r.label, r.score FROM records r WHERE r.score = "
            "(SELECT MIN(s.score) FROM records s)",
            "contradicted",
            0,
        ),
        (
            "MAX(score)",
            "SELECT r.label, r.score FROM records r WHERE r.score = "
            "(SELECT MAX(s.alternate_score) FROM records s)",
            "contradicted",
            0,
        ),
        (
            "MAX(score)",
            "SELECT r.label, r.score FROM records r WHERE r.score = "
            "(SELECT MAX(s.score) FROM other_records s)",
            "contradicted",
            0,
        ),
        (
            "MAX(score)",
            "SELECT r.label, r.score FROM records r WHERE r.score = "
            "(SELECT 1 FROM records s ORDER BY MAX(s.score))",
            "contradicted",
            0,
        ),
        (
            "MAX(other_records.score)",
            "SELECT r.label, r.score FROM records r WHERE r.score = "
            "(SELECT MAX(s.score) FROM records s)",
            "contradicted",
            0,
        ),
    ),
    ids=(
        "matching_max_score",
        "min_instead_of_max",
        "max_of_other_column",
        "max_of_same_named_other_table",
        "max_outside_scalar_projection",
        "trusted_qualified_other_table",
    ),
)
def test_result_review_allows_exact_extremum_as_qualifying_predicate(
    trusted_expression: str, sql: str, expected_verdict: str, expected_calls: int
) -> None:
    state = _exact_extremum_formula_review_state(trusted_expression)
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "extremum predicate matches"})

    receipt = _projection_review(
        state,
        sql,
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
        documents=(f"The exact formula is {trusted_expression}.",),
        execution_data=[["label", 10]],
        execution_columns=["label", "score"],
    )

    assert receipt.verdict == expected_verdict
    assert receipt.deterministic_failure_code is (
        CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
        if expected_verdict == "contradicted"
        else None
    )
    assert calls == expected_calls


def test_result_review_defers_non_sql_exact_formula_dsl_to_model() -> None:
    expression = (
        "DIVIDE(SUBTRACT(SUM(recorded_at WHERE label = 'accepted'), "
        "SUM(recorded_at WHERE label = 'O''Brien')), "
        "MULTIPLY(SUM(recorded_at WHERE label = 'rejected'), 100))"
    )
    state = _trusted_exact_formula_review_state(expression)
    label_binding, recorded_at_binding, formula_binding = state.bindings
    formula_binding = formula_binding.model_copy(
        update={
            "columns": (
                label_binding.physical_column,
                recorded_at_binding.physical_column,
            ),
            "input_columns": (
                label_binding.physical_column,
                recorded_at_binding.physical_column,
            ),
        }
    )
    state = state.model_copy(
        update={"bindings": (label_binding, recorded_at_binding, formula_binding)}
    )
    calls = 0

    def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(prompt)
        assert "sum(case when r.label = 'accepted'" in payload["sql"].lower()
        assert "sum(case when r.label = 'o''brien'" in payload["sql"].lower()
        assert "sum(case when r.label = 'rejected'" in payload["sql"].lower()
        return json.dumps({"status": "consistent", "reason": "formula semantics match"})

    receipt = _projection_review(
        state,
        "SELECT (SUM(CASE WHEN r.label = 'accepted' THEN r.recorded_at ELSE 0 END) "
        "- SUM(CASE WHEN r.label = 'O''Brien' THEN r.recorded_at ELSE 0 END)) "
        "/ (SUM(CASE WHEN r.label = 'rejected' THEN r.recorded_at ELSE 0 END) "
        "* 100) AS ratio FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def test_result_review_defers_text_format_binding_that_is_not_the_requested_formula() -> None:
    state = _trusted_exact_formula_review_state("duration stored as H:MM:SS text")
    formula_item = state.query_spec.semantic_items[2].model_copy(
        update={"normalized_meaning": "duration in seconds"}
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        *state.query_spec.semantic_items[:2],
                        formula_item,
                    )
                }
            )
        }
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "conversion is supported"})

    receipt = _projection_review(
        state,
        "SELECT CAST(SUBSTRING(r.recorded_at, 1, 2) AS INTEGER) * 3600 AS seconds "
        "FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def test_result_review_defers_storage_format_literal_that_is_not_the_requested_formula() -> None:
    state = _trusted_exact_formula_review_state("'HH:MM:SS.mmm'")
    formula_item = state.query_spec.semantic_items[2].model_copy(
        update={"normalized_meaning": "total duration in seconds"}
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        *state.query_spec.semantic_items[:2],
                        formula_item,
                    )
                }
            )
        }
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "conversion is supported"})

    receipt = _projection_review(
        state,
        "SELECT SUM(CAST(SUBSTRING(r.recorded_at, 1, 2) AS INTEGER) * 3600) "
        "AS seconds FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def test_result_review_rejects_different_value_for_textually_exact_literal_formula() -> None:
    state = _trusted_exact_formula_review_state("'HH:MM:SS.mmm'")
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "literal is accepted"})

    receipt = _projection_review(
        state,
        "SELECT 'HH:MM:SS' AS storage_format FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "contradicted"
    assert (
        receipt.deterministic_failure_code
        is CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
    )
    assert calls == 0


@pytest.mark.parametrize(
    ("sql", "expected_verdict", "expected_calls"),
    (
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS REAL) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "consistent",
            1,
        ),
        (
            "SELECT CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) * 100 AS REAL) "
            "/ COUNT(r.recorded_at) AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS REAL) "
            "/ COUNT(r.recorded_at)) * 10 AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'rejected' THEN 1 END) AS REAL) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS REAL) "
            "/ COUNT(r.label)) * 100 AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS INTEGER) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS NUMERIC) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "consistent",
            1,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS DECIMAL) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "consistent",
            1,
        ),
        (
            "SELECT 100 * (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) AS REAL) "
            "/ COUNT(r.recorded_at)) AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 END) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 ELSE 0 END) AS REAL) "
            "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "contradicted",
            0,
        ),
        (
            "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' THEN 1 "
            "WHEN r.label = 'rejected' THEN 1 END) AS REAL) / COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
            "contradicted",
            0,
        ),
    ),
    ids=(
        "scale_after_divide",
        "scale_inside_numerator",
        "different_scale",
        "different_predicate",
        "different_denominator",
        "integer_cast",
        "postgres_numeric_cast",
        "postgres_decimal_cast",
        "reverse_factor_order",
        "missing_fractional_cast",
        "case_else",
        "multiple_case_when",
    ),
)
def test_result_review_enforces_predicate_count_formula_operator_order(
    sql: str, expected_verdict: str, expected_calls: int
) -> None:
    state = _trusted_exact_formula_review_state(
        "DIVIDE(COUNT(recorded_at < 18 AND label = 'accepted'), COUNT(recorded_at)) * 100"
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula is valid"})

    receipt = _projection_review(
        state,
        sql,
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == expected_verdict
    assert receipt.deterministic_failure_code is (
        CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
        if expected_verdict == "contradicted"
        else None
    )
    assert calls == expected_calls


@pytest.mark.parametrize(
    ("cast_type", "expected_verdict", "expected_calls"),
    (
        ("REAL", "consistent", 1),
        ("NUMERIC", "contradicted", 0),
        ("DECIMAL", "contradicted", 0),
    ),
)
def test_result_review_requires_real_affinity_for_sqlite_predicate_count_ratio(
    cast_type: str, expected_verdict: str, expected_calls: int
) -> None:
    state = _trusted_exact_formula_review_state(
        "DIVIDE(COUNT(recorded_at < 18 AND label = 'accepted'), COUNT(recorded_at)) * 100"
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula is valid"})

    receipt = _projection_review(
        state,
        "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 AND r.label = 'accepted' "
        f"THEN 1 END) AS {cast_type}) / COUNT(r.recorded_at)) * 100 AS ratio "
        "FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
        dsn="sqlite:///:memory:",
    )

    assert receipt.verdict == expected_verdict
    assert receipt.deterministic_failure_code is (
        CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
        if expected_verdict == "contradicted"
        else None
    )
    assert calls == expected_calls


@pytest.mark.parametrize(
    "expression",
    (
        "DIVIDE(SUM(recorded_at WHERE ), 100)",
        "DIVIDE(SUM(recorded_at WHERE category = ), 100)",
        "DIVIDE(SUBTRACT(SUM(recorded_at WHERE label = 'accepted'), "
        "SUM(recorded_at WHERE category = )), 100)",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted') + 1, 100)",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted'))",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted'), 100, 2)",
        "DIVIDE(SUM('WHERE label = ''accepted'''), 100)",
        "DIVIDE(CASE WHEN label = 'accepted' THEN "
        "SUM(recorded_at WHERE label = 'accepted') ELSE 0 END, 100)",
        "DIVIDE(CAST(SUM(recorded_at WHERE label = 'accepted') AS INTEGER), 100)",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted'), 100) trailing",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted'), 100) AS trailing",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted'), 100,)",
        "DIVIDE(SUM(recorded_at WHERE label = 'accepted'), 100, /* comment */)",
    ),
    ids=(
        "empty_where",
        "missing_right_operand",
        "mixed_valid_and_broken_where",
        "stray_plus",
        "divide_one_argument",
        "divide_three_arguments",
        "quoted_where_is_not_an_inline_predicate",
        "case_with_inline_predicate",
        "cast_with_inline_predicate",
        "trailing_text",
        "trailing_alias",
        "trailing_empty_argument",
        "commented_trailing_empty_argument",
    ),
)
def test_result_review_rejects_incomplete_inline_where_formula_dsl_without_model(
    expression: str,
) -> None:
    state = _trusted_exact_formula_review_state(expression)
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "model accepted formula"})

    receipt = _projection_review(
        state,
        "SELECT SUM(r.recorded_at) / 100 AS ratio FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "contradicted"
    assert (
        receipt.deterministic_failure_code
        is CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
    )
    assert calls == 0


@pytest.mark.parametrize(
    ("expression", "sql", "expected_verdict", "expected_calls"),
    (
        (
            "CURRENT_TIMESTAMP - recorded_at",
            "SELECT CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
            "consistent",
            1,
        ),
        (
            "SUBTRACT((CURRENT_TIMESTAMP, recorded_at))",
            "SELECT CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
            "consistent",
            1,
        ),
        (
            "CURRENT_TIMESTAMP - recorded_at",
            "SELECT CAST((JULIANDAY(CURRENT_TIMESTAMP) - JULIANDAY(r.recorded_at)) "
            "/ 365 AS INTEGER) AS elapsed FROM records r",
            "contradicted",
            0,
        ),
        (
            "CURRENT_TIMESTAMP -",
            "SELECT CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
            "contradicted",
            0,
        ),
        (
            "CAST((JULIANDAY(CURRENT_TIMESTAMP) - JULIANDAY(recorded_at)) "
            "/ 365 AS INTEGER)",
            "SELECT CAST((JULIANDAY(CURRENT_TIMESTAMP) - JULIANDAY(r.recorded_at)) "
            "/ 365 AS INTEGER) AS elapsed FROM records r",
            "consistent",
            1,
        ),
    ),
    ids=(
        "direct_subtraction",
        "documented_subtract_equivalence",
        "unauthorized_transform",
        "unparseable_trusted_expression",
        "trusted_transform",
    ),
)
def test_result_review_enforces_trusted_exact_formula_ast_before_model(
    expression: str,
    sql: str,
    expected_verdict: str,
    expected_calls: int,
) -> None:
    state = _trusted_exact_formula_review_state(expression)
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "model accepted formula"})

    receipt = _projection_review(
        state,
        sql,
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == expected_verdict
    assert receipt.deterministic_failure_code is (
        CheckFailureCode.FORMULA_SEMANTICS_MISMATCH
        if expected_verdict == "contradicted"
        else None
    )
    assert calls == expected_calls


@pytest.mark.parametrize(
    ("sql", "expected_verdict"),
    (
        (
            "SELECT CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
            "consistent",
        ),
        (
            "SELECT DATE_DIFF('day', r.recorded_at, CURRENT_TIMESTAMP) / 365.0 "
            "AS elapsed FROM records r",
            "contradicted",
        ),
        (
            "SELECT CURRENT_TIMESTAMP - r.replaced_at AS elapsed FROM records r",
            "contradicted",
        ),
    ),
    ids=("exact_arithmetic", "duration_conversion", "input_substitution"),
)
def test_result_review_preserves_exact_trusted_arithmetic_formula(
    sql: str, expected_verdict: str
) -> None:
    state = _exact_arithmetic_projection_review_state()
    calls = 0

    def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split()).lower()
        exact_formula = (
            payload["query_spec"]["semantic_items"][1]["kind"] == "formula"
            and payload["query_spec"]["semantic_items"][1]["normalized_meaning"]
            == "CURRENT_TIMESTAMP - recorded_at"
            and "current_timestamp - recorded_at" in str(payload["documents"]).lower()
        )
        exact_ast = (
            "current_timestamp - r.recorded_at" in payload["sql"].lower()
            and "date_diff" not in payload["sql"].lower()
            and "/ 365" not in payload["sql"].lower()
        )
        required_rule = (
            "when an exact trusted formula applies arithmetic to its confirmed inputs, "
            "the ast must retain that operator and those inputs. do not replace it with a "
            "domain calculation, duration conversion, date-difference helper, unit "
            "normalization, or rounding"
        )
        if exact_formula and required_rule in instruction and exact_ast:
            return json.dumps({"status": "consistent", "reason": "formula retained"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "exact arithmetic formula was replaced",
                "source_id": "arithmetic-formula",
            }
        )

    receipt = _projection_review(
        state,
        sql,
        model,
        documents=("The exact formula is CURRENT_TIMESTAMP - recorded_at.",),
    )

    assert receipt.verdict == expected_verdict
    assert calls == 1


def test_result_review_rejects_unrequested_display_with_requested_formula_without_model() -> None:
    state = _exact_arithmetic_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "extra display accepted"})

    receipt = _projection_review(
        state,
        "SELECT r.label, CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
        model,
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "arithmetic-label"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_rejects_unannotated_root_projection_with_requested_formula_without_model() -> None:
    state = _exact_arithmetic_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "debug output accepted"})

    receipt = _projection_review(
        state,
        "SELECT 1 AS debug_value, CURRENT_TIMESTAMP - r.recorded_at AS elapsed "
        "FROM records r",
        model,
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "arithmetic-formula"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_rejects_separate_formula_input_projection_without_model() -> None:
    state = _formula_with_input_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "formula input accepted"})

    receipt = _projection_review(
        state,
        "SELECT r.recorded_at, CURRENT_TIMESTAMP - r.recorded_at AS elapsed "
        "FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "arithmetic-recorded-at"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_rejects_computed_formula_input_projection_without_model() -> None:
    state = _formula_with_input_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "auxiliary input accepted"})

    receipt = _projection_review(
        state,
        "SELECT r.recorded_at + 0 AS auxiliary_value, "
        "CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "arithmetic-recorded-at"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_allows_only_requested_formula_without_model_shape_failure() -> None:
    state = _exact_arithmetic_projection_review_state()
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "requested formula only"})

    receipt = _projection_review(
        state,
        "SELECT CURRENT_TIMESTAMP - r.recorded_at AS elapsed FROM records r",
        model,
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def test_result_review_rejects_authenticated_unrequested_root_projection_without_model() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity",)
    )
    sql = (
        "SELECT i.id, SUM(i.amount) AS total FROM items i "
        "GROUP BY i.id ORDER BY total ASC"
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "ignored output role"})

    receipt = _projection_review(state, sql, model)

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "projection-total"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_rejects_root_aggregate_for_dimension_output_without_model() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity",)
    )
    entity_item, condition_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        entity_item,
                        condition_item.model_copy(
                            update={
                                "kind": SemanticItemKind.FILTER,
                                "source_text": "positive amount",
                                "normalized_meaning": "amount is positive",
                            }
                        ),
                    )
                }
            )
        }
    )
    sql = (
        "SELECT i.id, COUNT(*) AS total FROM items i "
        "WHERE i.amount > 0 GROUP BY i.id"
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "aggregate accepted"})

    receipt = _projection_review(
        state,
        sql,
        model,
        execution_data=[[1, 2]],
        execution_columns=["id", "total"],
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "projection-entity"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_rejects_combined_requested_dimensions_without_model() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity", "projection-total")
    )
    entity_item, total_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        entity_item,
                        total_item.model_copy(
                            update={
                                "kind": SemanticItemKind.DIMENSION,
                                "source_text": "item amount label",
                                "normalized_meaning": "item amount label",
                            }
                        ),
                    )
                }
            )
        }
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "combined output accepted"})

    receipt = _projection_review(
        state,
        "SELECT i.id || i.amount AS combined FROM items i",
        model,
        execution_data=[["1-10"]],
        execution_columns=["combined"],
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "projection-entity"
    assert receipt.deterministic_failure_code is CheckFailureCode.RESULT_SHAPE_MISMATCH
    assert calls == 0


def test_result_review_allows_separate_requested_dimension_projections() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity", "projection-total")
    )
    entity_item, total_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        entity_item,
                        total_item.model_copy(
                            update={
                                "kind": SemanticItemKind.DIMENSION,
                                "source_text": "item amount label",
                                "normalized_meaning": "item amount label",
                            }
                        ),
                    )
                }
            )
        }
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "separate outputs"})

    receipt = _projection_review(
        state,
        "SELECT i.id, i.amount FROM items i",
        model,
        execution_data=[[1, 10]],
        execution_columns=["id", "amount"],
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def test_result_review_allows_root_aggregate_for_requested_metric() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity",)
    )
    entity_item, condition_item = state.query_spec.semantic_items
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (
                        entity_item.model_copy(
                            update={
                                "kind": SemanticItemKind.METRIC,
                                "source_text": "record count",
                                "normalized_meaning": "count of records",
                            }
                        ),
                        condition_item,
                    )
                }
            )
        }
    )
    sql = "SELECT COUNT(i.id) AS record_count FROM items i"
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "requested metric"})

    receipt = _projection_review(
        state,
        sql,
        model,
        execution_data=[[2]],
        execution_columns=["record_count"],
    )

    assert receipt.verdict == "consistent"
    assert receipt.deterministic_failure_code is None
    assert calls == 1


def test_result_review_requires_separate_values_for_separately_requested_metrics() -> None:
    state = _projection_review_state(requested_output_source_ids=("projection-total",))
    amount = _coverage_column("items", "amount")
    period_binding = PhysicalColumnBinding(
        binding_id="projection-period-total-binding",
        source_id="projection-total-period",
        tables=(amount.table,),
        columns=(amount,),
        predicates=(),
        join_path=(),
        evidence_ids=("projection-total-evidence",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        physical_column=amount,
    )
    period_item = SemanticItem(
        source_id="projection-total-period",
        kind=SemanticItemKind.METRIC,
        source_text="period total",
        normalized_meaning="total for the requested period",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(period_binding.binding_id,),
    )
    state = state.model_copy(
        update={
            "bindings": (*state.bindings, period_binding),
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (*state.query_spec.semantic_items, period_item),
                    "requested_output_source_ids": (
                        "projection-total",
                        "projection-total-period",
                    ),
                }
            ),
        }
    )
    sql = "SELECT SUM(i.amount) AS total FROM items i"
    calls = 0

    def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        requested = payload["query_spec"]["requested_output_source_ids"]
        if (
            "exactly one combined value" in instruction
            and len(requested) == 2
            and len(payload["ast"]["projections"]) == 1
            and len(payload["columns"]) == 1
            and len(payload["data"]) == 1
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "one aggregate cannot return two separately requested metrics",
                    "source_id": "projection-total-period",
                }
            )
        return json.dumps({"status": "consistent", "reason": "outputs are present"})

    receipt = _projection_review(
        state,
        sql,
        model,
        execution_data=[[80]],
        execution_columns=["combined_total"],
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "projection-total-period"
    assert receipt.deterministic_failure_code is None

    grouped_receipt = _projection_review(
        state,
        "SELECT SUM(i.amount) AS total FROM items i GROUP BY i.id",
        model,
        execution_data=[[10], [20]],
        execution_columns=["group_total"],
    )

    assert grouped_receipt.verdict == "consistent"
    assert grouped_receipt.deterministic_failure_code is None
    assert calls == 2


def test_result_review_does_not_require_non_output_grouping_dimension_projection() -> None:
    state = _projection_review_state(requested_output_source_ids=("projection-total",))
    sql = "SELECT SUM(i.amount) AS total FROM items i GROUP BY i.id"

    def model(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        requested = payload["query_spec"]["requested_output_source_ids"]
        if (
            "Only semantic items listed in requested_output_source_ids must be projected"
            in instruction
            and requested == ["projection-total"]
        ):
            return json.dumps(
                {
                    "status": "consistent",
                    "reason": "grouping dimension is used but not requested as output",
                }
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "required grouping dimension is missing from output",
                "source_id": "projection-entity",
            }
        )

    receipt = _projection_review(state, sql, model)

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None


def test_result_review_leaves_incomplete_root_projection_annotations_to_model() -> None:
    state = _projection_review_state(requested_output_source_ids=())
    formula_evidence = _document_evidence(
        "projection-formula-evidence", content="The reported value is amount plus one."
    )
    formula_binding = DerivedExpressionBinding(
        binding_id="projection-formula-binding",
        source_id="projection-formula",
        tables=(_coverage_column("items", "id").table,),
        columns=(
            _coverage_column("items", "id"),
            _coverage_column("items", "amount"),
        ),
        predicates=(),
        join_path=(),
        evidence_ids=(
            "projection-entity-evidence",
            "projection-total-evidence",
            formula_evidence.evidence_id,
        ),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        expression=ExpressionRef(
            expression_id="projection-formula-expression", expression="id + amount"
        ),
        rule_excerpt="The reported value is amount plus one.",
        input_columns=(
            _coverage_column("items", "id"),
            _coverage_column("items", "amount"),
        ),
    )
    formula_item = SemanticItem(
        source_id="projection-formula",
        kind=SemanticItemKind.FORMULA,
        source_text="reported value",
        normalized_meaning="amount plus one",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(formula_binding.binding_id,),
    )
    state = state.model_copy(
        update={
            "bindings": (*state.bindings, formula_binding),
            "evidence": (*state.evidence, formula_evidence),
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (*state.query_spec.semantic_items, formula_item),
                    "requested_output_source_ids": ("projection-formula",),
                }
            ),
        }
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "model review"})

    receipt = _projection_review(
        state,
        "SELECT SUM(i.amount) AS total FROM items i",
        model,
        document_sources=(
            DocumentSourceState(
                document_id="coverage-document",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v1",
            ),
        ),
    )

    assert receipt.verdict == "consistent"
    assert calls == 1


def test_result_review_prompt_includes_required_unbound_formula() -> None:
    state = _projection_review_state(
        requested_output_source_ids=("projection-entity", "projection-total")
    )
    formula_item = SemanticItem(
        source_id="projection-formula",
        kind=SemanticItemKind.FORMULA,
        source_text="converted recorded measurement",
        normalized_meaning="convert the recorded text measurement into seconds",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "semantic_items": (*state.query_spec.semantic_items, formula_item)
                }
            )
        }
    )
    captured: dict[str, object] = {}

    def model(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"status": "consistent", "reason": "model review"})

    receipt = _projection_review(
        state,
        "SELECT i.id, SUM(i.amount) AS total FROM items i GROUP BY i.id",
        model,
    )

    assert receipt.verdict == "consistent"
    formulas = [
        item
        for item in captured["query_spec"]["semantic_items"]
        if item["kind"] == "formula"
    ]
    assert formulas == [formula_item.model_dump(mode="json")]
    instruction = " ".join(captured["instruction"].split())
    assert "Compare the SQL with every required semantic item in QuerySpec" in instruction
    assert "different physical column or precomputed value" in instruction
    assert "schema descriptions do not override the required computation" in instruction
    assert "takes precedence over a selected physical binding for that metric" in instruction
    assert "Distinguish selected bindings from columns actually referenced by the SQL AST" in instruction
    assert "unless the AST references that column" in instruction
    assert "different trusted input column" in instruction
    assert "use the supplied binding whose column substituted for the formula" in instruction
    assert "repair_kind must be null because the computation, not the physical binding, is wrong" in instruction
    assert "omits or fails to apply a required semantic item" in instruction
    assert "the selected physical binding is correct" in instruction
    assert "ranked top N" in instruction
    assert "do not require an additional outer MIN or MAX" in instruction
    assert "Preserve the ORDER BY and LIMIT N" in instruction
    assert (
        "repair_kind must be null because the sql, not the binding, is wrong"
        in instruction.lower()
    )
    assert "suffix identified as integer milliseconds" in instruction
    assert "divided by 1000" in instruction
    assert "decimal fractional digits" in instruction
    assert "conditional entity output" in instruction
    assert "must be implemented in the SELECT projection with CASE or IIF" in instruction
    assert "textual absence marker rather than SQL NULL" in instruction
    assert "must not be moved to WHERE" in instruction


@pytest.mark.parametrize(
    ("requested_output_source_ids", "sql"),
    (
        (
            ("projection-entity", "projection-total"),
            "SELECT i.id, SUM(i.amount) AS total FROM items i GROUP BY i.id ORDER BY total ASC",
        ),
        (
            ("projection-entity",),
            "SELECT i.id FROM items i GROUP BY i.id ORDER BY SUM(i.amount) ASC",
        ),
        (
            ("projection-entity",),
            "SELECT i.id + SUM(i.amount) AS mixed FROM items i GROUP BY i.id",
        ),
        (
            ("projection-entity",),
            "SELECT CURRENT_DATE AS current_value FROM items i",
        ),
    ),
)
def test_result_review_leaves_requested_or_unproven_root_projection_to_model(
    requested_output_source_ids: tuple[str, ...], sql: str
) -> None:
    state = _projection_review_state(
        requested_output_source_ids=requested_output_source_ids
    )
    calls = 0

    def model(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return json.dumps({"status": "consistent", "reason": "model review"})

    receipt = _projection_review(state, sql, model)

    assert receipt.verdict == "consistent"
    assert calls == 1


def test_terminal_normalizes_short_reason_result_review(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=True)
    reason = "r" * 542
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "status": "consistent",
                "short_reason": reason,
                "source_id": None,
                "repair_kind": None,
                "repair_binding_id": None,
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=model,
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert result["result_review"]["verdict"] == "consistent"
    assert result["result_review"]["reason"] == reason
    assert result["result_review"]["candidate_id"] == candidate.candidate_id
    assert calls == ["executor", "audit", "persistence"]
    instruction = json.loads(prompts[0])["instruction"]
    assert (
        "Return only JSON object with exactly these keys: status, reason, source_id, "
        "repair_kind, repair_binding_id, predicate_authority" in instruction
    )
    assert "short_reason" not in instruction


def test_typed_terminal_fails_closed_when_result_review_is_missing(monkeypatch) -> None:
    state, _, _, _ = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    token = set_tool_runtime_context({RESULT_REVIEW_REQUIRED_RUNTIME_KEY: True})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["reason_code"] == "RESULT_REVIEW_FAILED"
    assert calls == ["executor", "audit"]


def test_consistent_result_review_accepts_null_reason(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=True)
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: json.dumps(
            {
                "status": "consistent",
                "reason": None,
                "source_id": None,
                "repair_kind": None,
                "repair_binding_id": None,
                "predicate_authority": None,
            }
        ),
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "succeeded"
    assert result["result_review"]["verdict"] == "consistent"
    assert result["result_review"]["reason"] == "result is consistent"
    assert calls == ["executor", "audit", "persistence"]


@pytest.mark.parametrize(
    "response",
    (
        "not json",
        json.dumps(
            {
                "status": "consistent",
                "reason": "result matches request",
                "short_reason": "result matches request",
                "source_id": None,
                "repair_kind": None,
                "repair_binding_id": None,
            }
        ),
    ),
)
def test_malformed_result_review_returns_durable_no_target_receipt(
    monkeypatch, response
) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: response,
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "malformed"
    assert result["source_id"] is None
    assert result["evidence_id"] is None
    assert calls == ["executor", "audit"]


def test_expired_result_review_returns_durable_no_target_receipt(
    monkeypatch,
) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=FreshnessContext(
            evaluated_at=state.evidence[0].observed_at,
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            schema_namespace_version=state.schema_namespace_version,
        ),
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: (_ for _ in ()).throw(
            WorkflowDeadlineExceeded("expired before result review call")
        ),
    )
    token = set_tool_runtime_context(
        {RESULT_VALIDATION_RUNTIME_KEY: validator, RESULT_REVIEW_RUNTIME_KEY: review}
    )
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "timeout"
    assert result["source_id"] is None
    assert result["evidence_id"] is None
    assert calls == ["executor", "audit"]


@pytest.mark.parametrize(
    ("verdict", "source_id", "evidence_id"),
    (
        ("consistent", "source-1", "terminal-result-validation-not-null"),
        ("contradicted", None, None),
        ("ambiguous", "source-1", None),
        ("malformed", "source-1", "terminal-result-validation-not-null"),
        ("timeout", None, "terminal-result-validation-not-null"),
    ),
)
def test_result_review_receipt_rejects_forged_reentry_targets(
    verdict, source_id, evidence_id
) -> None:
    state, requirements, candidate, _ = _case()

    with pytest.raises(ValueError):
        ResultReviewReceipt(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            research_state_revision=state.revision,
            candidate_id=candidate.candidate_id,
            normalized_ast_digest=candidate.normalized_ast_digest,
            requirements_digest=requirements.requirements_digest,
            source_id=source_id,
            evidence_id=evidence_id,
            verdict=verdict,
            reason="review outcome",
            execution=_executor_result([["paid"]]),
            deterministic_failure_code=None,
        )


def test_result_review_receipt_requires_deterministic_failure_code() -> None:
    state, requirements, candidate, _ = _case()

    with pytest.raises(ValueError):
        ResultReviewReceipt(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            research_state_revision=state.revision,
            candidate_id=candidate.candidate_id,
            normalized_ast_digest=candidate.normalized_ast_digest,
            requirements_digest=requirements.requirements_digest,
            source_id=None,
            evidence_id=None,
            verdict="consistent",
            reason="review outcome",
            execution=_executor_result([["paid"]]),
        )


def test_result_review_receipt_preserves_typed_predicate_authority() -> None:
    state, requirements, candidate, _ = _case()
    predicate = PredicateRef(
        left=_column(),
        operator=PredicateOperator.EQ,
        right="active",
    )

    receipt = ResultReviewReceipt(
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        research_state_revision=state.revision,
        candidate_id=candidate.candidate_id,
        normalized_ast_digest=candidate.normalized_ast_digest,
        requirements_digest=requirements.requirements_digest,
        source_id="source-1",
        evidence_id="terminal-result-validation-not-null",
        verdict="contradicted",
        reason="exact status evidence is required",
        execution=_executor_result([["active"]]),
        deterministic_failure_code=None,
        predicate_authority=predicate,
    )

    assert receipt.predicate_authority == predicate


def test_result_review_arbitration_selects_only_an_executed_grain_candidate() -> None:
    state, requirements, candidate, _ = _case()
    distinct_sql = "SELECT DISTINCT o.status FROM orders o"
    distinct_parsed = parse_sql_candidate(
        distinct_sql, POSTGRES_DSN, "terminal-distinct-candidate"
    )
    distinct_candidate = SqlCandidate(
        candidate_id="terminal-distinct-candidate",
        sql=distinct_sql,
        normalized_ast_digest=distinct_parsed.candidate_digest,
        revision=state.revision,
    )

    def contradicted_receipt(candidate, requirement):
        return ResultReviewReceipt(
            run_id=state.run_id,
            run_incarnation=state.run_incarnation,
            research_state_revision=state.revision,
            candidate_id=candidate.candidate_id,
            normalized_ast_digest=candidate.normalized_ast_digest,
            requirements_digest=requirements.requirements_digest,
            source_id="source-1",
            evidence_id="terminal-result-validation-not-null",
            verdict="contradicted",
            reason="the candidates require opposite row grains",
            execution={**_executor_result([["paid"]]), "sql_query": candidate.sql},
            deterministic_failure_code=None,
            row_grain_requirement=requirement,
        )

    preserve = contradicted_receipt(candidate, "preserve_qualifying_rows")
    deduplicate = contradicted_receipt(distinct_candidate, "deduplicate_entity")

    def arbiter(prompt: str) -> str:
        payload = json.loads(prompt)
        assert [item["candidate_id"] for item in payload["candidates"]] == [
            candidate.candidate_id,
            distinct_candidate.candidate_id,
        ]
        assert "Do not generate, rewrite, execute SQL" in payload["instruction"]
        return json.dumps({"status": "resolve", "candidate_id": candidate.candidate_id})

    resolution = evaluate_result_review_arbitration_capability(
        create_result_review_arbitration_capability(
            state=state,
            requirements=requirements,
            candidates=(candidate, distinct_candidate),
            receipts=(preserve, deduplicate),
            documents=(),
            model=arbiter,
        )
    )

    assert resolution is not None
    assert resolution.review_kind == "conflict_arbitration"
    assert resolution.verdict == "consistent"
    assert resolution.candidate_id == candidate.candidate_id
    assert resolution.execution == preserve.execution




def test_terminal_fails_closed_for_invalid_result_validation_capability(monkeypatch) -> None:
    state, _, _, _ = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=False)
    token = set_tool_runtime_context({RESULT_VALIDATION_RUNTIME_KEY: object()})
    try:
        result = _finalize(state.run_id)
    finally:
        reset_tool_runtime_context(token)

    assert result["status"] == "failed"
    assert result["reason_code"] == "RESULT_RECONCILIATION_FAILED"
    assert result["persistence"]["status"] == "error"
    assert len(result["persistence"]["error"]) <= 512
    assert calls == ["executor", "audit"]
