"""RED contract for R4c-2 post-execution result validation."""

from __future__ import annotations

from datetime import UTC, datetime

from custom_tools.text_to_sql.adaptive.evidence import probe_result_to_evidence
from custom_tools.text_to_sql.adaptive.models import ResultExpectationKind
from custom_tools.text_to_sql.adaptive.result_validation import (
    ResultContradictionFinding,
    ResultContradictionReceipt,
    _vertical_join_path_matches,
    validate_execution_result_expectations,
)
from custom_tools.text_to_sql.adaptive.freshness import FreshnessContext
from custom_tools.text_to_sql.adaptive.models import (
    EvidenceCost,
    Hypothesis,
    HypothesisStatus,
    LiteralValue,
    PredicateOperator,
    ResearchAction,
    ResearchActionKind,
    ResultExpectation,
    SemanticItemKind,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus, build_probe_result
from custom_tools.text_to_sql.adaptive.semantic_coverage import validate_coverage_inputs
from custom_tools.text_to_sql.adaptive.serialization import canonical_json_bytes
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from test_text_to_sql_result_expectations import _action_and_evidence, _column, _state_for
from test_text_to_sql_semantic_reducer import INCARNATION, RUN, SCHEMA
from test_text_to_sql_semantic_coverage_subtypes import (
    _state_with_binding_joins,
    _vertical_binding,
)
from text_to_sql_semantic_checks_helpers import POSTGRES_DSN
from tests.text_to_sql_semantic_coverage_helpers import _schema_evidence, _value_evidence


def test_vertical_join_validation_compares_physical_predicate_endpoints_only() -> None:
    binding = _vertical_binding()
    value_table = binding.value_table.model_copy(update={"table": "value_rows"})
    value_entity_key = binding.value_entity_key.model_copy(update={"table": value_table})
    value_attribute_key = binding.value_attribute_key.model_copy(
        update={"table": value_table}
    )
    value_predicate = binding.value_predicate.model_copy(
        update={
            "left": binding.value_predicate.left.model_copy(update={"table": value_table})
        }
    )
    binding = binding.model_copy(
        update={
            "tables": (
                binding.entity_table,
                binding.attribute_catalog_table,
                value_table,
            ),
            "columns": (
                binding.entity_key,
                binding.attribute_catalog_key,
                binding.attribute_name_predicate.left,
                value_entity_key,
                value_attribute_key,
                value_predicate.left,
            ),
            "predicates": (binding.attribute_name_predicate, value_predicate),
            "join_path": (
                binding.join_path[0].model_copy(update={"right": value_entity_key}),
                binding.join_path[1].model_copy(update={"right": value_attribute_key}),
            ),
            "value_table": value_table,
            "value_entity_key": value_entity_key,
            "value_attribute_key": value_attribute_key,
            "value_predicate": value_predicate,
        }
    )
    parsed = parse_sql_candidate(
        "SELECT v.number_value FROM entities e "
        "ASOF JOIN value_rows v ON e.id = v.entity_id "
        "JOIN attributes a ON a.id = v.attribute_id",
        POSTGRES_DSN,
        "candidate-1",
    )
    relation_tables = {
        scan.relation_id: next(table for table in binding.tables if table.table == scan.table.name)
        for scan in parsed.table_scans
    }

    assert _vertical_join_path_matches(parsed, relation_tables, binding)


def test_result_validation_exposes_only_the_approved_closed_seam() -> None:
    assert callable(validate_execution_result_expectations)
    assert ResultContradictionFinding.__module__ == (
        "custom_tools.text_to_sql.adaptive.result_validation"
    )


def test_direct_output_expectations_use_root_projection_rows_only() -> None:
    column = _column()
    action, evidence = _action_and_evidence(
        ResearchActionKind.INSPECT_COLUMN,
        column,
        {
            "status": "matched",
            "column": column.model_dump(mode="json", by_alias=True),
            "metadata": {"not_null": "True", "is_primary_key": False},
        },
        evidence_id="result-validation-not-null",
    )
    state = _state_for(action, evidence)
    expectation = ResultExpectation(
        source_id="source-1",
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
        column=column,
    )
    state = state.model_validate(
        {**state.model_dump(mode="python"), "result_expectations": (expectation,)}
    )
    freshness = FreshnessContext(
        evaluated_at=evidence.observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    finding = validate_execution_result_expectations(
        state=state,
        requirements=validate_coverage_inputs(state, freshness, state.run_id, state.run_incarnation),
        freshness_context=freshness,
        candidate=SqlCandidate(
            candidate_id="candidate-1",
            sql="SELECT o.status AS state FROM orders o",
            normalized_ast_digest=(
                parsed := parse_sql_candidate(
                    "SELECT o.status AS state FROM orders o", POSTGRES_DSN, "candidate-1"
                )
            ).candidate_digest,
            revision=state.revision,
        ),
        parsed_ast=parsed,
        columns=["state"],
        data=[[None]],
    )
    assert finding is not None
    assert finding.expectation == expectation
    assert finding.output_index == 0


def _validated_finding(state, expectation, sql, columns, data):
    state = state.model_validate(
        {**state.model_dump(mode="python"), "result_expectations": (expectation,)}
    )
    evidence = next(item for item in state.evidence if item.evidence_id == expectation.evidence_id)
    freshness = FreshnessContext(evaluated_at=evidence.observed_at, run_id=state.run_id, run_incarnation=state.run_incarnation, schema_namespace_version=state.schema_namespace_version)
    parsed = parse_sql_candidate(sql, POSTGRES_DSN, "candidate-1")
    return validate_execution_result_expectations(state=state, requirements=validate_coverage_inputs(state, freshness, state.run_id, state.run_incarnation), freshness_context=freshness, candidate=SqlCandidate(candidate_id="candidate-1", sql=sql, normalized_ast_digest=parsed.candidate_digest, revision=state.revision), parsed_ast=parsed, columns=columns, data=data)


def _sequential_probe_evidence(
    kind,
    target,
    payload,
    *,
    evidence_id,
    expected_revision,
    parameters,
    hypothesis_id=None,
    action_digest=None,
    run_id=RUN,
    run_incarnation=INCARNATION,
    schema_namespace_version=SCHEMA,
):
    action_digest = action_digest or canonical_action_digest(
        kind=kind,
        hypothesis_id=hypothesis_id,
        target=target,
        parameters=parameters,
        expected_revision=expected_revision,
    )
    action = ResearchAction(
        action_id=f"action-{evidence_id}",
        kind=kind,
        hypothesis_id=hypothesis_id,
        target=target,
        parameters=parameters,
        action_digest=action_digest,
        expected_revision=expected_revision,
    )
    payload_bytes = canonical_json_bytes(payload)
    evidence = probe_result_to_evidence(
        build_probe_result(
            run_id=run_id,
            run_incarnation=run_incarnation,
            revision=expected_revision,
            schema_namespace_version=schema_namespace_version,
            invocation_id=evidence_id,
            action_digest=action.action_digest,
            probe_kind=kind,
            status=ProbeStatus.SUCCESS,
            target=target,
            started_at=datetime(2026, 8, 5, tzinfo=UTC),
            completed_at=datetime(2026, 8, 5, tzinfo=UTC),
            summary="sequential result validation fixture",
            cost=EvidenceCost(
                wall_clock_ms=0,
                model_calls=0,
                model_tokens=0,
                db_probe_ms=0,
                rows=len(payload["rows"]),
                bytes=len(payload_bytes),
            ),
            row_count=len(payload["rows"]),
            truncated=False,
            payload=payload,
        ),
        action,
    )
    assert evidence is not None
    return action, evidence


def _refresh_hypothesis(hypothesis_id, column):
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        source_ids=("source-1",),
        claim="Refresh the probe result for the selected column.",
        candidate_targets=(column,),
        status=HypothesisStatus.TESTING,
        evidence_ids=(),
    )


def test_filter_absence_rejects_rows_but_not_count_aggregate() -> None:
    column = _column()
    parameters = (("top_k", 10), ("value", "cancelled"))
    action0, evidence0 = _sequential_probe_evidence(
        ResearchActionKind.SEARCH_VALUE,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.SEARCH_VALUE.value,
            "schema_namespace_version": SCHEMA,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [["cancelled"]],
        },
        evidence_id="filter-positive",
        expected_revision=0,
        parameters=parameters,
    )
    state = _state_for(action0, evidence0, filter_binding=True)
    action1, evidence1 = _sequential_probe_evidence(
        ResearchActionKind.SEARCH_VALUE,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.SEARCH_VALUE.value,
            "schema_namespace_version": SCHEMA,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [],
        },
        evidence_id="filter-absence",
        expected_revision=1,
        parameters=parameters,
        hypothesis_id="filter-refresh",
    )
    schema = _schema_evidence(
        "filter-schema",
        column,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        revision=1,
        provenance_schema=state.schema_namespace_version,
    )
    binding = state.bindings[0].model_copy(
        update={"evidence_ids": (schema.evidence_id, evidence0.evidence_id)}
    )
    state = state.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": 2,
            "evidence": (schema, evidence0, evidence1),
            "bindings": (binding,),
            "hypotheses": (_refresh_hypothesis("filter-refresh", column),),
            "action_history": (action0, action1),
        }
    )
    expectation = ResultExpectation(source_id="source-1", evidence_id=evidence1.evidence_id, kind=ResultExpectationKind.FILTER_MATCH_ABSENT, column=column)
    assert _validated_finding(state, expectation, "SELECT o.status FROM orders o WHERE o.status = 'cancelled'", ["status"], [["cancelled"]]).output_index is None
    assert _validated_finding(state, expectation, "SELECT COUNT(*) FROM orders o WHERE o.status = 'cancelled'", ["count"], [[1]]) is None


