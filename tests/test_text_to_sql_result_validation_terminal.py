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
    ExpressionRef,
    PhysicalColumnBinding,
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
    create_result_review_capability,
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
from text_to_sql_semantic_checks_helpers import ItemSpec, POSTGRES_DSN, build_state
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
    assert "winning alternative label or role" in prompt["instruction"].lower()


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
                        "Which entity had the lowest metric over the reporting period?"
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
        assert payload["question"] == "Which entity had the lowest metric over the reporting period?"
        assert not payload["ast"]["aggregates"]
        assert not payload["ast"]["groupings"]
        assert payload["evidence"]
        assert payload["documents"] == [
            "Each row is a raw observation for one entity and one period."
        ]
        assert payload["columns"] == ["entity", "period", "metric"]
        assert payload["data"] == [["entity-a", "period-1", 4]]
        if "cannot be consistent" not in payload["instruction"]:
            return json.dumps({"status": "consistent", "reason": "raw row accepted"})
        return json.dumps(
            {
                "status": "ambiguous",
                "reason": "trusted evidence does not resolve the requested computation",
                "source_id": "source-1",
            }
        )

    review = create_result_review_capability(
        state=period_state,
        requirements=period_requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("Each row is a raw observation for one entity and one period.",),
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

    assert receipt.verdict == "ambiguous"
    assert receipt.source_id == "source-1"
    assert "entity-level computation over a period" in prompts[0]["instruction"]
    assert "extremal raw observation" in prompts[0]["instruction"]
    assert "cannot be consistent" in prompts[0]["instruction"]

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
        documents=("Each row is a raw observation for one entity and one period.",),
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


def _projection_review(state, sql: str, model, *, document_sources=()):
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-output-role")
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
        documents=(),
        model=model,
    )
    return evaluate_result_review_capability(
        capability,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            "success": True,
            "data": [[1, 10]],
            "columns": ["id", "total"],
            "rows_affected": 1,
            "execution_time_ms": 1,
            "error_message": None,
            "dry_run_only": False,
            "skipped_execution": False,
            "sql_query": sql,
            "applied_row_limit": 10,
        },
    )


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


def test_terminal_durably_binds_consistent_model_review(monkeypatch) -> None:
    state, requirements, candidate, validator = _case()
    calls = _terminal_side_effects(monkeypatch, [["paid"]], persistence_allowed=True)
    reason = "r" * 542
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
        model=lambda _prompt: json.dumps({"status": "consistent", "reason": reason}),
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


def test_malformed_result_review_returns_durable_no_target_receipt(monkeypatch) -> None:
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
        model=lambda _prompt: "not json",
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
