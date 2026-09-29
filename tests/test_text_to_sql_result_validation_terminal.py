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
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest, canonical_json_bytes
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


def test_terminal_uses_the_only_allowed_source_for_model_review(monkeypatch) -> None:
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
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "candidate uses the wrong aggregate input",
                "source_id": "truncated-source-id",
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

    assert result["record_kind"] == "text2sql_result_review"
    assert result["verdict"] == "contradicted"
    assert result["source_id"] == "source-1"
    assert calls == ["executor", "audit"]


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
    assert (
        "each required FILTER or TIME predicate that defines its population must apply "
        "to every numerator and denominator term" in prompts[0]["instruction"]
    )
    assert "explicitly requires separate populations" in prompts[0]["instruction"]

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


def test_result_review_accepts_grouped_grain_uniqueness_certificate() -> None:
    state, requirements, _, _ = _case()
    sql = (
        "SELECT actor.status FROM orders actor "
        "JOIN orders event ON event.status = actor.status "
        "JOIN orders record ON record.status = event.status "
        "WHERE event.status = 'qualified' "
        "GROUP BY actor.status "
        "HAVING COUNT(*) = COUNT(DISTINCT actor.status)"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-grouped-grain-certificate")
    candidate = SqlCandidate(
        candidate_id="terminal-grouped-grain-certificate",
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
    rule = (
        "Within one already-qualified WHERE/JOIN group, HAVING COUNT(*) = COUNT(DISTINCT "
        "grain_key) certifies exactly one observed non-NULL row per grain_key; do not reject "
        "it using hypothetical rows outside that qualifying rowset. It proves neither external "
        "or global completeness, another key, nor an explicitly different QuerySpec/document "
        "grain or distinct requirement."
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        if rule not in instruction:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the grouped actor output might hide unrelated records",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the qualifying actor/event/record rowset is certified per actor",
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
        execution={
            **_executor_result([["actor-a"]]),
            "columns": ["actor"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


def test_result_review_scopes_universal_child_condition_to_required_filter() -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Return each actor for whom every observed child record meets the "
                        "required condition among child records with score at least 10."
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
    sql = (
        "SELECT actor.status FROM orders actor "
        "JOIN orders child ON child.status = actor.status "
        "WHERE child.score >= 10 "
        "GROUP BY actor.status "
        "HAVING COUNT(*) = COUNT(CASE WHEN child.status = 'qualified' THEN 1 END)"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-universal-filter-scope")
    candidate = SqlCandidate(
        candidate_id="terminal-universal-filter-scope",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    rule = (
        "When an explicit universal child quantifier has a required FILTER or TIME on that same "
        "child relation, that qualifying filter forms its observed child universe; do not require "
        "children outside it unless the question, QuerySpec, or trusted context explicitly requests "
        "the full universe."
    )

    def reviewer(prompt: str) -> str:
        if rule not in json.loads(prompt)["instruction"]:
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the condition must include child records outside the required filter",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the universal condition is evaluated at root grain within its filter",
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
        execution={
            **_executor_result([["actor-a"]]),
            "columns": ["actor"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


@pytest.mark.parametrize(
"sql",
    (
        (
            "SELECT actor.status FROM orders actor "
            "JOIN orders child ON child.status = actor.status "
            "WHERE child.status = 'qualified' "
            "GROUP BY actor.status, child.status HAVING COUNT(*) = 1"
        ),
        (
            "SELECT actor.status FROM orders actor "
            "JOIN orders child ON child.status = actor.status "
            "WHERE child.status = 'qualified' "
            "GROUP BY actor.status HAVING COUNT(*) = 1"
        ),
    ),
    ids=("child_grain", "prefiltered_root_grain"),
)
def test_result_review_rejects_child_grain_for_universal_root_condition(sql: str) -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Return each actor for whom every observed child record is qualified."
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
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-universal-root-grain")
    candidate = SqlCandidate(
        candidate_id="terminal-universal-root-grain",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    rule = (
        "An explicit universal child quantifier must be enforced at the requested root grain; "
        "grouping or checking each returned root+child group does not prove all children, and "
        "child groups cannot substitute for the root result. Return contradicted when SQL merely "
        "selects or retains qualifying children without proving absence of violating observed children, "
        "whether result rows are at root or child grain. Do not apply this when the question "
        "explicitly requests child groups or pairs."
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        if rule not in instruction:
            return json.dumps(
                {
                    "status": "consistent",
                    "reason": "each returned actor/child group has one qualified child",
                }
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the child grouping does not prove every child for an actor",
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
        execution={
            **_executor_result([["actor-a"]]),
            "columns": ["actor"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"


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
        if not all(
            clause in instruction
            for clause in (
                "ratio or percentage over entities",
                "deduplicate only when the question, queryspec, or trusted formula explicitly requires unique, distinct, or entity-once counting",
                "one-to-many relationship or an entity name alone does not add that requirement",
                "trusted schema or evidence confirms",
                "alternative endpoint rows",
                "entity-relationship pair once",
            )
        ):
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


def test_result_review_rejects_formula_operands_multiplied_across_child_tables() -> None:
    state, requirements, _, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    sql = (
        "SELECT CAST(SUM(p.amount) AS REAL) / COUNT(i.item_id) "
        "FROM orders o JOIN payments p ON p.order_id = o.order_id "
        "JOIN line_items i ON i.order_id = o.order_id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-child-fanout-review")
    candidate = SqlCandidate(
        candidate_id="terminal-child-fanout-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        required = (
            "different one-to-many child relations",
            "joining those children before aggregation multiplies both row populations",
            "compute each aggregate in its own child scope",
        )
        if not all(clause in instruction for clause in required):
            return json.dumps({"status": "consistent", "reason": "formula accepted"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the join multiplies payments by line items before both aggregates",
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
            "The ratio divides the sum of payment amounts by the count of line item IDs.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[2.0]]),
            "columns": ["ratio"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert receipt.row_grain_requirement is None


def test_result_review_rejects_measured_child_rows_multiplied_by_qualifying_sibling() -> None:
    item_join = (inner_join("orders", "order_id", "items", "order_id"),)
    inspection_join = (inner_join("orders", "order_id", "inspections", "order_id"),)
    state = build_state(
        (
            ItemSpec(
                source_id="measured-items",
                kind=SemanticItemKind.METRIC,
                table="items",
                column="item_id",
                join_path=item_join,
            ),
            ItemSpec(
                source_id="completed-inspection",
                kind=SemanticItemKind.FILTER,
                table="inspections",
                column="status",
                operator=PredicateOperator.EQ,
                literal="completed",
                join_path=inspection_join,
            ),
        ),
        shape=ExpectedResultShape.SCALAR,
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "How many items belong to orders with a completed inspection?"
                    ),
                    "requested_output_source_ids": ("measured-items",),
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
        "SELECT COUNT(i.item_id) FROM orders o "
        "JOIN items i ON i.order_id = o.order_id "
        "JOIN inspections s ON s.order_id = o.order_id "
        "WHERE s.status = 'completed'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-qualifying-sibling-fanout")
    candidate = SqlCandidate(
        candidate_id="terminal-qualifying-sibling-fanout",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split()).lower()
        required_rule = (
            "an aggregate measures rows of one child relation and a separate child relation "
            "only qualifies their common parent"
        )
        measured_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "measured-items"
        )
        qualifying_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "completed-inspection"
        )
        fixture_is_consistent = (
            measured_binding["physical_column"]["table"]["table"] == "items"
            and measured_binding["physical_column"]["column"] == "item_id"
            and measured_binding["join_path"]
            and qualifying_binding["kind"] == "discriminator_value"
            and qualifying_binding["discriminator_column"]["table"]["table"]
            == "inspections"
            and qualifying_binding["discriminator_predicate"]["right"] == "completed"
            and qualifying_binding["join_path"]
            and "orders" in str(payload["bindings"])
            and "items" in payload["sql"]
            and "inspections" in payload["sql"]
        )
        if required_rule not in instruction or not fixture_is_consistent:
            return json.dumps({"status": "consistent", "reason": "the count executed"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "completed inspections multiply the measured item rows",
                "source_id": "measured-items",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Items and inspections are separate one-to-many children of orders; "
            "completed inspections qualify orders, while the count measures item rows.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[4]]),
            "columns": ["item_count"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "measured-items"


@pytest.mark.parametrize(
    ("direct_participation", "expected_verdict"),
    ((False, "contradicted"), (True, "consistent")),
    ids=("measured_child_population", "explicit_direct_participation_rows"),
)
def test_result_review_reconciles_association_path_with_requested_population(
    direct_participation: bool, expected_verdict: str
) -> None:
    association_join = (
        inner_join("specimens", "specimen_id", "specimen_screening_links", "specimen_id"),
        inner_join(
            "specimen_screening_links",
            "screening_id",
            "screenings",
            "screening_id",
        ),
    )
    state = build_state(
        (
            ItemSpec(
                source_id="measured-specimens",
                kind=SemanticItemKind.METRIC,
                table="specimens",
                column="specimen_id",
                join_path=association_join,
            ),
            ItemSpec(
                source_id="accepted-screening",
                kind=SemanticItemKind.FILTER,
                table="screenings",
                column="outcome",
                operator=PredicateOperator.EQ,
                literal="accepted",
                join_path=association_join,
            ),
        ),
        shape=ExpectedResultShape.SCALAR,
    )
    question = (
        "How many direct specimen-screening participation rows have an accepted screening?"
        if direct_participation
        else "How many specimens belong to collections with an accepted screening?"
    )
    formula = (
        "COUNT(specimen_screening_links.specimen_id) over direct specimen-screening "
        "relationship/detail rows"
        if direct_participation
        else "COUNT(specimens.specimen_id) for collections with accepted screenings"
    )
    measured_item = state.query_spec.semantic_items[0].model_copy(
        update={"normalized_meaning": formula}
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": question,
                    "semantic_items": (
                        measured_item,
                        state.query_spec.semantic_items[1],
                    ),
                    "requested_output_source_ids": ("measured-specimens",),
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
    counted_column = "l.specimen_id" if direct_participation else "s.specimen_id"
    sql = (
        f"SELECT COUNT({counted_column}) FROM collections c "
        "JOIN specimens s ON s.collection_id = c.collection_id "
        "JOIN specimen_screening_links l ON l.specimen_id = s.specimen_id "
        "JOIN screenings q ON q.screening_id = l.screening_id "
        "WHERE q.outcome = 'accepted'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-association-population")
    candidate = SqlCandidate(
        candidate_id="terminal-association-population",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split()).lower()
        measured_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "measured-specimens"
        )
        qualifying_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "accepted-screening"
        )
        shared_parent_roles = (
            measured_binding["physical_column"]["table"]["table"] == "specimens"
            and "specimen_screening_links" in str(measured_binding["join_path"])
            and qualifying_binding["discriminator_column"]["table"]["table"]
            == "screenings"
            and "specimen_screening_links" in str(qualifying_binding["join_path"])
            and "specimen_screening_links" in str(payload["ast"])
            and "collections with accepted screenings" in str(payload["query_spec"])
            and "specimens and screenings independently belong to collections"
            in str(payload["documents"]).lower()
        )
        required_rule = (
            "even if selected bindings no longer retain the earlier independent child-to-parent "
            "paths"
        )
        audit_rule = "a matching audit issue is additional evidence to examine, not semantic authority"
        multiset_rule = "preserve the qualifying join-row multiset"
        direct_boundary = "explicitly requested direct participation or relationship/detail rows"
        if not (
            shared_parent_roles
            and required_rule in instruction
            and audit_rule in instruction
            and multiset_rule in instruction
        ):
            return json.dumps({"status": "consistent", "reason": "the association path is selected"})
        if direct_participation:
            if direct_boundary not in instruction:
                return json.dumps(
                    {
                        "status": "contradicted",
                        "reason": "the explicit direct-participation population was rejected",
                        "source_id": "measured-specimens",
                    }
                )
            return json.dumps(
                {"status": "consistent", "reason": "direct participation rows are requested"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the association path replaced measured specimens of qualifying collections",
                "source_id": "measured-specimens",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Trusted schema roles: specimens and screenings independently belong to collections; "
            "link rows record direct specimen-screening participation. Trusted formula: " + formula,
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[5]]),
            "advisory_issues": [
                {
                    "issue_type": "LLM_logic",
                    "description": "The association path changes the measured population.",
                    "blocking": False,
                }
            ],
            "columns": ["specimen_count"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == expected_verdict
    assert receipt.source_id == (
        "measured-specimens" if expected_verdict == "contradicted" else None
    )


def test_result_review_keeps_population_contradiction_primary_before_distinct_multiset() -> None:
    association_join = (
        inner_join("specimens", "specimen_id", "specimen_screening_links", "specimen_id"),
        inner_join(
            "specimen_screening_links",
            "screening_id",
            "screenings",
            "screening_id",
        ),
    )
    state = build_state(
        (
            ItemSpec(
                source_id="measured-specimen-id",
                kind=SemanticItemKind.DIMENSION,
                table="specimens",
                column="specimen_id",
                join_path=association_join,
            ),
            ItemSpec(
                source_id="accepted-screening",
                kind=SemanticItemKind.FILTER,
                table="screenings",
                column="outcome",
                operator=PredicateOperator.EQ,
                literal="accepted",
                join_path=association_join,
            ),
        )
    )
    measured_binding = state.bindings[0]
    assert isinstance(measured_binding, PhysicalColumnBinding)
    formula = (
        "COUNT(specimens.specimen_id WHERE collection has accepted screening) * 100 "
        "/ COUNT(specimens.specimen_id)"
    )
    formula_evidence = _document_evidence(
        "measured-population-formula-evidence",
        content="The exact requested formula is " + formula + ".",
    )
    formula_binding = DerivedExpressionBinding(
        binding_id="measured-population-formula-binding",
        source_id="measured-population",
        tables=measured_binding.tables,
        columns=measured_binding.columns,
        predicates=(),
        join_path=measured_binding.join_path,
        evidence_ids=(
            measured_binding.evidence_ids[0],
            formula_evidence.evidence_id,
        ),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="semantic-certificate:v1:derived_expression",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        expression=ExpressionRef(
            expression_id="measured-population-formula-expression",
            expression=formula,
        ),
        rule_excerpt="The exact requested formula is " + formula + ".",
        input_columns=measured_binding.columns,
    )
    formula_item = SemanticItem(
        source_id="measured-population",
        kind=SemanticItemKind.FORMULA,
        source_text="percentage of specimens",
        normalized_meaning=formula,
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(formula_binding.binding_id,),
        exact_formula_binding_id=formula_binding.binding_id,
    )
    state = state.model_copy(
        update={
            "bindings": (*state.bindings, formula_binding),
            "evidence": (*state.evidence, formula_evidence),
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "What percentage of specimens belong to collections with an accepted screening?",
                    "semantic_items": (*state.query_spec.semantic_items, formula_item),
                    "requested_output_source_ids": ("measured-population",),
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
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    sql = (
        "WITH associated_specimens AS ("
        "SELECT DISTINCT l.specimen_id FROM specimen_screening_links l "
        "JOIN screenings q ON q.screening_id = l.screening_id "
        "WHERE q.outcome = 'accepted'"
        ") SELECT COUNT(a.specimen_id) * 100.0 / COUNT(s.specimen_id) AS specimen_percentage "
        "FROM specimens s JOIN associated_specimens a ON a.specimen_id = s.specimen_id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-distinct-association")
    candidate = SqlCandidate(
        candidate_id="terminal-distinct-association",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split()).lower()
        fixture_is_present = (
            "select distinct l.specimen_id" in payload["sql"].lower()
            and "collections with an accepted screening" in str(payload["query_spec"]).lower()
            and payload["query_spec"]["requested_output_source_ids"] == ["measured-population"]
            and any(
                item["source_id"] == "measured-population"
                and item["kind"] == "formula"
                and "count(specimens.specimen_id" in item["normalized_meaning"].lower()
                for item in payload["query_spec"]["semantic_items"]
            )
            and "specimens and screenings independently belong to collections"
            in str(payload["documents"]).lower()
            and "exact requested formula is count(specimens.specimen_id"
            in str(payload["documents"]).lower()
        )
        priority_rule = (
                "remains the primary contradicted reason and keeps the same source_handle"
        )
        if not fixture_is_present or priority_rule not in instruction:
            return json.dumps(
                {"status": "consistent", "reason": "DISTINCT fixed the multiset"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the association still substitutes direct participants for the measured population",
                "source_id": "measured-population",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Trusted schema roles: specimens and screenings independently belong to collections; "
            "link rows record direct specimen-screening participation. The exact requested formula is "
            + formula,
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[100.0]]),
            "columns": ["specimen_percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "measured-population"


def test_result_review_allows_normal_join_for_explicit_item_inspection_detail_rows() -> None:
    item_join = (inner_join("orders", "order_id", "items", "order_id"),)
    inspection_join = (inner_join("orders", "order_id", "inspections", "order_id"),)
    state = build_state(
        (
            ItemSpec(
                source_id="relationship-average",
                kind=SemanticItemKind.METRIC,
                table="items",
                column="amount",
                join_path=item_join,
            ),
            ItemSpec(
                source_id="completed-inspection",
                kind=SemanticItemKind.FILTER,
                table="inspections",
                column="status",
                operator=PredicateOperator.EQ,
                literal="completed",
                join_path=inspection_join,
            ),
        ),
        shape=ExpectedResultShape.SCALAR,
    )
    relationship_average = state.query_spec.semantic_items[0].model_copy(
        update={
            "source_text": "documented item-inspection relationship average",
            "normalized_meaning": "DIVIDE(SUM(items.amount), COUNT(inspections.inspection_id))",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "What is the documented average item amount across completed "
                        "item-inspection relationship detail rows?"
                    ),
                    "semantic_items": (
                        relationship_average,
                        state.query_spec.semantic_items[1],
                    ),
                    "requested_output_source_ids": ("relationship-average",),
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
        "SELECT CAST(SUM(i.amount) AS REAL) / COUNT(s.inspection_id) FROM orders o "
        "JOIN items i ON i.order_id = o.order_id "
        "JOIN inspections s ON s.order_id = o.order_id "
        "WHERE s.status = 'completed'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-item-inspection-detail")
    candidate = SqlCandidate(
        candidate_id="terminal-item-inspection-detail",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split()).lower()
        relationship_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "relationship-average"
        )
        qualifying_binding = next(
            binding
            for binding in payload["bindings"]
            if binding["source_id"] == "completed-inspection"
        )
        exact_detail_formula = (
            payload["query_spec"]["semantic_items"][0]["kind"] == "metric"
            and "divide(sum(items.amount), count(inspections.inspection_id))"
            in payload["query_spec"]["semantic_items"][0]["normalized_meaning"].lower()
            and relationship_binding["physical_column"]["table"]["table"] == "items"
            and relationship_binding["physical_column"]["column"] == "amount"
            and relationship_binding["join_path"]
            and qualifying_binding["kind"] == "discriminator_value"
            and qualifying_binding["discriminator_column"]["table"]["table"]
            == "inspections"
            and qualifying_binding["discriminator_predicate"]["right"] == "completed"
            and qualifying_binding["join_path"]
            and "compute exactly sum(items.amount)/count(inspections.inspection_id)"
            in str(payload["documents"]).lower()
            and "(item_id, inspection_id) pair is one requested relationship/detail row"
            in str(payload["documents"]).lower()
            and {aggregate["function"] for aggregate in payload["ast"]["aggregates"]}
            == {"sum", "count"}
            and "distinct" not in payload["sql"].lower()
            and "exists" not in payload["sql"].lower()
        )
        if (
            "explicitly requests relationship or detail rows as its counting unit"
            not in instruction
            or "required metric or formula and a trusted document explicitly specify the exact avg or sum/count formula and its counting unit"
            not in instruction
            or "preserve the qualifying join-row multiset" not in instruction
            or not exact_detail_formula
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the joined detail rows were incorrectly rejected",
                    "source_id": "relationship-average",
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
        documents=(
            "Compute exactly SUM(items.amount)/COUNT(inspections.inspection_id); each "
            "joined completed (item_id, inspection_id) pair is one requested "
            "relationship/detail row.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[12.5]]),
            "columns": ["relationship_average"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None


def test_result_review_rejects_count_identifier_sibling_fanout_without_detail_unit() -> None:
    asset_join = (inner_join("portfolios", "portfolio_id", "assets", "portfolio_id"),)
    inspection_join = (
        inner_join("portfolios", "portfolio_id", "inspections", "portfolio_id"),
    )
    state = build_state(
        (
            ItemSpec(
                source_id="measured-assets",
                kind=SemanticItemKind.METRIC,
                table="assets",
                column="asset_id",
                join_path=asset_join,
            ),
            ItemSpec(
                source_id="passed-inspection",
                kind=SemanticItemKind.FILTER,
                table="inspections",
                column="status",
                operator=PredicateOperator.EQ,
                literal="passed",
                join_path=inspection_join,
            ),
        ),
        shape=ExpectedResultShape.SCALAR,
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "What percentage of assets belong to portfolios with a passed "
                        "inspection?"
                    ),
                    "requested_output_source_ids": ("measured-assets",),
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
        "SELECT 100.0 * COUNT(a.asset_id) / COUNT(a.asset_id) FROM portfolios p "
        "JOIN assets a ON a.portfolio_id = p.portfolio_id "
        "JOIN inspections i ON i.portfolio_id = p.portfolio_id "
        "WHERE i.status = 'passed'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-asset-sibling-fanout")
    candidate = SqlCandidate(
        candidate_id="terminal-asset-sibling-fanout",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = " ".join(payload["instruction"].split()).lower()
        required_rule = (
            "count(identifier) with a predicate from a related relation preserves those "
            "stated operations but does not itself establish physical joined-row multiplicity"
        )
        explicit_boundary = (
            "only when the question or trusted document explicitly names those rows as its "
            "counting unit"
        )
        has_assets = any(
            binding["source_id"] == "measured-assets"
            and binding["physical_column"]["table"]["table"] == "assets"
            and binding["physical_column"]["column"] == "asset_id"
            for binding in payload["bindings"]
        )
        has_inspections = any(
            binding["source_id"] == "passed-inspection"
            and binding["discriminator_column"]["table"]["table"] == "inspections"
            and binding["discriminator_predicate"]["right"] == "passed"
            for binding in payload["bindings"]
        )
        if not (required_rule in instruction and explicit_boundary in instruction and has_assets and has_inspections):
            return json.dumps({"status": "consistent", "reason": "the percentage executed"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "passed inspections multiply measured asset rows without a detail-row unit",
                "source_id": "measured-assets",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Compute DIVIDE(COUNT(assets.asset_id WHERE inspection is passed), "
            "COUNT(assets.asset_id))*100. Inspections only qualify portfolios; no "
            "relationship or detail rows are named as the counting unit.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[100.0]]),
            "columns": ["percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "measured-assets"
    assert receipt.row_grain_requirement is None


def test_result_review_checks_audit_logic_finding_against_formula_and_ast() -> None:
    state, requirements, _, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    sql = (
        "SELECT CAST(SUM(CASE WHEN p.status = 'paid' THEN 1 ELSE 0 END) AS REAL) "
        "/ COUNT(p.payment_id) FROM payments p WHERE p.status = 'paid'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-audit-logic-review")
    candidate = SqlCandidate(
        candidate_id="terminal-audit-logic-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"].lower()
        required = (
            "substantive audit finding",
            "verify it against the trusted formula, ast, and returned data",
            "return contradicted when that independent comparison confirms",
            "bare division without real coercion computes a different result",
            "casting the numerator to a real type preserves the exact operands",
        )
        advisory_issues = payload.get("advisory_issues")
        if (
            not all(clause in instruction for clause in required)
            or not isinstance(advisory_issues, list)
            or not any(
                issue.get("issue_type") == "LLM_logic"
                and "always true" in issue.get("description", "")
                for issue in advisory_issues
                if isinstance(issue, dict)
            )
        ):
            return json.dumps({"status": "consistent", "reason": "formula accepted"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the WHERE predicate makes every CASE condition true",
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
            "Divide the count of paid payments by the count of all payments.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[1.0]]),
            "advisory_issues": [
                {
                    "issue_type": "LLM_logic",
                    "description": (
                        "The WHERE predicate makes the CASE condition always true, "
                        "so the result is always 1.0 for non-empty input."
                    ),
                    "blocking": False,
                }
            ],
            "columns": ["ratio"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"
    assert receipt.row_grain_requirement is None


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
        exact_document = "sum(case eligible event)/sum(case eligible event)" in str(
            payload["documents"]
        ).lower()
        ast_follows_document = (
            "sum(case when e.status = 'eligible' then 1 else 0 end) / "
            "sum(case when e.status = 'eligible' then 1 else 0 end)"
            in payload["sql"].lower()
        )
        if (
            "deduplicate only when the question, queryspec, or trusted formula explicitly requires unique, distinct, or entity-once counting"
            not in instruction
            or "one-to-many relationship or an entity name alone does not add that requirement"
            not in instruction
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


def test_result_review_preserves_plain_count_in_exact_ratio_formula() -> None:
    state, _, _, _ = _case()
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "eligible record percentage",
            "normalized_meaning": (
                "DIVIDE(COUNT(record_id WHERE status = 'eligible'), "
                "COUNT(record_id))*100"
            ),
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Return the documented eligible record percentage.",
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
        "SELECT CAST(COUNT(CASE WHEN e.status = 'eligible' THEN e.record_id END) AS REAL) "
        "/ COUNT(e.record_id) * 100 FROM records e "
        "JOIN record_attributes a ON a.record_id = e.record_id"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-exact-count-ratio")
    candidate = SqlCandidate(
        candidate_id="terminal-exact-count-ratio",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"].lower()
        if all(
            clause in instruction
            for clause in (
                "count(input) in an exact trusted formula counts non-null input occurrences",
                "does not implicitly add distinct",
                "unique, distinct, or entity-once",
            )
        ):
            return json.dumps(
                {"status": "consistent", "reason": "the exact count formula is preserved"}
            )
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the join may repeat one record, so count distinct records",
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
            "Percentage = DIVIDE(COUNT(record_id WHERE status = 'eligible'), "
            "COUNT(record_id))*100.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[25.0]]),
            "columns": ["percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


def test_result_review_rejects_text_formatted_numeric_metric() -> None:
    state, _, _, _ = _case()
    formula = state.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FORMULA,
            "source_text": "event rate percentage",
            "normalized_meaning": "event rate as percent with five decimal places",
        }
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Return the event rate as a percentage with five decimal places."
                    ),
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
        "SELECT CONCAT(ROUND(CAST(COUNT(*) AS numeric) * 100 "
        "/ NULLIF(COUNT(*), 0), 5), '%') FROM orders o"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "text-formatted-metric")
    candidate = SqlCandidate(
        candidate_id="text-formatted-metric",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "numeric metric is requested with a fixed number of decimal places"
            in instruction
            and "keep the SQL result numeric" in instruction
            and "Do not use text formatting or append a display suffix" in instruction
            and payload["data"] == [["25.00000%"]]
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "numeric metric was changed into display text",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "formatted text accepted"}
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
        execution={
            **_executor_result([["25.00000%"]]),
            "columns": ["percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"


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


def test_result_review_does_not_infer_distinct_for_counted_entity_join() -> None:
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
        if (
            "for counts as well as ratios, do not infer distinct"
            in instruction
            and "a named entity, identifier, or one-to-many join alone does not prove deduplication"
            in instruction
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

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None


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
        if (
            "for counts as well as ratios, do not infer distinct"
            not in instruction
        ):
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


def test_result_review_rejects_duplicate_bounded_attribute_values() -> None:
    state, requirements, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "List the first three status values alphabetically."
                    )
                }
            )
        }
    )
    sql = "SELECT o.status FROM orders o ORDER BY o.status LIMIT 3"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "bounded-attribute-values")
    candidate = SqlCandidate(
        candidate_id="bounded-attribute-values",
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

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            "values of an attribute themselves as a set, whether bounded or unbounded"
            in instruction
            and "return each value once" in instruction
            and "requests rows or entities and merely displays that attribute"
            in instruction
            and payload["data"] == [["new"], ["new"], ["paid"]]
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "duplicate attribute values consume the bounded result",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "duplicate values accepted"}
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
        execution={
            **_executor_result([["new"], ["new"], ["paid"]]),
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "source-1"


def test_result_review_rejects_duplicate_unbounded_attribute_values() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="material-value",
                kind=SemanticItemKind.DIMENSION,
                table="products",
                column="material",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List all material values.",
                    "requested_output_source_ids": ("material-value",),
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
    sql = "SELECT p.material FROM products p ORDER BY p.material"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "unbounded-attribute-values")
    candidate = SqlCandidate(
        candidate_id="unbounded-attribute-values",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if (
            "values of an attribute themselves as a set, whether bounded or unbounded"
            in payload["instruction"]
            and "return each value once" in payload["instruction"]
            and payload["data"] == [["linen"], ["linen"], ["wool"]]
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "duplicate values do not form the requested set",
                    "source_id": "material-value",
                }
            )
        return json.dumps({"status": "consistent", "reason": "duplicates accepted"})

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
        execution={
            **_executor_result([["linen"], ["linen"], ["wool"]]),
            "columns": ["material"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "material-value"


def test_result_review_preserves_entity_rows_with_repeated_attribute_values() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="product-name",
                kind=SemanticItemKind.DIMENSION,
                table="products",
                column="name",
            ),
            ItemSpec(
                source_id="material-value",
                kind=SemanticItemKind.DIMENSION,
                table="products",
                column="material",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List each product and its material.",
                    "requested_output_source_ids": ("product-name", "material-value"),
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
    sql = "SELECT p.name, p.material FROM products p ORDER BY p.name"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "entity-rows-with-material")
    candidate = SqlCandidate(
        candidate_id="entity-rows-with-material",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if (
            "values of an attribute themselves as a set, whether bounded or unbounded"
            in payload["instruction"]
            and "requests rows or entities and merely displays that attribute, preserve separate rows"
            in payload["instruction"]
            and payload["data"] == [["desk", "wood"], ["shelf", "wood"]]
        ):
            return json.dumps(
                {"status": "consistent", "reason": "each product row is requested"}
            )
        return json.dumps({"status": "contradicted", "reason": "rows collapsed"})

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
        execution={
            **_executor_result([["desk", "wood"], ["shelf", "wood"]]),
            "columns": ["name", "material"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


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
            and "When the requested output is a proven unique identity of a root entity and a "
            "joined child relation is used only to qualify that entity, with no child output "
            "requested, return each qualifying root identity once" in instruction
            and "does not apply to counts, metrics, formulas, or requested child rows"
            in instruction
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
    ("document", "sql", "query_literal", "alternative_rows", "expected_verdict"),
    (
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            None,
            None,
            "consistent",
        ),
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            "STORED-ENABLED",
            None,
            "consistent",
        ),
        (
            "The source spelling stored/enabled denotes the stored value stored-enabled.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            "stored/enabled",
            None,
            "consistent",
        ),
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            "stored-disabled",
            (),
            "consistent",
        ),
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            "stored-disabled",
            ("stored-disabled",),
            "contradicted",
        ),
        (
            "The exact physical predicate requires state_code equals 'stored-disabled'.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-enabled'",
            None,
            None,
            "consistent",
        ),
        (
            "An enabled record is also described as a current record.",
            "SELECT r.state_code FROM records r WHERE r.state_code = 'stored-disabled'",
            None,
            None,
            "contradicted",
        ),
    ),
)
def test_result_review_keeps_confirmed_discriminator_representation_despite_alias(
    document: str,
    sql: str,
    query_literal: str | None,
    alternative_rows: tuple[str, ...] | None,
    expected_verdict: str,
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
    binding = state.bindings[0]
    alternative_evidence = None
    if alternative_rows is not None:
        selected_evidence = next(
            item for item in state.evidence if item.evidence_id.endswith("-value")
        )
        payload = {
            "columns": ["state_code"],
            "requested_value": "stored-disabled",
            "rows": [[row] for row in alternative_rows],
        }
        payload_bytes = canonical_json_bytes(payload)
        observation = json.loads(selected_evidence.observation)
        observation.update(
            {
                "byte_count": len(payload_bytes),
                "invocation_id": "evidence-record-status-alternative",
                "payload": payload,
                "payload_digest": canonical_digest(payload),
                "row_count": len(alternative_rows),
            }
        )
        observation["provenance"].update(
            {
                "invocation_id": "evidence-record-status-alternative",
                "payload_digest": observation["payload_digest"],
            }
        )
        alternative_evidence = selected_evidence.model_copy(
            update={
                "evidence_id": "evidence-record-status-alternative",
                "observation": canonical_json_bytes(observation).decode("utf-8"),
                "cost": selected_evidence.cost.model_copy(
                    update={"rows": len(alternative_rows), "bytes": len(payload_bytes)}
                ),
            }
        )
        binding = binding.model_copy(
            update={"evidence_ids": (*binding.evidence_ids, alternative_evidence.evidence_id)}
        )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List enabled records.",
                    "semantic_items": (
                        state.query_spec.semantic_items[0].model_copy(
                            update={"literal_or_reference": query_literal}
                        )
                        if query_literal is not None
                        else state.query_spec.semantic_items[0],
                    ),
                }
            ),
            "evidence": state.evidence
            if alternative_evidence is None
            else (*state.evidence, alternative_evidence),
            "bindings": (binding,),
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
        case_only_rule = (
            "for that same physical column and operator, a case-only difference in that literal "
            "does not override the selected observed physical spelling"
        )
        positive_evidence_rule = (
            "an alternative literal may contradict the selected exact discriminator only when "
            "separate durable exact db evidence positively contains that alternative for the same "
            "physical column and operator"
        )
        old_absolute_rule = "a different casefolded literal is a contradiction"
        equivalent_surface_rule = (
            "punctuation or formatting alone does not override the selected observed "
            "physical spelling when both forms denote the same value"
        )
        query_literal_value = payload["query_spec"]["semantic_items"][0][
            "literal_or_reference"
        ]
        observed_literal = binding["discriminator_predicate"]["right"]
        if (
            not binding_is_exact
            or required_rule not in instruction
            or positive_evidence_rule not in instruction
            or old_absolute_rule in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the document alias must replace the stored representation",
                    "source_id": "record-status",
                }
            )
        alternative_is_positive = False
        if alternative_evidence is not None:
            observation = next(
                item
                for item in payload["evidence"]
                if item["evidence_id"] == alternative_evidence.evidence_id
            )
            alternative_payload = json.loads(observation["observation"])["payload"]
            alternative_is_positive = (
                not json.loads(observation["observation"])["truncated"]
                and alternative_payload
                == {
                    "columns": ["state_code"],
                    "requested_value": "stored-disabled",
                    "rows": [[row] for row in alternative_rows],
                }
                and bool(alternative_rows)
            )
        if query_literal_value != observed_literal:
            equivalent_surface = (
                query_literal_value == "stored/enabled"
                and equivalent_surface_rule in instruction
            )
            if (
                isinstance(query_literal_value, str)
                and query_literal_value.casefold() == observed_literal.casefold()
            ):
                if case_only_rule not in instruction:
                    return json.dumps(
                        {
                            "status": "contradicted",
                            "reason": "the query spelling must replace the observed spelling",
                            "source_id": "record-status",
                        }
                    )
            elif not equivalent_surface:
                if alternative_is_positive:
                    return json.dumps(
                        {
                            "status": "contradicted",
                            "reason": "separate exact DB evidence contains the query literal",
                            "source_id": "record-status",
                        }
                    )
                return json.dumps(
                    {
                        "status": "consistent",
                        "reason": "the query literal has no positive exact DB evidence",
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


@pytest.mark.parametrize(
    (
        "alternative_rows",
        "alternative_truncated",
        "candidate_predicate",
        "response_kind",
        "expected_verdict",
    ),
    (
        ((), False, "status_code = 'stored-approved'", "bare", "consistent"),
        (("document-alternative",), False, "status_code = 'stored-approved'", "bare", "contradicted"),
        ((), True, "status_code = 'stored-approved'", "bare", "contradicted"),
        ((), False, "status_code != 'stored-approved'", "bare", "contradicted"),
        ((), False, "other_code = 'stored-approved'", "bare", "contradicted"),
        ((), False, "status_code = 'stored-approved'", "semantic_repair", "contradicted"),
        ((), False, "status_code = 'stored-approved'", "predicate_authority", "contradicted"),
        ((), False, "status_code = 'stored-approved'", "row_grain", "contradicted"),
    ),
    ids=(
        "untruncated_empty_exact_search",
        "positive_exact_search",
        "truncated_empty_exact_search",
        "wrong_operator",
        "wrong_column",
        "semantic_repair",
        "predicate_authority",
        "row_grain",
    ),
)
def test_result_review_requires_positive_exact_evidence_for_alternative_discriminator(
    alternative_rows: tuple[str, ...],
    alternative_truncated: bool,
    candidate_predicate: str,
    response_kind: str,
    expected_verdict: str,
) -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="fictional-status",
                kind=SemanticItemKind.FILTER,
                table="fictional_records",
                column="status_code",
                operator=PredicateOperator.EQ,
                literal="stored-approved",
            ),
        )
    )
    binding = state.bindings[0]
    selected_evidence = next(
        item for item in state.evidence if item.evidence_id.endswith("-value")
    )

    def exact_search_evidence(
        evidence_id: str,
        value: str,
        rows: tuple[str, ...],
        *,
        truncated: bool = False,
    ):
        payload = {
            "columns": ["status_code"],
            "requested_value": value,
            "rows": [[row] for row in rows],
        }
        payload_bytes = canonical_json_bytes(payload)
        observation = json.loads(selected_evidence.observation)
        observation.update(
            {
                "byte_count": len(payload_bytes),
                "invocation_id": evidence_id,
                "payload": payload,
                "payload_digest": canonical_digest(payload),
                "row_count": len(rows),
                "truncated": truncated,
            }
        )
        observation["provenance"].update(
            {
                "invocation_id": evidence_id,
                "payload_digest": observation["payload_digest"],
            }
        )
        return selected_evidence.model_copy(
            update={
                "evidence_id": evidence_id,
                "observation": canonical_json_bytes(observation).decode("utf-8"),
                "cost": selected_evidence.cost.model_copy(
                    update={"rows": len(rows), "bytes": len(payload_bytes)}
                ),
            }
        )

    selected_evidence = exact_search_evidence(
        "evidence-fictional-status-stored", "stored-approved", ("stored-approved",)
    )
    alternative_evidence = exact_search_evidence(
        "evidence-fictional-status-alternative",
        "document-alternative",
        alternative_rows,
        truncated=alternative_truncated,
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List fictional records with the documented status.",
                    "semantic_items": (
                        state.query_spec.semantic_items[0].model_copy(
                            update={"literal_or_reference": "document-alternative"}
                        ),
                    ),
                }
            ),
            "evidence": tuple(
                item
                for item in state.evidence
                if item.evidence_id != binding.evidence_ids[1]
            )
            + (selected_evidence, alternative_evidence),
            "bindings": (
                binding.model_copy(
                    update={
                        "evidence_ids": (
                            binding.evidence_ids[0],
                            selected_evidence.evidence_id,
                            alternative_evidence.evidence_id,
                        )
                    }
                ),
            ),
        }
    )
    freshness = FreshnessContext(
        evaluated_at=selected_evidence.observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    sql = (
        "SELECT f.status_code FROM fictional_records f "
        f"WHERE f.{candidate_predicate}"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-alternative-discriminator")
    candidate = SqlCandidate(
        candidate_id="terminal-alternative-discriminator",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    rule = (
        "An alternative literal may contradict the selected exact discriminator only when "
        "separate durable exact DB evidence positively contains that alternative for the same "
        "physical column and operator. QuerySpec/document wording and an untruncated exact "
        "search with rows=[] do not prove the stored literal."
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        observations = {
            item["evidence_id"]: json.loads(item["observation"])
            for item in payload["evidence"]
        }
        assert payload["query_spec"]["semantic_items"][0]["literal_or_reference"] == "document-alternative"
        assert "status_code equals 'document-alternative'" in str(payload["documents"])
        assert not observations[selected_evidence.evidence_id]["truncated"]
        assert observations[selected_evidence.evidence_id]["payload"]["rows"] == [
            ["stored-approved"]
        ]
        assert (
            observations[alternative_evidence.evidence_id]["truncated"]
            is alternative_truncated
        )
        assert observations[alternative_evidence.evidence_id]["payload"] == {
            "columns": ["status_code"],
            "requested_value": "document-alternative",
            "rows": [[row] for row in alternative_rows],
        }
        assert rule in payload["instruction"]
        response = {
            "status": "contradicted",
            "reason": "the document alternative overrides the selected literal",
            "source_id": "fictional-status",
        }
        if response_kind == "semantic_repair":
            response["repair_kind"] = "semantic_binding_mismatch"
        elif response_kind == "predicate_authority":
            response["predicate_authority"] = binding.discriminator_predicate.model_dump(
                mode="json"
            )
        elif response_kind == "row_grain":
            response["row_grain_requirement"] = "preserve_qualifying_rows"
        return json.dumps(response)

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "The exact physical predicate requires status_code equals 'document-alternative'.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["stored-approved"]]),
            "columns": ["status_code"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == expected_verdict


def test_result_review_keeps_bare_contradiction_for_non_discriminator_source() -> None:
    state, requirements, candidate, _ = _case()
    freshness = FreshnessContext(
        evaluated_at=state.evidence[0].observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    parsed = parse_sql_candidate(candidate.sql, POSTGRES_DSN, candidate.candidate_id)
    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(),
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "the selected output needs review",
                "source_id": "source-1",
            }
        ),
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=candidate.sql,
        execution=_executor_result([["paid"]]),
    )

    assert receipt.verdict == "contradicted"


@pytest.mark.parametrize(
    ("alternative_has_state_authority", "expected_verdict"),
    ((True, "consistent"), (False, "contradicted")),
    ids=("durable_zero_row", "foreign_zero_row"),
)
def test_result_review_normalizes_zero_row_alternative_in_discriminator(
    alternative_has_state_authority: bool, expected_verdict: str
) -> None:
    selected_values = ("stored-alpha", "stored-beta")
    alternative_values = ("document-alpha", "document-beta")
    state = build_state(
        (
            ItemSpec(
                source_id="fictional-status-set",
                kind=SemanticItemKind.FILTER,
                table="fictional_records",
                column="status_code",
                operator=PredicateOperator.IN,
                literal=selected_values,
            ),
        )
    )
    binding = state.bindings[0]
    value_evidence = next(
        item for item in state.evidence if item.evidence_id.endswith("-value")
    )

    def exact_search_evidence(evidence_id: str, value: str, rows: tuple[str, ...]):
        payload = {
            "columns": ["status_code"],
            "requested_value": value,
            "rows": [[row] for row in rows],
        }
        payload_bytes = canonical_json_bytes(payload)
        observation = json.loads(value_evidence.observation)
        observation.update(
            {
                "byte_count": len(payload_bytes),
                "invocation_id": evidence_id,
                "payload": payload,
                "payload_digest": canonical_digest(payload),
                "row_count": len(rows),
            }
        )
        observation["provenance"].update(
            {
                "invocation_id": evidence_id,
                "payload_digest": observation["payload_digest"],
            }
        )
        return value_evidence.model_copy(
            update={
                "evidence_id": evidence_id,
                "observation": canonical_json_bytes(observation).decode("utf-8"),
                "cost": value_evidence.cost.model_copy(
                    update={"rows": len(rows), "bytes": len(payload_bytes)}
                ),
            }
        )

    selected_evidence = tuple(
        exact_search_evidence(
            f"evidence-fictional-status-set-selected-{index}", value, (value,)
        )
        for index, value in enumerate(selected_values, start=1)
    )
    alternative_evidence = tuple(
        exact_search_evidence(
            f"evidence-fictional-status-set-alternative-{index}", value, ()
        )
        for index, value in enumerate(alternative_values, start=1)
    )
    if not alternative_has_state_authority:
        alternative_evidence = tuple(
            item.model_copy(update={"run_id": "foreign-run"})
            for item in alternative_evidence
        )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List fictional records with documented statuses.",
                    "semantic_items": (
                        state.query_spec.semantic_items[0].model_copy(
                            update={"literal_or_reference": alternative_values}
                        ),
                    ),
                }
            ),
            "evidence": tuple(
                item
                for item in state.evidence
                if item.evidence_id != binding.evidence_ids[1]
            )
            + selected_evidence
            + alternative_evidence,
            "bindings": (
                binding.model_copy(
                    update={
                        "evidence_ids": (
                            binding.evidence_ids[0],
                            *(item.evidence_id for item in selected_evidence),
                            *(
                                item.evidence_id
                                for item in alternative_evidence
                                if alternative_has_state_authority
                            ),
                        )
                    }
                ),
            ),
        }
    )
    freshness = FreshnessContext(
        evaluated_at=value_evidence.observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    requirements = validate_coverage_inputs(
        state, freshness, state.run_id, state.run_incarnation
    )
    sql = (
        "SELECT f.status_code FROM fictional_records f "
        "WHERE f.status_code IN ('stored-alpha', 'stored-beta')"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-alternative-in")
    candidate = SqlCandidate(
        candidate_id="terminal-alternative-in",
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
        documents=("The documented statuses use alternative names.",),
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "the document alternatives override the selected statuses",
                "source_id": "fictional-status-set",
            }
        ),
    )

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["stored-alpha"], ["stored-beta"]]),
            "columns": ["status_code"],
            "sql_query": sql,
        },
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
    assert (
        "aggregate of a quantity computed separately for each entity"
        in prompts[0]["instruction"]
    )
    assert "include zero matching children" in prompts[0]["instruction"]
    assert "does not erase that explicit entity grain" in prompts[0]["instruction"]
    assert (
        "and that document explicitly states its row scope or counting unit"
        in prompts[0]["instruction"]
    )


def test_result_review_preserves_explicit_parent_absence_grain() -> None:
    state, _, _, _ = _case()
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "What percentage of verified orders does not contain a returned item?"
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
        "WITH per_order AS ("
        "SELECT o.id AS order_id, "
        "MAX(CASE WHEN i.status = 'returned' THEN 1 ELSE 0 END) AS has_returned "
        "FROM orders o LEFT JOIN order_items i ON i.order_id = o.id "
        "WHERE o.status = 'verified' GROUP BY o.id"
        ") SELECT 100.0 * SUM(CASE WHEN has_returned = 0 THEN 1 ELSE 0 END) "
        "/ COUNT(*) AS percentage FROM per_order"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "parent-absence-grain-review")
    candidate = SqlCandidate(
        candidate_id="parent-absence-grain-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        assert payload["question"] == (
            "What percentage of verified orders does not contain a returned item?"
        )
        assert "LEFT JOIN order_items" in payload["sql"]
        assert "WHERE o.status = 'verified' GROUP BY o.id" in payload["sql"]
        if (
            "do not contain any matching child" not in instruction
            or "do not by themselves state a child-row result grain" not in instruction
            or "counting unit in semantic terms" not in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the document counts child rows",
                    "source_id": "source-1",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the candidate excludes each parent with a matching child",
                "source_id": None,
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "Percentage = COUNT(item.status = 'returned') * 100 / COUNT(order_id).",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([[75.0]]),
            "columns": ["percentage"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


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


def test_result_review_final_check_preserves_named_base_population_and_calendar_year() -> None:
    state, requirements, candidate, _ = _case()
    prompts: list[str] = []

    def reviewer(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"status": "consistent", "reason": "captured prompt"})

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
    instruction = " ".join(json.loads(prompts[0])["instruction"].split())
    final_rule = (
        "Final mandatory population and time check: an ordinary requested count of named "
        "base entities must preserve the base-entity population when no trusted exact "
        "formula explicitly establishes another counting unit. A related table may qualify "
        "those entities through EXISTS or a deduplicated qualifying-key set without creating "
        "DISTINCT or entity-once semantics. Explicit relationship/detail-row counting units "
        "and exact formula counting units, including their existing plain COUNT behavior, "
        "remain exceptions. Return contradicted for a base-population violation or when a "
        "full date/time value is directly compared with a bare calendar-year literal. A full "
        "date/time value directly compared with a bare calendar-year literal cannot be "
        "consistent. Calendar-year extraction or a trusted full-date boundary remains valid. "
        "An advisory issue is only supporting evidence, not authority."
    )

    assert instruction.endswith(final_rule)


def test_result_review_keeps_selected_probe_evidence() -> None:
    state, requirements, candidate, _ = _case()
    marker = "admitted-probe-observation"
    probe_evidence = state.evidence[0].model_copy(
        update={
            "source_kind": EvidenceSourceKind.PROBE,
            "observation": json.dumps(
                {
                    "marker": marker,
                    "sql": "SELECT COUNT(DISTINCT entity_key) FROM source_rows",
                    "summary": "three unique entities",
                    "payload": {"columns": ["entity_count"], "rows": [[3]]},
                }
            ),
            "validity_scope": EvidenceValidityScope.RUN_ONLY,
            "data_snapshot_token": None,
        }
    )
    state = state.model_copy(update={"evidence": (probe_evidence,)})

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        if marker in json.dumps(payload["evidence"]):
            return json.dumps({"status": "consistent", "reason": "result matches"})
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "selected evidence is missing",
                "source_id": "source-1",
            }
        )

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


def test_result_review_preserves_supported_best_available_entity_proxy() -> None:
    state = build_state(
        (
            ItemSpec(
                source_id="home-region",
                kind=SemanticItemKind.DIMENSION,
                table="accounts",
                column="billing_region",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "What is the account holder's home region?",
                    "requested_output_source_ids": ("home-region",),
                }
            ),
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
    sql = "SELECT a.billing_region FROM accounts a"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-best-proxy-review")
    candidate = SqlCandidate(
        candidate_id="terminal-best-proxy-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def best_proxy_review(prompt: str) -> str:
        payload = json.loads(prompt)
        if (
            "A selected SUPPORTED binding may also preserve that best-available proxy conclusion"
            in payload["instruction"]
            and payload["bindings"][0]["status"] == "supported"
        ):
            return json.dumps({"status": "consistent", "reason": "research resolved the proxy"})
        return json.dumps(
            {
                "status": "ambiguous",
                "reason": "billing region is not literally home region",
                "source_id": "home-region",
                "repair_kind": "semantic_binding_mismatch",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=("accounts.billing_region stores the account's billing region.",),
        model=best_proxy_review,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["North"]]),
            "columns": ["billing_region"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"


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
            "a direct named-entity column because an event or record has a same-named column. "
            "A qualifying relation that trusted schema describes as another representation of the same "
            "named entity at the same identity key is not an event or record for this checklist: prefer "
                "its direct qualifying-row label over adding a separate master or entity join solely for "
                "another label, unless the question explicitly requests a current, canonical, master, "
                "persistent, or independent attribute. Before accepting a requested entity label as "
                "consistent, compare every direct matching label already visible on same-identity "
                "representations that supply a required condition or formula. Do not stop at the first "
                "plausible master label."
            )
        if (
                owner_checklist not in instruction
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
            or "For a direct matching entity label on a same-identity relation that supplies "
            "a required condition or formula, trusted table and identity semantics are "
            "sufficient row-local authority; its column description does not need to call "
            "the label full or official" not in instruction
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


@pytest.mark.parametrize(
    ("source_ids", "reported_source", "expected_source"),
    (
        (("semantic:abcd", "semantic:wxyz"), "semantic:abc", "semantic:abcd"),
        (("semantic:abcd", "semantic:wxyz"), "semantic:abcxd", "semantic:abcd"),
        (("semantic:abcd", "semantic:wxyz"), "semantic:abxd", "semantic:abcd"),
        (("semantic:abcx", "semantic:abcy"), "semantic:abc", None),
        (("semantic:abcd", "semantic:wxyz"), "semantic:ab", None),
        (("semantic:abcd", "semantic:wxyz"), "semantic:qrst", None),
    ),
    ids=("deletion", "insertion", "substitution", "ambiguous", "two-edits", "unrelated"),
)
def test_result_review_canonicalizes_only_unique_single_edit_opaque_source_id(
    source_ids, reported_source, expected_source
) -> None:
    state = build_state(
        (
            ItemSpec(
                source_id=source_ids[0],
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="status",
            ),
            ItemSpec(
                source_id=source_ids[1],
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="kind",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List order status and kind.",
                    "requested_output_source_ids": source_ids,
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
    sql = "SELECT o.status, o.kind FROM orders o"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-source-id-typo")
    candidate = SqlCandidate(
        candidate_id="terminal-source-id-typo",
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
        model=lambda _prompt: json.dumps(
            {
                "status": "contradicted",
                "reason": "the status population is wrong",
                "source_id": reported_source,
                "repair_kind": None,
                "repair_binding_id": None,
            }
        ),
    )
    execution = {
        **_executor_result([["open", "retail"]]),
        "columns": ["status", "kind"],
        "sql_query": sql,
    }

    if expected_source is None:
        with pytest.raises(ValueError, match="review source is not an allowed binding"):
            evaluate_result_review_capability(
                review,
                expected_run_id=state.run_id,
                expected_sql=sql,
                execution=execution,
            )
        return

    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution=execution,
    )
    assert receipt.verdict == "contradicted"
    assert receipt.source_id == expected_source
    assert receipt.repair_kind is None
    assert receipt.repair_binding_id is None


def test_result_review_resolves_short_handle_for_long_semantic_source_id() -> None:
    source_ids = (
        "semantic:fictional:population:with:a:long:compound:identifier",
        "semantic:fictional:qualifier:with:another:long:compound:identifier",
    )
    state = build_state(
        (
            ItemSpec(
                source_id=source_ids[0],
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="status",
            ),
            ItemSpec(
                source_id=source_ids[1],
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="kind",
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List order status and kind.",
                    "requested_output_source_ids": source_ids,
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
    sql = "SELECT o.status, o.kind FROM orders o"
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-short-source-handle")
    candidate = SqlCandidate(
        candidate_id="terminal-short-source-handle",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    prompts: list[str] = []

    def reviewer(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps(
            {
                "status": "contradicted",
                "reason": "the qualifying population is wrong",
                "source_handle": "r2",
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
        execution={
            **_executor_result([["open", "retail"]]),
            "columns": ["status", "kind"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == source_ids[1]
    assert [item["source_handle"] for item in json.loads(prompts[0])["bindings"]] == [
        "r1",
        "r2",
    ]


def test_result_review_rejects_unknown_short_source_handle() -> None:
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
                "source_handle": "r2",
            }
        ),
    )

    with pytest.raises(ValueError, match="review source handle is not allowed"):
        evaluate_result_review_capability(
            review,
            expected_run_id=state.run_id,
            expected_sql=SQL,
            execution=_executor_result([["open"]]),
        )


def test_result_review_rejects_handle_and_legacy_source_together() -> None:
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
                "source_handle": "r1",
                "source_id": "source-1",
            }
        ),
    )

    with pytest.raises(
        ValueError, match="review must name either a source handle or legacy source"
    ):
        evaluate_result_review_capability(
            review,
            expected_run_id=state.run_id,
            expected_sql=SQL,
            execution=_executor_result([["open"]]),
        )


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
    malformed_review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parse_sql_candidate(SQL, POSTGRES_DSN, candidate.candidate_id),
        documents=(),
        model=lambda _prompt: json.dumps(
            {
                "status": "consistent",
                "reason": "the result is consistent",
                "predicate_authority": predicate.model_dump(mode="json"),
            }
        ),
    )
    malformed_receipt = evaluate_result_review_capability(
        malformed_review,
        expected_run_id=state.run_id,
        expected_sql=SQL,
        execution=_executor_result([["open"]]),
    )
    assert malformed_receipt.verdict == "malformed"
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


def test_result_review_accepts_json_predicate_authority_in_transport_form() -> None:
    state, requirements, candidate, _ = _case()
    predicate = PredicateRef(
        left=_column(),
        operator=PredicateOperator.IN,
        right=("active", "pending"),
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
                "reason": "exact status evidence is required",
                "source_handle": "r1",
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
    assert receipt.source_id == "source-1"
    assert receipt.predicate_authority == predicate


def test_result_review_rejects_master_label_when_qualifying_same_identity_label_exists() -> None:
    join_path = (
        inner_join(
            "qualification_records",
            "entity_key",
            "entity_catalog",
            "entity_key",
        ),
    )
    state = build_state(
        (
            ItemSpec(
                source_id="entity-name",
                kind=SemanticItemKind.DIMENSION,
                table="entity_catalog",
                column="master_label",
                join_path=join_path,
            ),
            ItemSpec(
                source_id="qualifying-score",
                kind=SemanticItemKind.FILTER,
                table="qualification_records",
                column="score",
                operator=PredicateOperator.GT,
                literal=10,
                join_path=join_path,
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "List entity names for qualifying records with score above 10.",
                    "requested_output_source_ids": ("entity-name",),
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
        "SELECT c.master_label FROM qualification_records r "
        "INNER JOIN entity_catalog c ON r.entity_key = c.entity_key "
        "WHERE r.score > 10"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-same-identity-label-review")
    candidate = SqlCandidate(
        candidate_id="terminal-same-identity-label-review",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )
    schema = {
        "main.qualification_records": {
            "description": (
                "Same-identity representation of each entity on entity_key; "
                "these rows supply the required qualifying score."
            ),
            "columns": {
                "entity_key": {"description": "Shared entity identity."},
                "row_label": {"description": "Entity name recorded on this row."},
                "score": {"description": "Qualifying score."},
            },
        },
        "main.entity_catalog": {
            "description": "Master entity attributes.",
            "columns": {
                "entity_key": {"description": "Shared entity identity."},
                "master_label": {"description": "Current master entity name."},
            },
        },
    }

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            payload["sql"] == sql
            and payload["schema"] == schema
            and any(
                binding["source_id"] == "qualifying-score"
                and binding["columns"][0]["table"]["table"]
                == "qualification_records"
                for binding in payload["bindings"]
            )
            and "For a direct matching entity label on a same-identity relation that supplies "
            "a required condition or formula, trusted table and identity semantics are "
            "sufficient row-local authority; its column description does not need to call "
            "the label full or official" in instruction
        ):
            binding_id = next(
                binding["binding_id"]
                for binding in payload["bindings"]
                if binding["source_id"] == "entity-name"
            )
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "master label replaced the same-identity qualifying-row label",
                    "source_id": "entity-name",
                    "repair_kind": "semantic_binding_mismatch",
                    "repair_binding_id": binding_id,
                }
            )
        return json.dumps({"status": "consistent", "reason": "master label accepted"})

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
            **_executor_result([["Master A"]]),
            "columns": ["master_label"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "entity-name"
    assert receipt.repair_kind == "semantic_binding_mismatch"


def test_result_review_rejects_outer_reuse_of_aggregate_reference_subset() -> None:
    join_path = (
        inner_join("device_registry", "device_id", "reading_rows", "device_id"),
    )
    state = build_state(
        (
            ItemSpec(
                source_id="device-name",
                kind=SemanticItemKind.DIMENSION,
                table="device_registry",
                column="device_name",
            ),
            ItemSpec(
                source_id="reading-average",
                kind=SemanticItemKind.FORMULA,
                table="reading_rows",
                column="reading_value",
                join_path=join_path,
            ),
            ItemSpec(
                source_id="reference-classification",
                kind=SemanticItemKind.FILTER,
                table="reading_rows",
                column="classification",
                operator=PredicateOperator.EQ,
                literal="reference",
                join_path=join_path,
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "List all devices with a reading above the average reading among "
                        "reference-classified readings."
                    ),
                    "requested_output_source_ids": ("device-name",),
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
        "WITH reference_readings AS ("
        "SELECT r.device_id, r.reading_value FROM reading_rows r "
        "WHERE r.classification = 'reference'"
        "), reference_average AS ("
        "SELECT AVG(reading_value) AS average_reading FROM reference_readings"
        ") SELECT d.device_name FROM device_registry d "
        "JOIN reference_readings r ON r.device_id = d.device_id "
        "CROSS JOIN reference_average a "
        "WHERE r.reading_value > a.average_reading"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-reference-subset")
    candidate = SqlCandidate(
        candidate_id="terminal-reference-subset",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        payload = json.loads(prompt)
        assert payload["question"] == state.query_spec.original_text
        assert payload["sql"] == sql
        if (
            "independently compare the returned population from the original question "
            "against the aggregate reference population" in payload["instruction"].lower()
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the reference subset also restricts returned devices",
                    "source_id": "reference-classification",
                }
            )
        return json.dumps(
            {"status": "consistent", "reason": "the reference subset is accepted"}
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
        execution={
            **_executor_result([["device-a"]]),
            "columns": ["device_name"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "contradicted"
    assert receipt.source_id == "reference-classification"
    assert receipt.repair_kind is None
    assert receipt.predicate_authority is None


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


def test_result_review_prompt_keeps_parent_role_separate_from_history_row_actor() -> None:
    history_parent = inner_join("history", "parent_id", "parents", "id")
    parent_editor = inner_join("parents", "last_editor_id", "users", "id")
    state = build_state(
        (
            ItemSpec(
                source_id="recorded-detail",
                kind=SemanticItemKind.FILTER,
                table="history",
                column="detail",
                operator=PredicateOperator.EQ,
                literal="flagged",
                join_path=(history_parent,),
            ),
            ItemSpec(
                source_id="parent-editor",
                kind=SemanticItemKind.DIMENSION,
                table="users",
                column="display_name",
                join_path=(history_parent, parent_editor),
            ),
        )
    )
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": (
                        "Which parent editor is recorded for parents with a flagged history detail?"
                    ),
                    "requested_output_source_ids": ("parent-editor",),
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
        "SELECT u.display_name FROM history h "
        "JOIN parents p ON p.id = h.parent_id "
        "JOIN users u ON u.id = p.last_editor_id "
        "WHERE h.detail = 'flagged'"
    )
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "terminal-history-parent-role")
    candidate = SqlCandidate(
        candidate_id="terminal-history-parent-role",
        sql=sql,
        normalized_ast_digest=parsed.candidate_digest,
        revision=state.revision,
    )

    def reviewer(prompt: str) -> str:
        instruction = json.loads(prompt)["instruction"]
        if (
            "history, audit, or log row selects its qualifying parent/entity"
            not in instruction
            or "explicitly requests the filter-row actor, author, owner, or updater role"
            not in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the history row actor replaced the selected parent editor",
                    "source_id": "parent-editor",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the history filter qualifies the parent without selecting its row actor",
            }
        )

    review = create_result_review_capability(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=candidate,
        parsed_ast=parsed,
        documents=(
            "history.parent_id identifies the parent of a history row; "
            "history.actor_id is the actor for that history row; "
            "parents.last_editor_id identifies the parent editor.",
        ),
        model=reviewer,
    )
    receipt = evaluate_result_review_capability(
        review,
        expected_run_id=state.run_id,
        expected_sql=sql,
        execution={
            **_executor_result([["Rin"]]),
            "columns": ["display_name"],
            "sql_query": sql,
        },
    )

    assert receipt.verdict == "consistent"
    assert receipt.source_id is None


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


def _exact_arithmetic_projection_review_state(
    recorded_column: str = "recorded_at",
):
    label = _coverage_column("records", "label")
    recorded_at = _coverage_column("records", recorded_column)
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
                                "normalized_meaning": (
                                    f"CURRENT_TIMESTAMP - {recorded_column}"
                                ),
                            }
                        ),
                    ),
                    "requested_output_source_ids": ("arithmetic-formula",),
                }
            )
        }
    )


def _formula_with_input_projection_review_state(
    recorded_column: str = "recorded_at",
):
    state = _exact_arithmetic_projection_review_state(recorded_column)
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


def _trusted_exact_formula_review_state(
    expression: str,
    *,
    recorded_column: str = "recorded_at",
):
    state = _formula_with_input_projection_review_state(recorded_column)
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


def test_result_review_defers_equivalent_count_ratio_to_model() -> None:
    state = _trusted_exact_formula_review_state(
        "DIVIDE(COUNT(recorded_at < 18 AND label = 'accepted'), "
        "COUNT(recorded_at)) * 100"
    )
    calls = 0

    def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        payload = json.loads(prompt)
        assert any(
            item["kind"] == "formula"
            and item["normalized_meaning"]
            == "DIVIDE(COUNT(recorded_at < 18 AND label = 'accepted'), COUNT(recorded_at)) * 100"
            for item in payload["query_spec"]["semantic_items"]
        )
        assert "COUNT(CASE WHEN" in payload["sql"]
        return json.dumps({"status": "consistent", "reason": "formula is valid"})

    receipt = _projection_review(
        state,
        "SELECT (CAST(COUNT(CASE WHEN r.recorded_at < 18 "
        "AND r.label = 'accepted' THEN r.recorded_at END) AS REAL) "
        "/ COUNT(r.recorded_at)) * 100 AS ratio FROM records r",
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


def test_result_review_preserves_explicit_total_input_percentage_formula() -> None:
    formula = (
        "[(total(recorded_at) & label = 'accepted') / total(recorded_at)] * 100"
    )
    state = _trusted_exact_formula_review_state(formula)
    calls = 0

    def model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        instruction = " ".join(json.loads(prompt)["instruction"].split()).lower()
        required_rule = (
            "when a trusted document explicitly defines a percentage or ratio with "
            "a conditioned aggregate numerator and the same named input aggregate as "
            "its denominator, preserve that named aggregate input"
        )
        if required_rule in instruction:
            return json.dumps(
                {"status": "consistent", "reason": "documented aggregate retained"}
            )
        return json.dumps(
            {
                "status": "ambiguous",
                "reason": "entity wording might imply a different counting unit",
                "source_id": "arithmetic-formula",
            }
        )

    receipt = _projection_review(
        state,
        "SELECT (CAST(SUM(CASE WHEN r.label = 'accepted' THEN r.recorded_at ELSE 0 END) "
        "AS REAL) / SUM(r.recorded_at)) * 100 AS percentage FROM records r",
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


def test_result_review_does_not_require_non_output_entity_label_when_row_is_selected() -> None:
    state = _projection_review_state(requested_output_source_ids=("projection-total",))
    state = state.model_copy(
        update={
            "query_spec": state.query_spec.model_copy(
                update={
                    "original_text": "Which owner has the marked record? Return only its amount."
                }
            )
        }
    )
    sql = "SELECT i.amount FROM items i WHERE i.kind = 'marked'"

    def model(prompt: str) -> str:
        payload = json.loads(prompt)
        instruction = payload["instruction"]
        if (
            payload["query_spec"]["requested_output_source_ids"]
            == ["projection-total"]
            and "Only the owner's record has kind = 'marked'." in payload["documents"]
            and "WHERE i.kind = 'marked'" in payload["sql"]
            and "do not require its label or identifier column" not in instruction
        ):
            return json.dumps(
                {
                    "status": "contradicted",
                    "reason": "the required owner identifier is unused",
                    "source_id": "projection-entity",
                }
            )
        return json.dumps(
            {
                "status": "consistent",
                "reason": "the documented predicate already selects the requested entity row",
            }
        )

    receipt = _projection_review(
        state,
        sql,
        model,
        documents=("Only the owner's record has kind = 'marked'.",),
        execution_data=[[42]],
        execution_columns=["amount"],
    )

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
        status=SemanticItemStatus.RESOLVED,
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
            "Return only JSON object with exactly these keys: status, reason, source_handle, "
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