def test_vertical_filter_absence_rejects_returned_rows() -> None:
    binding = _vertical_binding()
    value_table = binding.value_table.model_copy(update={"table": "value_rows"})
    value_entity_key = binding.value_entity_key.model_copy(update={"table": value_table})
    value_attribute_key = binding.value_attribute_key.model_copy(
        update={"table": value_table}
    )
    value_predicate = binding.value_predicate.model_copy(
        update={
            "left": binding.value_predicate.left.model_copy(
                update={"table": value_table}
            )
        }
    )
    binding = binding.model_copy(
        update={
            "tables": (
                binding.entity_table,
                binding.attribute_catalog_table,
                value_table,
            ),
            "columns": (
                binding.entity_key,
                binding.attribute_catalog_key,
                binding.attribute_name_predicate.left,
                value_entity_key,
                value_attribute_key,
                value_predicate.left,
            ),
            "predicates": (binding.attribute_name_predicate, value_predicate),
            "join_path": (
                binding.join_path[0].model_copy(update={"right": value_entity_key}),
                binding.join_path[1].model_copy(
                    update={"right": value_attribute_key}
                ),
            ),
            "value_table": value_table,
            "value_entity_key": value_entity_key,
            "value_attribute_key": value_attribute_key,
            "value_predicate": value_predicate,
        }
    )
    base = _state_with_binding_joins(binding)
    column = binding.value_predicate.left
    assert column == binding.value_predicate.left
    schema_evidence = tuple(
        _schema_evidence(
            f"vertical-schema-{index}",
            schema_column,
            run_id=base.run_id,
            run_incarnation=base.run_incarnation,
            revision=base.revision,
            provenance_schema=base.schema_namespace_version,
        )
        for index, schema_column in enumerate(binding.columns)
    )
    catalog_evidence = _value_evidence(
        "vertical-catalog",
        binding.attribute_name_predicate.left,
        "priority",
    )
    value_evidence = _value_evidence("vertical-value", column, 10)
    binding = binding.model_copy(
        update={
            "evidence_ids": (
                *(item.evidence_id for item in schema_evidence),
                catalog_evidence.evidence_id,
                value_evidence.evidence_id,
            )
        }
    )
    item = base.query_spec.semantic_items[0].model_copy(
        update={
            "kind": SemanticItemKind.FILTER,
            "operator": PredicateOperator.EQ,
            "literal_or_reference": LiteralValue(value=10),
        }
    )
    query_spec = base.query_spec.model_copy(update={"semantic_items": (item,)})
    action, evidence = _sequential_probe_evidence(
        ResearchActionKind.SEARCH_VALUE,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.SEARCH_VALUE.value,
            "schema_namespace_version": base.schema_namespace_version,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [],
        },
        evidence_id="vertical-filter-absence",
        expected_revision=base.revision,
        parameters=(("top_k", 10), ("value", 10)),
        run_id=base.run_id,
        run_incarnation=base.run_incarnation,
        schema_namespace_version=base.schema_namespace_version,
    )
    state = base.model_validate(
        {
            **base.model_dump(mode="python"),
            "revision": base.revision + 1,
            "query_spec": query_spec,
            "evidence": (
                *base.evidence,
                *schema_evidence,
                catalog_evidence,
                value_evidence,
                evidence,
            ),
            "bindings": (binding,),
            "action_history": (*base.action_history, action),
            "result_expectations": (),
        }
    )
    expectation = ResultExpectation(
        source_id=binding.source_id,
        evidence_id=evidence.evidence_id,
        kind=ResultExpectationKind.FILTER_MATCH_ABSENT,
        column=column,
    )
    freshness = FreshnessContext(
        evaluated_at=evidence.observed_at,
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
    )
    state = state.model_validate(
        {
            **state.model_dump(mode="python"),
            "result_expectations": (expectation,),
        }
    )
    requirements = validate_coverage_inputs(
        state,
        freshness,
        state.run_id,
        state.run_incarnation,
    )
    assert tuple(item.source_id for item in requirements.selected_bindings) == (
        binding.source_id,
    )
    parsed = parse_sql_candidate(
        "SELECT e.id FROM entities e JOIN value_rows v ON e.id=v.entity_id "
        "JOIN attributes a ON a.id=v.attribute_id "
        "WHERE a.name='priority' AND v.number_value=10",
        POSTGRES_DSN,
        "candidate-1",
    )
    finding = validate_execution_result_expectations(
        state=state,
        requirements=requirements,
        freshness_context=freshness,
        candidate=SqlCandidate(
            candidate_id="candidate-1",
            sql="SELECT e.id FROM entities e JOIN value_rows v ON e.id=v.entity_id "
            "JOIN attributes a ON a.id=v.attribute_id "
            "WHERE a.name='priority' AND v.number_value=10",
            normalized_ast_digest=parsed.candidate_digest,
            revision=state.revision,
        ),
        parsed_ast=parsed,
        columns=["id"],
        data=[[1]],
    )
    assert finding is not None
    assert finding.output_index is None


