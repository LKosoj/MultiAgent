"""Strict transient semantic decisions for the adaptive research model."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.research_decision import (
    MAX_RESEARCH_DECISION_BYTES,
    BindingAssessment,
    DerivedExpressionCandidate,
    LogicalPredicate,
    NewBindingProposal,
    ResearchDecisionV1,
    parse_research_decision,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    ColumnRef,
    DocumentRef,
    DerivedExpressionBinding,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    ContractDecodeError,
    ContractValidationError,
    StateSizeLimitError,
    canonical_json_bytes,
    deserialize_as,
    serialize_contract,
)
from custom_tools.text_to_sql.adaptive.tool_registry import (
    ExecuteResearchProbeArguments,
    GetDistinctValuesArguments,
    InspectColumnArguments,
    InspectRelationshipsArguments,
    InspectTableArguments,
    ProfileColumnArguments,
    ReadSchemaEvidenceArguments,
    SampleRowsArguments,
    SearchSchemaCatalogArguments,
    SearchValueArguments,
)


EVIDENCE_ID = "evidence-1"
SOURCE_ID = "source-1"


VALID_TOOL_INTENTS: tuple[tuple[str, type, dict[str, object]], ...] = (
    (
        "search_schema_catalog",
        SearchSchemaCatalogArguments,
        {"query": "tariff", "top_k": 3},
    ),
    ("inspect_table", InspectTableArguments, {"table": "entities"}),
    (
        "inspect_column",
        InspectColumnArguments,
        {"table": "attributes", "column": "name"},
    ),
    (
        "inspect_relationships",
        InspectRelationshipsArguments,
        {"table": "values", "top_k": 5, "depth": 3},
    ),
    (
        "profile_column",
        ProfileColumnArguments,
        {"table": "values", "column": "number_value"},
    ),
    (
        "sample_rows",
        SampleRowsArguments,
        {"table": "values", "columns": ["entity_id", "number_value"], "limit": 10},
    ),
    (
        "search_value",
        SearchValueArguments,
        {"table": "attributes", "column": "name", "value": "premium", "top_k": 4},
    ),
    (
        "get_distinct_values",
        GetDistinctValuesArguments,
        {"table": "attributes", "column": "name", "top_k": 10},
    ),
    (
        "execute_research_probe",
        ExecuteResearchProbeArguments,
        {
            "sql": "SELECT entity_id FROM values ORDER BY entity_id LIMIT 2",
            "parameters": [],
        },
    ),
    (
        "read_schema_evidence",
        ReadSchemaEvidenceArguments,
        {"document_id": "schema-doc"},
    ),
)


def _tool_next(
    tool_name: str = "inspect_table",
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    if arguments is None:
        arguments = {"table": "entities"}
    return {
        "next_kind": "tool",
        "hypothesis_ref": None,
        "intent": {"tool_name": tool_name, "arguments": arguments},
    }


def _decision(
    *proposals: dict[str, object],
    next_step: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "decision_version": 1,
        "proposals": list(proposals),
        "next": next_step or _tool_next(),
    }


def _parse(mapping: dict[str, object]) -> ResearchDecisionV1:
    return parse_research_decision(json.dumps(mapping, ensure_ascii=False))


@pytest.mark.parametrize(
    ("tool_name", "arguments_type", "arguments"), VALID_TOOL_INTENTS
)
def test_each_tool_intent_reuses_the_exact_w2_argument_model(
    tool_name: str,
    arguments_type: type,
    arguments: dict[str, object],
) -> None:
    decision = _parse(_decision(next_step=_tool_next(tool_name, arguments)))

    assert decision.next.next_kind == "tool"
    assert decision.next.intent.tool_name == tool_name
    assert type(decision.next.intent.arguments) is arguments_type


def test_decision_has_no_model_authored_revision_or_runtime_identity() -> None:
    decision = _parse(_decision())
    assert set(type(decision).model_fields) == {"decision_version", "proposals", "next"}

    for forbidden in (
        "expected_revision",
        "run_id",
        "run_incarnation",
        "schema_namespace_version",
        "namespace",
        "schema",
        "dsn",
        "scope",
        "action",
        "action_id",
        "action_digest",
        "status",
        "terminal",
        "stop_reason",
    ):
        malformed = _decision()
        malformed[forbidden] = "model-controlled"
        with pytest.raises(ContractValidationError):
            _parse(malformed)


def test_all_five_candidate_binding_variants_are_transient_and_logical() -> None:
    candidates = (
        {
            "kind": "physical_column",
            "physical_column": {"table": "orders", "column": "amount"},
        },
        {
            "kind": "vertical_attribute",
            "entity_table": {"table": "entities"},
            "entity_key": {"table": "entities", "column": "id"},
            "attribute_catalog_table": {"table": "attributes"},
            "attribute_catalog_key": {"table": "attributes", "column": "id"},
            "attribute_name_predicate": {
                "left": {"table": "attributes", "column": "name"},
                "operator": "eq",
                "right": "premium",
            },
            "value_table": {"table": "values"},
            "value_entity_key": {"table": "values", "column": "entity_id"},
            "value_attribute_key": {"table": "values", "column": "attribute_id"},
            "value_predicate": {
                "left": {"table": "values", "column": "number_value"},
                "operator": "gt",
                "right": 100,
            },
        },
        {
            "kind": "discriminator_value",
            "discriminator_column": {"table": "events", "column": "event_type"},
            "discriminator_predicate": {
                "left": {"table": "events", "column": "event_type"},
                "operator": "eq",
                "right": "purchase",
            },
        },
        {
            "kind": "derived_expression",
            "expression_claim": "revenue - cost",
            "document_id": "schema-doc",
            "rule_excerpt": "gross margin is revenue minus cost",
            "input_columns": [
                {"table": "facts", "column": "revenue"},
                {"table": "facts", "column": "cost"},
            ],
        },
        {
            "kind": "document_rule",
            "document_id": "schema-doc",
            "rule_id": "gross-margin",
            "rule_text": "gross margin is revenue minus cost",
        },
    )

    proposals = [
        {
            "proposal_type": "new_binding",
            "proposal_key": f"proposal:b{index}",
            "source_id": SOURCE_ID,
            "candidate": candidate,
            "join_references": [],
            "citation_evidence_ids": [EVIDENCE_ID],
        }
        for index, candidate in enumerate(candidates)
    ]
    decision = _parse(_decision(*reversed(proposals)))

    assert [item.proposal_key for item in decision.proposals] == [
        "proposal:b0",
        "proposal:b1",
        "proposal:b2",
        "proposal:b3",
        "proposal:b4",
    ]
    for proposal in decision.proposals:
        assert isinstance(proposal, NewBindingProposal)
        dumped = json.dumps(proposal.model_dump(mode="json"), sort_keys=True)
        for forbidden in {
            "binding_id",
            "status",
            "confidence",
            "validator_rule",
            "namespace",
            "action_digest",
        }:
            assert forbidden not in dumped


def test_time_discriminator_candidate_accepts_additional_physical_predicates() -> None:
    decision = _parse(
        _decision(
            {
                "proposal_type": "new_binding",
                "proposal_key": "proposal:calendar-period",
                "source_id": SOURCE_ID,
                "candidate": {
                    "kind": "discriminator_value",
                    "discriminator_column": {"table": "events", "column": "year"},
                    "discriminator_predicate": {
                        "left": {"table": "events", "column": "year"},
                        "operator": "eq",
                        "right": 2024,
                    },
                    "additional_predicates": [
                        {
                            "left": {"table": "events", "column": "month"},
                            "operator": "eq",
                            "right": 6,
                        }
                    ],
                },
                "join_references": [],
                "citation_evidence_ids": [EVIDENCE_ID],
            }
        )
    )

    proposal = decision.proposals[0]
    assert isinstance(proposal, NewBindingProposal)
    assert [
        predicate.left.column for predicate in proposal.candidate.additional_predicates
    ] == ["month"]


def _logical_predicate(operator: str, right: object) -> LogicalPredicate:
    return LogicalPredicate.model_validate_json(
        json.dumps(
            {
                "left": {"table": "values", "column": "value"},
                "operator": operator,
                "right": right,
            }
        )
    )


@pytest.mark.parametrize("operator", ("in", "not_in"))
def test_set_predicates_require_unique_canonically_sorted_tuple(operator: str) -> None:
    first = _logical_predicate(operator, [True, "a", 1, 1.0])
    second = _logical_predicate(operator, [1.0, 1, "a", True])

    assert first == second
    assert [(type(value), value) for value in first.right] == [
        (str, "a"),
        (int, 1),
        (float, 1.0),
        (bool, True),
    ]
    with pytest.raises(ValidationError):
        _logical_predicate(operator, "a")
    with pytest.raises(ValidationError):
        _logical_predicate(operator, [True, True])
    with pytest.raises(ValidationError):
        _logical_predicate(operator, [1, 1])


def test_between_preserves_exact_two_value_order() -> None:
    predicate = _logical_predicate("between", [10, 1])

    assert predicate.right == (10, 1)
    with pytest.raises(ValidationError):
        _logical_predicate("between", 10)
    with pytest.raises(ValidationError):
        _logical_predicate("between", [1])
    with pytest.raises(ValidationError):
        _logical_predicate("between", [1, 2, 3])


@pytest.mark.parametrize(
    "operator",
    ("eq", "neq", "gt", "gte", "lt", "lte"),
)
def test_non_collection_predicates_require_one_scalar(operator: str) -> None:
    assert _logical_predicate(operator, 1).right == 1
    with pytest.raises(ValidationError):
        _logical_predicate(operator, [1, 2])
    with pytest.raises(ValidationError):
        _logical_predicate(operator, None)


def test_like_requires_text_and_null_operators_require_only_null() -> None:
    assert _logical_predicate("like", "%paid%").right == "%paid%"
    with pytest.raises(ValidationError):
        _logical_predicate("like", 1)
    with pytest.raises(ValidationError):
        _logical_predicate("like", ["paid"])

    for operator in ("is_null", "is_not_null"):
        assert _logical_predicate(operator, None).right is None
        with pytest.raises(ValidationError):
            _logical_predicate(operator, "null")


def test_derived_expression_is_only_an_untrusted_claim() -> None:
    proposal = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:derived",
        "source_id": SOURCE_ID,
        "candidate": {
            "kind": "derived_expression",
            "expression_claim": "(SELECT secret FROM private_table)",
            "document_id": "schema-doc",
            "rule_excerpt": "amount is reduced by cost",
            "input_columns": [
                {"table": "facts", "column": "amount"},
                {"table": "facts", "column": "cost"},
            ],
        },
        "join_references": [],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    candidate = _parse(_decision(proposal)).proposals[0].candidate

    assert isinstance(candidate, DerivedExpressionCandidate)
    assert "expression" not in type(candidate).model_fields
    assert (
        "untrusted"
        in (
            type(candidate).model_json_schema()["properties"]["expression_claim"][
                "description"
            ]
        ).lower()
    )
    assert not callable(getattr(candidate, "execute", None))

    table = TableRef(namespace="main", schema=None, table="facts")
    amount = ColumnRef(table=table, column="amount")
    cost = ColumnRef(table=table, column="cost")
    with pytest.raises(ValidationError) as exc_info:
        DerivedExpressionBinding(
            binding_id="binding-1",
            source_id=SOURCE_ID,
            tables=(table,),
            columns=(amount, cost),
            predicates=(),
            join_path=(),
            evidence_ids=(EVIDENCE_ID,),
            confidence=0.5,
            status=BindingStatus.CANDIDATE,
            validator_rule=None,
            expression=candidate.expression_claim,
            document=DocumentRef(document_id="schema-doc", namespace="main"),
            rule_excerpt=candidate.rule_excerpt,
            input_columns=(amount, cost),
        )
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("expression",)
    assert errors[0]["type"] == "model_type"
    assert errors[0]["ctx"] == {"class_name": "ExpressionRef"}

    malformed = copy.deepcopy(proposal)
    malformed["candidate"]["expression"] = malformed["candidate"].pop(
        "expression_claim"
    )
    with pytest.raises(ContractValidationError):
        _parse(_decision(malformed))


@pytest.mark.parametrize(
    ("input_columns", "expression_claim"),
    (
        ([{"table": "facts", "column": "c_gross"}], "ABS(c_gross)"),
        (
            [
                {"table": "facts", "column": "c_gross"},
                {"table": "facts", "column": "c_expense"},
                {"table": "facts", "column": "c_tax"},
            ],
            "(c_gross - c_expense) * c_tax",
        ),
    ),
)
def test_derived_expression_operands_preserve_nonempty_order(
    input_columns: list[dict[str, str]],
    expression_claim: str,
) -> None:
    candidate = {
        "kind": "derived_expression",
        "expression_claim": expression_claim,
        "document_id": "schema-doc",
        "rule_excerpt": "calculation uses the listed columns",
        "input_columns": input_columns,
    }
    proposal = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:ordered-derived",
        "source_id": SOURCE_ID,
        "candidate": candidate,
        "join_references": [],
        "citation_evidence_ids": [EVIDENCE_ID],
    }

    parsed = _parse(_decision(proposal)).proposals[0].candidate

    assert isinstance(parsed, DerivedExpressionCandidate)
    assert tuple(column.column for column in parsed.input_columns) == tuple(
        column["column"] for column in input_columns
    )

    for invalid_input_columns in (
        [],
        [
            {"table": "facts", "column": "c_gross"},
            {"table": "facts", "column": "c_gross"},
        ],
    ):
        invalid = copy.deepcopy(proposal)
        invalid["candidate"]["input_columns"] = invalid_input_columns
        with pytest.raises(ContractValidationError):
            _parse(_decision(invalid))


def test_hypothesis_join_and_assessments_can_reference_local_or_existing_objects() -> (
    None
):
    proposals = (
        {
            "proposal_type": "new_hypothesis",
            "proposal_key": "proposal:h1",
            "source_ids": [SOURCE_ID],
            "claim": "premium is stored as a vertical attribute",
            "candidate_targets": [
                {"target_kind": "table", "table": "values"},
                {"target_kind": "column", "table": "attributes", "column": "name"},
            ],
            "citation_evidence_ids": ["evidence-2", EVIDENCE_ID],
        },
        {
            "proposal_type": "hypothesis_assessment",
            "subject": {"reference_kind": "proposed", "proposal_key": "proposal:h1"},
            "certificate": "consistent",
            "citation_evidence_ids": [EVIDENCE_ID],
        },
        {
            "proposal_type": "new_join",
            "proposal_key": "proposal:j1",
            "left": {"table": "entities", "column": "id"},
            "right": {"table": "values", "column": "entity_id"},
            "join_type": "inner",
            "path": [
                {
                    "left": {"table": "entities", "column": "id"},
                    "right": {"table": "values", "column": "entity_id"},
                    "join_type": "inner",
                }
            ],
            "citation_evidence_ids": [EVIDENCE_ID],
        },
        {
            "proposal_type": "join_assessment",
            "subject": {"reference_kind": "existing", "join_id": "join-unknown-here"},
            "certificate": "insufficient",
            "citation_evidence_ids": ["evidence-unknown-here"],
        },
        {
            "proposal_type": "new_binding",
            "proposal_key": "proposal:b1",
            "source_id": SOURCE_ID,
            "candidate": {
                "kind": "physical_column",
                "physical_column": {"table": "values", "column": "number_value"},
            },
            "join_references": [
                {"reference_kind": "proposed", "proposal_key": "proposal:j1"},
                {"reference_kind": "existing", "join_id": "join-existing"},
            ],
            "citation_evidence_ids": [EVIDENCE_ID],
        },
        {
            "proposal_type": "binding_assessment",
            "subject": {
                "reference_kind": "existing",
                "binding_id": "binding-unknown-here",
            },
            "certificate": "contradicted",
            "citation_evidence_ids": ["evidence-unknown-here"],
        },
    )
    next_step = _tool_next("inspect_table", {"table": "values"})
    next_step["hypothesis_ref"] = {
        "reference_kind": "proposed",
        "proposal_key": "proposal:h1",
    }

    decision = _parse(_decision(*proposals, next_step=next_step))

    assert len(decision.proposals) == 6
    assert decision.next.hypothesis_ref.proposal_key == "proposal:h1"
    hypothesis = next(
        proposal
        for proposal in decision.proposals
        if proposal.proposal_type == "new_hypothesis"
    )
    assert hypothesis.citation_evidence_ids == (EVIDENCE_ID, "evidence-2")


def test_local_references_must_exist_in_same_decision_and_match_object_kind() -> None:
    assessment = {
        "proposal_type": "binding_assessment",
        "subject": {"reference_kind": "proposed", "proposal_key": "proposal:missing"},
        "certificate": "consistent",
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    with pytest.raises(ContractValidationError):
        _parse(_decision(assessment))

    hypothesis = {
        "proposal_type": "new_hypothesis",
        "proposal_key": "proposal:h1",
        "source_ids": [SOURCE_ID],
        "claim": "candidate",
        "candidate_targets": [{"target_kind": "table", "table": "values"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    wrong_kind = copy.deepcopy(assessment)
    wrong_kind["subject"] = {
        "reference_kind": "proposed",
        "proposal_key": "proposal:h1",
    }
    with pytest.raises(ContractValidationError):
        _parse(_decision(hypothesis, wrong_kind))


def test_duplicate_proposal_keys_ids_and_assessment_subjects_are_rejected() -> None:
    proposal = {
        "proposal_type": "new_hypothesis",
        "proposal_key": "proposal:h1",
        "source_ids": [SOURCE_ID],
        "claim": "candidate",
        "candidate_targets": [{"target_kind": "table", "table": "values"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    with pytest.raises(ContractValidationError):
        _parse(_decision(proposal, copy.deepcopy(proposal)))

    duplicated_ids = copy.deepcopy(proposal)
    duplicated_ids["source_ids"] = [SOURCE_ID, SOURCE_ID]
    with pytest.raises(ContractValidationError):
        _parse(_decision(duplicated_ids))

    duplicated_citations = copy.deepcopy(proposal)
    duplicated_citations["citation_evidence_ids"] = [EVIDENCE_ID, EVIDENCE_ID]
    with pytest.raises(ContractValidationError):
        _parse(_decision(duplicated_citations))

    assessment = {
        "proposal_type": "binding_assessment",
        "subject": {"reference_kind": "existing", "binding_id": "binding-1"},
        "certificate": "consistent",
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    with pytest.raises(ContractValidationError):
        _parse(_decision(assessment, copy.deepcopy(assessment)))


def test_semantically_duplicate_new_objects_ignore_local_key_and_citations() -> None:
    hypothesis = {
        "proposal_type": "new_hypothesis",
        "proposal_key": "proposal:h1",
        "source_ids": [SOURCE_ID],
        "claim": "premium is a vertical attribute",
        "candidate_targets": [{"target_kind": "table", "table": "values"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    binding = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:b1",
        "source_id": SOURCE_ID,
        "candidate": {
            "kind": "physical_column",
            "physical_column": {"table": "values", "column": "number_value"},
        },
        "join_references": [{"reference_kind": "existing", "join_id": "join-1"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    join = {
        "proposal_type": "new_join",
        "proposal_key": "proposal:j1",
        "left": {"table": "entities", "column": "id"},
        "right": {"table": "values", "column": "entity_id"},
        "join_type": "inner",
        "path": [],
        "citation_evidence_ids": [EVIDENCE_ID],
    }

    for proposal, replacement_key in (
        (hypothesis, "proposal:h2"),
        (binding, "proposal:b2"),
        (join, "proposal:j2"),
    ):
        duplicate = copy.deepcopy(proposal)
        duplicate["proposal_key"] = replacement_key
        duplicate["citation_evidence_ids"] = ["evidence-other"]
        with pytest.raises(ContractValidationError):
            _parse(_decision(proposal, duplicate))


def test_meaningful_target_join_reference_and_path_keep_proposals_distinct() -> None:
    hypothesis_a = {
        "proposal_type": "new_hypothesis",
        "proposal_key": "proposal:h1",
        "source_ids": [SOURCE_ID],
        "claim": "candidate",
        "candidate_targets": [{"target_kind": "table", "table": "a"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    hypothesis_b = copy.deepcopy(hypothesis_a)
    hypothesis_b["proposal_key"] = "proposal:h2"
    hypothesis_b["candidate_targets"] = [{"target_kind": "table", "table": "b"}]

    binding_a = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:b1",
        "source_id": SOURCE_ID,
        "candidate": {
            "kind": "physical_column",
            "physical_column": {"table": "values", "column": "number_value"},
        },
        "join_references": [{"reference_kind": "existing", "join_id": "join-1"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    binding_b = copy.deepcopy(binding_a)
    binding_b["proposal_key"] = "proposal:b2"
    binding_b["join_references"] = [{"reference_kind": "existing", "join_id": "join-2"}]

    edge = {
        "left": {"table": "entities", "column": "id"},
        "right": {"table": "values", "column": "entity_id"},
        "join_type": "inner",
    }
    join_a = {
        "proposal_type": "new_join",
        "proposal_key": "proposal:j1",
        "left": edge["left"],
        "right": edge["right"],
        "join_type": "inner",
        "path": [],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    join_b = copy.deepcopy(join_a)
    join_b["proposal_key"] = "proposal:j2"
    join_b["path"] = [edge]

    decision = _parse(
        _decision(
            hypothesis_a,
            hypothesis_b,
            binding_a,
            binding_b,
            join_a,
            join_b,
        )
    )
    assert len(decision.proposals) == 6


@pytest.mark.parametrize("reason", ("complete", "ambiguous", "unsupported"))
def test_stop_is_only_a_cited_request_not_a_terminal_status(reason: str) -> None:
    source_ids = [] if reason == "complete" else [SOURCE_ID]
    decision = _parse(
        _decision(
            next_step={
                "next_kind": "stop",
                "reason": reason,
                "source_ids": source_ids,
                "citation_evidence_ids": [EVIDENCE_ID],
                **(
                    {
                        "ambiguity": {
                            "interpretations": ["First reading.", "Second reading."],
                            "citation_evidence_ids": [EVIDENCE_ID],
                            "missing_distinguishing_fact": "The definition is absent.",
                        }
                    }
                    if reason == "ambiguous"
                    else {}
                ),
            }
        )
    )

    assert decision.next.reason == reason
    assert "terminal" not in type(decision.next).model_fields
    assert "status" not in type(decision.next).model_fields


def test_invalid_stop_and_multiple_or_malformed_next_steps_are_rejected() -> None:
    for reason, source_ids in (("done", []), ("ambiguous", []), ("unsupported", [])):
        with pytest.raises(ContractValidationError):
            _parse(
                _decision(
                    next_step={
                        "next_kind": "stop",
                        "reason": reason,
                        "source_ids": source_ids,
                        "citation_evidence_ids": [EVIDENCE_ID],
                    }
                )
            )

    malformed = _decision()
    malformed["next_actions"] = [malformed.pop("next")]
    with pytest.raises(ContractValidationError):
        _parse(malformed)

    with pytest.raises(ContractValidationError):
        _parse(_decision(next_step={"next_kind": "tool", "intent": _tool_next()}))


def test_forbidden_fields_and_evidence_objects_are_rejected_inside_proposals() -> None:
    proposal = {
        "proposal_type": "new_binding",
        "proposal_key": "proposal:b1",
        "source_id": SOURCE_ID,
        "candidate": {
            "kind": "physical_column",
            "physical_column": {"table": "orders", "column": "amount"},
        },
        "join_references": [],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    for forbidden in (
        "binding_id",
        "status",
        "action_digest",
        "namespace",
        "rationale",
    ):
        malformed = copy.deepcopy(proposal)
        malformed[forbidden] = "forbidden"
        with pytest.raises(ContractValidationError):
            _parse(_decision(malformed))

    malformed = copy.deepcopy(proposal)
    malformed["citation_evidence_ids"] = [
        {"evidence_id": EVIDENCE_ID, "observation": "model-authored"}
    ]
    with pytest.raises(ContractValidationError):
        _parse(_decision(malformed))


def test_unordered_sets_normalize_but_meaningful_tool_order_is_preserved() -> None:
    first = {
        "proposal_type": "new_hypothesis",
        "proposal_key": "proposal:h2",
        "source_ids": ["source-2", SOURCE_ID],
        "claim": "second",
        "candidate_targets": [
            {"target_kind": "table", "table": "z"},
            {"target_kind": "table", "table": "a"},
        ],
        "citation_evidence_ids": ["evidence-2", EVIDENCE_ID],
    }
    second = {
        "proposal_type": "new_hypothesis",
        "proposal_key": "proposal:h1",
        "source_ids": [SOURCE_ID],
        "claim": "first",
        "candidate_targets": [{"target_kind": "table", "table": "a"}],
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    arguments = {"table": "values", "columns": ["z", "a"], "limit": 2}
    left = _parse(
        _decision(first, second, next_step=_tool_next("sample_rows", arguments))
    )
    right = _parse(
        _decision(second, first, next_step=_tool_next("sample_rows", arguments))
    )

    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert left.proposals[1].proposal_key == "proposal:h2"
    assert left.proposals[1].source_ids == (SOURCE_ID, "source-2")
    assert [target.table for target in left.proposals[1].candidate_targets] == [
        "a",
        "z",
    ]
    assert left.next.intent.arguments.columns == ("z", "a")


def test_raw_parser_rejects_duplicate_keys_and_noncanonical_or_oversized_input() -> (
    None
):
    with pytest.raises(ContractDecodeError, match="valid JSON"):
        parse_research_decision(
            '{"decision_version":1,"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":{"table":"entities"}}}}'
        )
    with pytest.raises(ContractDecodeError, match="valid JSON"):
        parse_research_decision(
            '{"decision_version":1,"proposals":[],"next":'
            '{"next_kind":"tool","hypothesis_ref":null,"intent":'
            '{"tool_name":"inspect_table","arguments":'
            '{"table":"entities","table":"other"}}}}'
        )
    with pytest.raises(TypeError):
        parse_research_decision(_decision())  # type: ignore[arg-type]
    with pytest.raises(StateSizeLimitError):
        parse_research_decision(" " * (MAX_RESEARCH_DECISION_BYTES + 1))


def test_decision_round_trips_only_through_nonpersisted_deserialize_as() -> None:
    decision = _parse(_decision())
    payload = canonical_json_bytes(decision)

    assert parse_research_decision(payload) == decision
    assert deserialize_as(payload, ResearchDecisionV1) == decision
    with pytest.raises(ContractValidationError):
        serialize_contract(decision)  # type: ignore[arg-type]


def test_import_keeps_yaml_and_probe_implementations_lazy() -> None:
    script = """
