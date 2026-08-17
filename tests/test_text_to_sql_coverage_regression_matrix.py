"""W5-06 persisted regression evidence from the actual W5-05 receipt."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from custom_tools.text_to_sql import core
from custom_tools.text_to_sql.adaptive import (
    semantic_coverage as semantic_coverage_module,
    semantic_plan as semantic_plan_module,
    sql_ast as sql_ast_module,
)
from custom_tools.text_to_sql.adaptive.models import (
    CheckFailureCode,
    CheckKind,
    CheckStatus,
    PredicateOperator,
    AstSemanticCoverage,
    SemanticItemKind,
)
from custom_tools.text_to_sql.adaptive.pre_execution_gate import (
    PreExecutionGateReceipt,
    create_pre_execution_gate_capability,
    evaluate_pre_execution_gate_capability,
)
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.eval import (
    pre_execution_gate_coverage_record,
    write_pre_execution_gate_coverage_jsonl,
)
from tests.fixtures.text_to_sql_adaptive import sqlite as adaptive_sqlite
from text_to_sql_semantic_checks_helpers import (
    ItemSpec,
    VerticalCaseSpec,
    build_state,
    build_vertical_state,
    inner_join,
)
from text_to_sql_semantic_coverage_helpers import (
    INCARNATION,
    RUN_ID,
    _context,
)
from workflow.deadline import DeadlineBudget


GOLDEN = Path("tests/fixtures/text_to_sql_adaptive/w5_06_coverage_regression.jsonl")
CONVENTIONAL_VALID_SQL = (
    "SELECT s.sale_value FROM sales_fact AS s "
    "JOIN branch_dim AS b ON b.branch_id = s.branch_id "
    "WHERE b.branch_label = 'branch-a'"
)
EAV_VALID_SQL = (
    "SELECT m.member_id FROM member AS m "
    "JOIN attribute_fact AS f ON m.member_id = f.member_id "
    "JOIN attribute_kind AS k ON k.attribute_id = f.attribute_id "
    "WHERE k.attribute_key = 'membership_level' AND f.value_text = 'gold'"
)
F02_SPEC = VerticalCaseSpec(
    entity_table="member",
    entity_key="member_id",
    catalog_table="attribute_kind",
    catalog_key="attribute_id",
    catalog_name="attribute_key",
    value_table="attribute_fact",
    value_entity_key="member_id",
    value_attribute_key="attribute_id",
    value_column="value_text",
    catalog_literal="membership_level",
    value_literal="gold",
)


@dataclass(frozen=True, slots=True)
class MatrixCase:
    case_id: str
    fixture_id: str
    fixture_category: str
    sql: str
    safety_passes: bool
    expected_allowed: bool
    expected_failure: CheckFailureCode | None
    expected_coverage: bool


MATRIX = (
    MatrixCase(
        "conventional-valid",
        "F01_CONVENTIONAL_STAR",
        "conventional_star",
        CONVENTIONAL_VALID_SQL,
        True,
        True,
        None,
        True,
    ),
    MatrixCase(
        "conventional-safety-reject",
        "F01_CONVENTIONAL_STAR",
        "conventional_star",
        f"{CONVENTIONAL_VALID_SQL} -- safety rejection case",
        False,
        False,
        CheckFailureCode.SAFETY_REJECTED,
        False,
    ),
    MatrixCase(
        "eav-valid",
        "F02_VERTICAL_EAV",
        "vertical_eav",
        EAV_VALID_SQL,
        True,
        True,
        None,
        True,
    ),
    MatrixCase(
        "eav-join-mismatch",
        "F02_VERTICAL_EAV",
        "vertical_eav",
        "SELECT m.member_id FROM member AS m "
        "JOIN attribute_fact AS f ON m.member_id = f.attribute_id "
        "JOIN attribute_kind AS k ON k.attribute_id = f.member_id "
        "WHERE k.attribute_key = 'membership_level' AND f.value_text = 'gold'",
        True,
        False,
        CheckFailureCode.UNAUTHORIZED_JOIN,
        True,
    ),
)


def _safe_result(*, allowed: bool) -> dict[str, object]:
    if allowed:
        return {
            "is_safe": True,
            "issues": [],
            "advisory_issues": [],
            "safety_status": "safe",
            "llm_audit": "skipped_static_only",
        }
    return {
        "is_safe": False,
        "issues": [
            {
                "issue_type": "FORBIDDEN_COMMAND",
                "description": "fixture safety rejection",
            }
        ],
        "advisory_issues": [],
        "safety_status": "unsafe",
        "llm_audit": "skipped_static_unsafe",
    }


def _authority(case: MatrixCase):
    if case.fixture_id == "F01_CONVENTIONAL_STAR":
        state = build_state(
            (
                ItemSpec(
                    source_id="sales",
                    kind=SemanticItemKind.METRIC,
                    table="sales_fact",
                    column="sale_value",
                ),
                ItemSpec(
                    source_id="branch",
                    kind=SemanticItemKind.FILTER,
                    table="branch_dim",
                    column="branch_label",
                    operator=PredicateOperator.EQ,
                    literal="branch-a",
                    join_path=(
                        inner_join(
                            "sales_fact",
                            "branch_id",
                            "branch_dim",
                            "branch_id",
                        ),
                    ),
                ),
            )
        )
    else:
        state = build_vertical_state(F02_SPEC)
    requirements = validate_coverage_inputs(
        state,
        _context(),
        RUN_ID,
        INCARNATION,
    )
    return state, requirements


def _evaluate_case(
    case: MatrixCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> PreExecutionGateReceipt:
    spec = adaptive_sqlite.get_fixture_spec(case.fixture_id)
    assert spec.category == case.fixture_category
    database = adaptive_sqlite.create_sqlite_adaptive_fixture(
        case.fixture_id,
        tmp_path / f"{case.case_id}.sqlite",
    )
    dsn = f"sqlite:///{database}"
    state, requirements = _authority(case)
    capability = create_pre_execution_gate_capability(
        state=state,
        requirements=requirements,
        dsn=dsn,
        table_namespace="main",
        expected_sql=case.sql,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        deadline=DeadlineBudget.from_duration(10),
        is_cancelled=lambda: False,
    )
    monkeypatch.setattr(
        core,
        "sql_safety_check",
        lambda *_args, **_kwargs: _safe_result(allowed=case.safety_passes),
    )
    return evaluate_pre_execution_gate_capability(
        capability,
        expected_run_id=RUN_ID,
        expected_sql=case.sql,
        safety_policy=None,
    )


def _record(
    case: MatrixCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    receipt = _evaluate_case(case, tmp_path, monkeypatch)
    return pre_execution_gate_coverage_record(
        receipt,
        case_id=case.case_id,
        fixture_id=case.fixture_id,
        fixture_category=case.fixture_category,
    )


@pytest.mark.parametrize("case", MATRIX, ids=lambda case: case.case_id)
def test_regression_matrix_retains_exact_gate_evidence(
    case: MatrixCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _evaluate_case(case, tmp_path, monkeypatch)

    assert receipt.allowed is case.expected_allowed
    assert receipt.source_coverage_available is case.expected_coverage
    assert (receipt.semantic_coverage is not None) is case.expected_coverage
    assert all(
        result.check_kind in {CheckKind.SAFETY, CheckKind.SEMANTIC}
        for result in receipt.check_results
    )
    blocking = tuple(
        result
        for result in receipt.check_results
        if result.status is not CheckStatus.PASSED
    )
    if case.expected_failure is None:
        assert receipt.primary_check_id is None
        assert blocking == ()
        assert tuple(result.check_kind for result in receipt.check_results) == (
            CheckKind.SAFETY,
            CheckKind.SEMANTIC,
        )
    else:
        assert len(blocking) == 1
        assert blocking[0].failure_code is case.expected_failure
        assert receipt.primary_check_id == blocking[0].check_id
        assert receipt.check_results[-1] == blocking[0]
    if case.expected_coverage:
        assert receipt.semantic_coverage is not None
        assert receipt.semantic_coverage.required_source_ids
    else:
        assert tuple(result.check_kind for result in receipt.check_results) == (
            CheckKind.SAFETY,
        )
        assert receipt.semantic_coverage is None


def test_coverage_record_is_a_pure_projection_of_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = MATRIX[3]
    receipt = _evaluate_case(case, tmp_path, monkeypatch)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("coverage serialization must not recompute evidence")

    monkeypatch.setattr(semantic_plan_module, "build_semantic_ast", forbidden)
    monkeypatch.setattr(semantic_coverage_module, "validate_coverage_inputs", forbidden)
    monkeypatch.setattr(sql_ast_module, "parse_sql_candidate", forbidden)

    record = pre_execution_gate_coverage_record(
        receipt,
        case_id=case.case_id,
        fixture_id=case.fixture_id,
        fixture_category=case.fixture_category,
    )

    restored = PreExecutionGateReceipt.model_validate_json(
        json.dumps(record["receipt"])
    )
    assert restored == receipt
    assert (
        AstSemanticCoverage.model_validate_json(
            json.dumps(record["receipt"]["semantic_coverage"])
        )
        == receipt.semantic_coverage
    )
    assert record["source_coverage"] == {
        "required_source_ids": list(receipt.semantic_coverage.required_source_ids),
        "evidence_ids": list(receipt.semantic_coverage.evidence_ids),
        "nodes": [
            {
                "node_id": annotation.node_id,
                "expression_field": annotation.expression_field,
                "expression_index": annotation.expression_index,
                "expression_path": [
                    {
                        "argument": segment.argument,
                        "ordinal": segment.ordinal,
                    }
                    for segment in annotation.expression_path
                ],
                "source_ids": list(annotation.source_ids),
                "evidence_ids": list(annotation.evidence_ids),
            }
            for annotation in receipt.semantic_coverage.annotations
        ],
    }


def test_coverage_record_revalidates_copied_receipt_and_nested_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = MATRIX[3]
    receipt = _evaluate_case(case, tmp_path, monkeypatch)
    assert receipt.semantic_coverage is not None
    forged_receipts = (
        receipt.model_copy(update={"candidate_id": "forged-candidate"}),
        receipt.model_copy(
            update={
                "semantic_coverage": receipt.semantic_coverage.model_copy(
                    update={"requirements_digest": "sha256:" + "f" * 64}
                )
            }
        ),
        receipt.model_copy(
            update={
                "semantic_coverage": receipt.semantic_coverage.model_copy(
                    update={"required_source_ids": ("forged-source",)}
                )
            }
        ),
        receipt.model_copy(update={"source_coverage_available": False}),
    )

    for forged in forged_receipts:
        with pytest.raises(ValueError):
            pre_execution_gate_coverage_record(
                forged,
                case_id=case.case_id,
                fixture_id=case.fixture_id,
                fixture_category=case.fixture_category,
            )


def test_writer_rejects_mismatched_source_coverage_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = MATRIX[3]
    record = _record(case, tmp_path, monkeypatch)
    record["source_coverage"] = {
        **record["source_coverage"],
        "required_source_ids": ["forged-source"],
    }
    output = tmp_path / "forged-coverage.jsonl"

    with pytest.raises(ValueError, match="source_coverage"):
        write_pre_execution_gate_coverage_jsonl(output, [record])

    assert not output.exists()


def test_regression_matrix_matches_the_persisted_golden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [_record(case, tmp_path, monkeypatch) for case in reversed(MATRIX)]
    output = tmp_path / "w5-06-coverage-regression.jsonl"

    assert write_pre_execution_gate_coverage_jsonl(output, records) == len(MATRIX)
    assert output.read_bytes() == GOLDEN.read_bytes()
