"""The structural Typed boundary authenticates one parsed sqlglot AST."""

from dataclasses import replace

import pytest

from custom_tools.text_to_sql.adaptive.checks import (
    DeterministicCheckInput,
    DeterministicCheckInputError,
    require_typed_check_result,
)
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    RepairKind,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate


def _candidate_and_ast() -> tuple[SqlCandidate, object]:
    sql = "SELECT o.id FROM orders o"
    parsed_ast = parse_sql_candidate(
        sql,
        "postgresql://user:password@localhost:5432/example",
        "candidate-1",
    )
    return (
        SqlCandidate(
            candidate_id="candidate-1",
            sql=sql,
            normalized_ast_digest=parsed_ast.candidate_digest,
            revision=1,
        ),
        parsed_ast,
    )


def test_deterministic_check_input_requires_exact_candidate_ast_link() -> None:
    candidate, parsed_ast = _candidate_and_ast()

    value = DeterministicCheckInput(candidate=candidate, parsed_ast=parsed_ast)

    assert value.candidate.candidate_id == "candidate-1"


def test_deterministic_check_input_rejects_candidate_sql_forged_under_digest() -> None:
    candidate, parsed_ast = _candidate_and_ast()

    with pytest.raises(DeterministicCheckInputError) as raised:
        DeterministicCheckInput(
            candidate=candidate.model_copy(update={"sql": "SELECT o.status FROM orders o"}),
            parsed_ast=parsed_ast,
        )

    assert raised.value.code is CheckFailureCode.CHECK_INPUT_INVALID


def test_deterministic_check_input_rejects_ast_payload_forged_under_digest() -> None:
    candidate, parsed_ast = _candidate_and_ast()
    projection = parsed_ast.projections[0]
    forged_expression = replace(
        projection.expression,
        attributes=tuple(
            (name, "status") if name == "name" else (name, value)
            for name, value in projection.expression.attributes
        ),
    )
    forged_ast = replace(
        parsed_ast,
        projections=(replace(projection, expression=forged_expression),),
    )

    with pytest.raises(DeterministicCheckInputError) as raised:
        DeterministicCheckInput(candidate=candidate, parsed_ast=forged_ast)

    assert raised.value.code is CheckFailureCode.CHECK_INPUT_INVALID


def test_typed_seam_rejects_legacy_free_text_repair_without_guessing() -> None:
    candidate, parsed_ast = _candidate_and_ast()
    legacy = CheckResult(
        check_id="check-1",
        candidate_id=candidate.candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.MISSING_FILTER,
        affected_source_ids=(),
        affected_ast_node_ids=(parsed_ast.projections[0].node_id,),
        observed_error=None,
        required_change="add filter",
    )
    typed = CheckResult(
        check_id="check-2",
        candidate_id=candidate.candidate_id,
        check_kind=CheckKind.SAFETY,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.SAFETY_REJECTED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )
    passed = CheckResult(
        check_id="check-3",
        candidate_id=candidate.candidate_id,
        check_kind=CheckKind.SEMANTIC,
        status=CheckStatus.PASSED,
        failure_code=None,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
    )

    with pytest.raises(DeterministicCheckInputError) as raised:
        require_typed_check_result(legacy)
    assert raised.value.code is CheckFailureCode.CHECK_INPUT_INVALID
    assert require_typed_check_result(typed) == typed
    assert require_typed_check_result(passed) == passed
