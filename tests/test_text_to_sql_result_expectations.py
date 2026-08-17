"""RED contract for R4c deterministic result expectations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    ColumnRef,
    DiscriminatorValueBinding,
    EvidenceCost,
    LiteralValue,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResultExpectation,
    ResultExpectationKind,
    QueryProbeRef,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.production_research import (
    _build_initial_research_state,
)
from custom_tools.text_to_sql.adaptive.semantic_reducer import (
    SemanticReducerError,
    derive_result_expectations,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes

from test_text_to_sql_semantic_reducer import INCARNATION, RUN, SCHEMA, _budget, _state


def _column() -> ColumnRef:
    return ColumnRef(
        table=TableRef(namespace="main", schema=None, table="orders"),
        column="status",
    )


def _action_and_evidence(
    kind: ResearchActionKind,
    target: object,
    payload: object,
    *,
    evidence_id: str,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...] = (),
    truncated: bool = False,
):
    if kind in {
        ResearchActionKind.SEARCH_VALUE,
        ResearchActionKind.DISTINCT_VALUES,
        ResearchActionKind.EXECUTE_PROBE,
    }:
        assert isinstance(payload, dict)
        payload = {
            "columns": ["status"],
            "probe_kind": kind.value,
            "schema_namespace_version": SCHEMA,
            "target": target.model_dump(mode="json", by_alias=True),
            **payload,
        }
        row_count = len(payload["rows"])
    else:
        assert isinstance(payload, dict)
        payload = {"schema_namespace_version": SCHEMA, **payload}
        row_count = 1
    digest = canonical_action_digest(
        kind=kind,
        hypothesis_id=None,
        target=target,
        parameters=parameters,
        expected_revision=0,
    )
    action = ResearchAction(
        action_id=f"action-{evidence_id}",
        kind=kind,
        hypothesis_id=None,
        target=target,
        parameters=parameters,
        action_digest=digest,
        expected_revision=0,
    )
    raw = canonical_json_bytes(payload)
    evidence = probe_result_to_evidence(
        build_probe_result(
            run_id=RUN,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            invocation_id=evidence_id,
            action_digest=digest,
            probe_kind=kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=datetime(2026, 8, 5, tzinfo=UTC),
            completed_at=datetime(2026, 8, 5, tzinfo=UTC),
            summary="result expectation certificate",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=row_count,
                bytes=len(raw),
            ),
            row_count=row_count,
            truncated=truncated,
            payload=payload,
        ),
        action,
    )
    assert evidence is not None
    return action, evidence


def _state_for(
    action: ResearchAction,
    evidence: object,
    *,
    filter_binding: bool = False,
    expectations: tuple[ResultExpectation, ...] = (),
) -> ResearchState:
    base = _state()
    column = _column()
    if filter_binding:
        predicate = PredicateRef(
            left=column,
            operator=PredicateOperator.EQ,
            right=LiteralValue(value="cancelled"),
        )
        binding = DiscriminatorValueBinding(
            binding_id="binding-1",
            source_id="source-1",
            tables=(column.table,),
            columns=(column,),
            predicates=(predicate,),
            join_path=(),
            evidence_ids=(evidence.evidence_id,),
            confidence=1.0,
            status=BindingStatus.SUPPORTED,
            validator_rule="semantic-certificate:v1:discriminator_value",
            discriminator_column=column,
            discriminator_predicate=predicate,
        )
        kind = SemanticItemKind.FILTER
        operator = PredicateOperator.EQ
        literal = LiteralValue(value="cancelled")
    else:
        binding = PhysicalColumnBinding(
            binding_id="binding-1",
            source_id="source-1",
            tables=(column.table,),
            columns=(column,),
            predicates=(),
            join_path=(),
            evidence_ids=(evidence.evidence_id,),
            confidence=1.0,
            status=BindingStatus.SUPPORTED,
            validator_rule="semantic-certificate:v1:physical_column",
            physical_column=column,
        )
        kind = SemanticItemKind.DIMENSION
        operator = None
        literal = None
    item = SemanticItem(
        source_id="source-1",
        kind=kind,
        source_text="orders",
        normalized_meaning="status",
        required=True,
        operator=operator,
        literal_or_reference=literal,
        status=SemanticItemStatus.RESOLVED,
        binding_ids=(binding.binding_id,),
    )
    query = base.query_spec.model_copy(update={"semantic_items": (item,)})
    return ResearchState(
        run_id=RUN,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=query,
        hypotheses=(),
        evidence=(evidence,),
        bindings=(binding,),
        join_candidates=(),
        unresolved_items=(),
        action_history=(action,),
        budget_state=_budget(),
        stop_reason=None,
        result_expectations=expectations,
    )


def test_derive_result_expectations_accepts_only_four_closed_certificates() -> None:
    column = _column()
    cases = (
        (
            ResearchActionKind.SEARCH_VALUE,
            {"rows": []},
            (("top_k", 10), ("value", "cancelled")),
            True,
            (ResultExpectationKind.FILTER_MATCH_ABSENT,),
        ),
        (
            ResearchActionKind.INSPECT_COLUMN,
            {
                "status": "matched",
                "column": column.model_dump(mode="json", by_alias=True),
                "metadata": {"not_null": "True", "is_primary_key": True},
            },
            (),
            False,
            (
                ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
                ResultExpectationKind.DIRECT_OUTPUT_PRIMARY_KEY_UNIQUE,
            ),
        ),
        (
            ResearchActionKind.DISTINCT_VALUES,
            {"rows": [["cancelled"], ["paid"]]},
            (("top_k", 10),),
            False,
            (ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,),
        ),
    )
    for index, (kind, payload, parameters, filter_binding, expected) in enumerate(cases):
        action, evidence = _action_and_evidence(
            kind,
            column,
            payload,
            evidence_id=f"evidence-{index}",
            parameters=parameters,
        )
        state = _state_for(action, evidence, filter_binding=filter_binding)

        expectations = derive_result_expectations(state, action, evidence)

        assert tuple(item.kind for item in expectations) == expected
        assert all(item.source_id == "source-1" for item in expectations)
        assert all(item.evidence_id == evidence.evidence_id for item in expectations)
        assert all(item.column == column for item in expectations)


@pytest.mark.parametrize(
    "rows",
    (
        [["cancelled"], ["paid"]],
        [["paid"], ["cancelled"]],
    ),
)
def test_distinct_values_does_not_imply_not_null_and_has_canonical_domain(
    rows: list[list[str]],
) -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {"rows": rows},
        evidence_id="evidence-domain",
        parameters=(("top_k", 10),),
    )

    expectations = derive_result_expectations(_state_for(action, evidence), action, evidence)

    assert tuple(item.kind for item in expectations) == (
        ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
    )
    assert expectations[0].allowed_values == ("cancelled", "paid")


def test_empty_distinct_values_derives_no_expectation() -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {"rows": []},
        evidence_id="evidence-empty-domain",
        parameters=(("top_k", 10),),
    )

    assert derive_result_expectations(_state_for(action, evidence), action, evidence) == ()


def test_production_initial_research_state_starts_with_no_expectations() -> None:
    state = _build_initial_research_state(
        _state().query_spec,
        schema_namespace_version=SCHEMA,
        budget_state=_budget(),
    )

    assert state.result_expectations == ()


@pytest.mark.parametrize(
    ("kind", "payload", "parameters"),
    (
        (
            ResearchActionKind.DISTINCT_VALUES,
            {"rows": [["paid", "cancelled"]]},
            (("top_k", 10),),
        ),
    ),
)
def test_claimed_certificate_fails_closed_when_payload_is_not_exact(
    kind: ResearchActionKind,
    payload: object,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...],
) -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        kind,
        column,
        payload,
        evidence_id="evidence-invalid",
        parameters=parameters,
    )

    with pytest.raises(SemanticReducerError):
        derive_result_expectations(
            _state_for(action, evidence, filter_binding=kind is ResearchActionKind.SEARCH_VALUE),
            action,
            evidence,
        )


def test_incomplete_certificate_returns_no_expectations() -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {"rows": [["cancelled"], ["paid"]]},
        evidence_id="evidence-truncated",
        parameters=(("top_k", 2),),
        truncated=True,
    )

    assert derive_result_expectations(_state_for(action, evidence), action, evidence) == ()


@pytest.mark.parametrize("not_null", ("False", "unsupported"))
def test_inspect_column_without_declared_not_null_derives_no_not_null(
    not_null: str,
) -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": not_null},
        },
        evidence_id=f"evidence-not-null-{not_null}",
    )

    assert derive_result_expectations(_state_for(action, evidence), action, evidence) == ()


def test_execute_probe_is_not_a_result_expectation_certificate() -> None:
    target = QueryProbeRef(probe_id="probe-1", namespace="main")
    action, evidence = _action_and_evidence(
        ResearchActionKind.EXECUTE_PROBE,
        target,
        {"rows": [["cancelled"]]},
        evidence_id="evidence-execute",
    )

    assert derive_result_expectations(_state_for(action, evidence), action, evidence) == ()


def test_claimed_certificate_fails_closed_for_wrong_action_or_target() -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True"},
        },
        evidence_id="evidence-action",
    )
    other = ColumnRef(table=column.table, column="other_status")

    with pytest.raises(SemanticReducerError):
        derive_result_expectations(
            _state_for(action, evidence),
            action.model_copy(update={"target": other}),
            evidence,
        )
    with pytest.raises(SemanticReducerError):
        derive_result_expectations(
            _state_for(action, evidence),
            action,
            evidence.model_copy(update={"target": other}),
        )


@pytest.mark.parametrize("field", ("run_id", "run_incarnation", "schema_namespace_version"))
def test_claimed_certificate_fails_closed_when_evidence_scope_differs(field: str) -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True"},
        },
        evidence_id="evidence-scope",
    )
    wrong = evidence.model_copy(update={field: "wrong-scope"})

    with pytest.raises(SemanticReducerError):
        derive_result_expectations(_state_for(action, evidence), action, wrong)


def test_result_expectation_closes_its_domain_contract() -> None:
    column = _column()
    base = {
        "source_id": "source-1",
        "evidence_id": "evidence-1",
        "column": column,
    }
    assert ResultExpectation(
        **base,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
        allowed_values=("cancelled", "paid"),
    ).allowed_values == ("cancelled", "paid")

    with pytest.raises(ValidationError):
        ResultExpectation(
            **base,
            kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
            allowed_values=(),
        )
    with pytest.raises(ValidationError):
        ResultExpectation(
            **base,
            kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
            allowed_values=("paid", "cancelled"),
        )
    with pytest.raises(ValidationError):
        ResultExpectation(
            **base,
            kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
            allowed_values=("paid", None),
        )
    with pytest.raises(ValidationError):
        ResultExpectation(
            **base,
            kind=ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
            allowed_values=("paid",),
        )


def test_research_state_requires_referenced_unique_expectations() -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True"},
        },
        evidence_id="evidence-state",
    )
    expectation = ResultExpectation(
        source_id="source-1",
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
        column=column,
    )
    _state_for(action, evidence, expectations=(expectation,))

    with pytest.raises(ValidationError):
        _state_for(action, evidence, expectations=(expectation, expectation))
    with pytest.raises(ValidationError):
        _state_for(
            action,
            evidence,
            expectations=(expectation.model_copy(update={"source_id": "unknown"}),),
        )
    with pytest.raises(ValidationError):
        _state_for(
            action,
            evidence,
            expectations=(expectation.model_copy(update={"evidence_id": "unknown"}),),
        )


def test_research_state_serialization_requires_result_expectations() -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True"},
        },
        evidence_id="evidence-required-field",
    )
    serialized = _state_for(action, evidence).model_dump(mode="json", by_alias=True)
    serialized.pop("result_expectations")

    with pytest.raises(ValidationError):
        ResearchState.model_validate(serialized)