def test_domain_and_primary_key_check_direct_rows_only() -> None:
    column = _column()
    domain_action, domain_evidence = _action_and_evidence(ResearchActionKind.DISTINCT_VALUES, column, {"rows": [["cancelled"]]}, evidence_id="domain", parameters=(("top_k", 10),))
    domain = ResultExpectation(source_id="source-1", evidence_id=domain_evidence.evidence_id, kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN, column=column, allowed_values=("cancelled",))
    assert _validated_finding(_state_for(domain_action, domain_evidence), domain, "SELECT o.status FROM orders o", ["status"], [[None]]) is None
    assert _validated_finding(_state_for(domain_action, domain_evidence), domain, "SELECT o.status FROM orders o", ["status"], [["other"]]).output_index == 0
    int_action, int_evidence = _action_and_evidence(ResearchActionKind.DISTINCT_VALUES, column, {"rows": [[1]]}, evidence_id="int-domain", parameters=(("top_k", 10),))
    int_domain = ResultExpectation(source_id="source-1", evidence_id=int_evidence.evidence_id, kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN, column=column, allowed_values=(1,))
    assert _validated_finding(_state_for(int_action, int_evidence), int_domain, "SELECT o.status FROM orders o", ["status"], [[True]]).output_index == 0


def test_domain_expectation_is_ignored_after_same_probe_is_superseded() -> None:
    column = _column()
    parameters = (("top_k", 10),)
    action0, evidence0 = _sequential_probe_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.DISTINCT_VALUES.value,
            "schema_namespace_version": SCHEMA,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [["cancelled"]],
        },
        evidence_id="domain-old",
        expected_revision=0,
        parameters=parameters,
    )
    state = _state_for(action0, evidence0)
    action1, evidence1 = _sequential_probe_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.DISTINCT_VALUES.value,
            "schema_namespace_version": SCHEMA,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [["paid"]],
        },
        evidence_id="domain-new",
        expected_revision=1,
        parameters=parameters,
        hypothesis_id="domain-refresh",
    )
    binding = state.bindings[0].model_copy(update={"evidence_ids": (evidence1.evidence_id,)})
    state = state.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": 2,
            "evidence": (evidence0, evidence1),
            "bindings": (binding,),
            "hypotheses": (_refresh_hypothesis("domain-refresh", column),),
            "action_history": (action0, action1),
        }
    )
    old_domain = ResultExpectation(
        source_id="source-1",
        evidence_id=evidence0.evidence_id,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
        column=column,
        allowed_values=("cancelled",),
    )
    assert _validated_finding(
        state,
        old_domain,
        "SELECT o.status FROM orders o",
        ["status"],
        [["other"]],
    ) is None