from pathlib import Path
import sys

original_read_text = Path.read_text

def guarded_read_text(path, *args, **kwargs):
    if path.suffix == ".yaml" and path.parent.name == "research_tool_definitions":
        raise AssertionError("research tool YAML read")
    return original_read_text(path, *args, **kwargs)

Path.read_text = guarded_read_text
try:
    import custom_tools.text_to_sql.adaptive.research_decision
    assert "custom_tools.text_to_sql.adaptive.schema_probes" not in sys.modules
    assert "custom_tools.text_to_sql.adaptive.data_probes" not in sys.modules
finally:
    Path.read_text = original_read_text
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_bounds_certificates_and_tool_argument_extra_fields_are_closed() -> None:
    base = {
        "proposal_type": "binding_assessment",
        "subject": {"reference_kind": "existing", "binding_id": "binding-1"},
        "certificate": "consistent",
        "citation_evidence_ids": [EVIDENCE_ID],
    }
    assert isinstance(_parse(_decision(base)).proposals[0], BindingAssessment)

    for certificate in ("supported", "rejected", "terminal"):
        malformed = {**base, "certificate": certificate}
        with pytest.raises(ContractValidationError):
            _parse(_decision(malformed))

    with pytest.raises(ContractValidationError):
        _parse(
            _decision(
                *(
                    {
                        **base,
                        "subject": {
                            "reference_kind": "existing",
                            "binding_id": f"b-{i}",
                        },
                    }
                    for i in range(33)
                )
            )
        )

    extra_arguments = {"table": "entities", "namespace": "model-controlled"}
    with pytest.raises(ContractValidationError):
        _parse(_decision(next_step=_tool_next("inspect_table", extra_arguments)))

    with pytest.raises(ValidationError):
        SampleRowsArguments(
            table="values", columns=tuple(str(i) for i in range(21)), limit=2
        )


def test_semantic_commit_requires_a_nonempty_proposal_batch() -> None:
    committed = _parse(
        _decision(
            {
                "proposal_type": "new_hypothesis",
                "proposal_key": "proposal:orders",
                "source_ids": [SOURCE_ID],
                "claim": "orders are relevant",
                "candidate_targets": [{"target_kind": "table", "table": "orders"}],
                "citation_evidence_ids": [EVIDENCE_ID],
            },
            next_step={"next_kind": "semantic_commit"},
        )
    )

    assert committed.next.next_kind == "semantic_commit"
    with pytest.raises(ContractValidationError):
        _parse(_decision(next_step={"next_kind": "semantic_commit"}))