def test_domain_expectation_is_not_superseded_by_noncanonical_action_digest() -> None:
    column = _column()
    parameters = (("top_k", 10),)
    action0, evidence0 = _sequential_probe_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.DISTINCT_VALUES.value,
            "schema_namespace_version": SCHEMA,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [["cancelled"]],
        },
        evidence_id="domain-canonical",
        expected_revision=0,
        parameters=parameters,
    )
    state = _state_for(action0, evidence0)
    invalid_digest = "sha256:" + "f" * 64
    action1, evidence1 = _sequential_probe_evidence(
        ResearchActionKind.DISTINCT_VALUES,
        column,
        {
            "columns": [column.column],
            "probe_kind": ResearchActionKind.DISTINCT_VALUES.value,
            "schema_namespace_version": SCHEMA,
            "target": column.model_dump(mode="json", by_alias=True),
            "rows": [["paid"]],
        },
        evidence_id="domain-noncanonical",
        expected_revision=1,
        parameters=parameters,
        action_digest=invalid_digest,
    )
    assert action1.action_digest != canonical_action_digest(
        kind=action1.kind,
        hypothesis_id=action1.hypothesis_id,
        target=action1.target,
        parameters=action1.parameters,
        expected_revision=action1.expected_revision,
    )
    binding = state.bindings[0].model_copy(update={"evidence_ids": (evidence1.evidence_id,)})
    state = state.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": 2,
            "evidence": (evidence0, evidence1),
            "bindings": (binding,),
            "action_history": (action0, action1),
        }
    )
    old_domain = ResultExpectation(
        source_id="source-1",
        evidence_id=evidence0.evidence_id,
        kind=ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
        column=column,
        allowed_values=("cancelled",),
    )
    assert _validated_finding(
        state,
        old_domain,
        "SELECT o.status FROM orders o",
        ["status"],
        [["other"]],
    ).output_index == 0


def test_primary_key_check_direct_rows_only() -> None:
    column = _column()
    pk_action, pk_evidence = _action_and_evidence(ResearchActionKind.INSPECT_COLUMN, column, {"status": "matched", "column": column.model_dump(mode="json", by_alias=True), "metadata": {"not_null": "True", "is_primary_key": True}}, evidence_id="pk")
    pk = ResultExpectation(source_id="source-1", evidence_id=pk_evidence.evidence_id, kind=ResultExpectationKind.DIRECT_OUTPUT_PRIMARY_KEY_UNIQUE, column=column)
    assert _validated_finding(_state_for(pk_action, pk_evidence), pk, "SELECT o.status FROM orders o", ["status"], [["a"], ["a"]]).output_index == 0
    assert _validated_finding(_state_for(pk_action, pk_evidence), pk, "SELECT o.status FROM orders o", ["status"], [[None], [None]]) is None
    assert _validated_finding(_state_for(pk_action, pk_evidence), pk, "SELECT COUNT(*) FROM orders o", ["count"], [[2]]) is None
    assert ResultContradictionReceipt.__module__ == (
        "custom_tools.text_to_sql.adaptive.result_validation"
    )
    assert tuple(ResultExpectationKind) == (
        ResultExpectationKind.FILTER_MATCH_ABSENT,
        ResultExpectationKind.DIRECT_OUTPUT_NOT_NULL,
        ResultExpectationKind.DIRECT_OUTPUT_NON_NULL_VALUE_DOMAIN,
        ResultExpectationKind.DIRECT_OUTPUT_PRIMARY_KEY_UNIQUE,
    )
