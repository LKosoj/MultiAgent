"""W5-02 binds required semantic items to an authenticated plan overlay."""

from dataclasses import replace

import pytest

from custom_tools.text_to_sql.adaptive import semantic_coverage
from custom_tools.text_to_sql.adaptive.checks import SemanticCheckInput
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    CheckFailureCode,
    CheckStatus,
    DiscriminatorValueBinding,
    PhysicalColumnBinding,
    PredicateOperator,
    ResearchState,
    SemanticItemKind,
    SemanticItemStatus,
    SqlCandidate,
)
from custom_tools.text_to_sql.adaptive.semantic_checks import evaluate_semantic_authority_checks
from custom_tools.text_to_sql.adaptive.semantic_plan import (
    authenticate_semantic_ast,
    build_semantic_ast,
)
from custom_tools.text_to_sql.adaptive.sql_ast import parse_sql_candidate
from custom_tools.text_to_sql.adaptive.semantic_coverage import CoverageRequirements
from custom_tools.text_to_sql.adaptive._semantic_coverage_footprint import (
    derive_coverage_footprint,
)
from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
from text_to_sql_semantic_checks_helpers import (
    POSTGRES_DSN,
    ItemSpec,
    build_case,
    column,
    inner_join,
)
from text_to_sql_semantic_coverage_helpers import _validate
from test_text_to_sql_semantic_coverage_subtypes import (
    _state_with_binding_joins,
    _vertical_binding,
)


def _filter() -> ItemSpec:
    return ItemSpec(
        source_id="status",
        kind=SemanticItemKind.FILTER,
        table="orders",
        column="status",
        operator=PredicateOperator.EQ,
        literal="active",
    )


def _evaluate(case):
    return evaluate_semantic_authority_checks(case.check_input, case.state, POSTGRES_DSN)


def _forged_requirements(
    case,
    *,
    bindings=None,
    joins=None,
) -> CoverageRequirements:
    selected = bindings or case.requirements.selected_bindings
    eligible_joins = joins or case.requirements.eligible_validated_joins
    footprint = derive_coverage_footprint(selected, eligible_joins)
    values = case.requirements.model_dump(mode="python")
    values.update(
        selected_bindings=selected,
        eligible_validated_joins=footprint.eligible_validated_joins,
        eligible_evidence_ids=footprint.eligible_evidence_ids,
        allowed_tables=footprint.allowed_tables,
        allowed_columns=footprint.allowed_columns,
        allowed_predicates=footprint.allowed_predicates,
        allowed_join_paths=footprint.allowed_join_paths,
    )
    values.pop("requirements_digest")
    return CoverageRequirements(
        **values,
        requirements_digest=canonical_digest(values),
    )


def _check_input_for_sql(case, sql: str, requirements: CoverageRequirements):
    parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, "forged-candidate")
    candidate = SqlCandidate(
        candidate_id="forged-candidate",
        sql=sql,
        normalized_ast_digest=parsed_ast.candidate_digest,
        revision=case.state.revision,
    )
    return SemanticCheckInput(
        semantic_ast=build_semantic_ast(
            candidate,
            parsed_ast,
            case.query_spec,
            requirements,
            "main",
        ),
        query_spec=case.query_spec,
        requirements=requirements,
    )


def test_helper_builds_one_revision_action_and_exact_filter_certificate() -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (_filter(),),
    )

    assert case.state.revision == 1
    assert tuple(action.expected_revision for action in case.state.action_history) == (
        0,
    )
    binding = case.requirements.selected_bindings[0]
    assert type(binding) is DiscriminatorValueBinding
    assert len(binding.evidence_ids) == 2
    assert binding.discriminator_predicate.right == "active"
    assert (
        authenticate_semantic_ast(
            case.check_input.semantic_ast,
            case.query_spec,
            case.requirements,
        )
        == case.check_input.semantic_ast
    )


def test_coverage_annotates_only_authorized_ast_locations() -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (_filter(),),
    )
    coverage = case.check_input.semantic_ast.coverage

    assert coverage.required_source_ids == ("status",)
    assert set(coverage.evidence_ids) == set(case.requirements.eligible_evidence_ids)
    predicate = next(
        annotation
        for annotation in coverage.annotations
        if annotation.expression_field == "expression"
    )
    assert predicate.source_ids == ("status",)


def test_positive_integer_limit_needs_no_binding_but_sql_value_stays_exact() -> None:
    base = build_case(
        "SELECT 1 LIMIT 1",
        (
            ItemSpec(
                source_id="limit",
                kind=SemanticItemKind.LIMIT,
                table="orders",
                column="id",
                literal=1,
            ),
        ),
    )
    limit = base.query_spec.semantic_items[0].model_copy(
        update={"status": SemanticItemStatus.RESOLVED, "binding_ids": ()}
    )
    query_spec = base.query_spec.model_copy(update={"semantic_items": (limit,)})
    payload = base.state.model_dump(mode="python")
    payload.update(
        query_spec=query_spec,
        bindings=(),
        evidence=(),
        unresolved_items=(),
    )
    state = ResearchState.model_validate(payload)
    requirements = _validate(state)

    assert requirements.required_source_ids == ("limit",)
    assert requirements.selected_bindings == ()

    def check(sql: str):
        parsed_ast = parse_sql_candidate(sql, POSTGRES_DSN, "limit-candidate")
        candidate = SqlCandidate(
            candidate_id="limit-candidate",
            sql=sql,
            normalized_ast_digest=parsed_ast.candidate_digest,
            revision=state.revision,
        )
        return evaluate_semantic_authority_checks(
            SemanticCheckInput(
                semantic_ast=build_semantic_ast(
                    candidate,
                    parsed_ast,
                    query_spec,
                    requirements,
                    "main",
                ),
                query_spec=query_spec,
                requirements=requirements,
            ),
            state,
            POSTGRES_DSN,
        )

    assert check("SELECT 1 LIMIT 1").status is CheckStatus.PASSED
    assert check("SELECT 1 LIMIT 5").failure_code is CheckFailureCode.LIMIT_MISMATCH


def test_overlay_authentication_does_not_rederive_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (_filter(),),
    )

    def unexpected_revalidation(*_args, **_kwargs):
        raise AssertionError("coverage authority must not be rederived")

    monkeypatch.setattr(
        semantic_coverage,
        "validate_coverage_inputs",
        unexpected_revalidation,
    )

    assert _evaluate(case).status is CheckStatus.PASSED


@pytest.mark.parametrize("forgery", ("source", "evidence", "digest"))
def test_overlay_forgery_is_check_input_invalid(forgery: str) -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (_filter(),),
    )
    authenticated = case.check_input.semantic_ast
    if forgery == "digest":
        forged = replace(
            authenticated,
            coverage=authenticated.coverage.model_copy(
                update={"requirements_digest": "sha256:" + "f" * 64}
            ),
        )
    else:
        changed_annotations = tuple(
            annotation.model_copy(
                update={
                    "source_ids": ("forged-source",)
                    if forgery == "source"
                    else annotation.source_ids,
                    "evidence_ids": ("forged-evidence",)
                    if forgery == "evidence"
                    else annotation.evidence_ids,
                }
            )
            if annotation == authenticated.coverage.annotations[0]
            else annotation
            for annotation in authenticated.coverage.annotations
        )
        coverage_update = {"annotations": changed_annotations}
        if forgery == "source":
            coverage_update["required_source_ids"] = ("forged-source",)
        else:
            coverage_update["evidence_ids"] = tuple(
                sorted((*authenticated.coverage.evidence_ids, "forged-evidence"))
            )
        forged = replace(
            authenticated,
            coverage=authenticated.coverage.model_copy(update=coverage_update),
        )
    check_input = SemanticCheckInput(
        semantic_ast=forged,
        query_spec=case.query_spec,
        requirements=case.requirements,
    )

    result = evaluate_semantic_authority_checks(check_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_equivalent_sql_forms_receive_the_same_semantic_coverage() -> None:
    first = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (_filter(),),
    )
    second = build_case(
        'select "x"."status" from "orders" as "x" where "x"."status" = \'active\'',
        (_filter(),),
    )

    assert first.check_input.semantic_ast.coverage == second.check_input.semantic_ast.coverage
    assert _evaluate(first).status is CheckStatus.PASSED
    assert _evaluate(second).status is CheckStatus.PASSED


def test_physical_filter_binding_is_check_input_invalid() -> None:
    case = build_case(
        "SELECT o.status FROM orders o WHERE o.status = 'active'",
        (_filter(),),
    )
    discriminator = case.requirements.selected_bindings[0]
    assert type(discriminator) is DiscriminatorValueBinding
    physical = PhysicalColumnBinding(
        **discriminator.model_dump(
            mode="python",
            exclude={
                "kind",
                "discriminator_column",
                "discriminator_predicate",
            },
        ),
        physical_column=discriminator.discriminator_column,
    )
    forged_requirements = case.requirements.model_copy(
        update={"selected_bindings": (physical,)}
    )
    check_input = SemanticCheckInput(
        semantic_ast=case.check_input.semantic_ast,
        query_spec=case.query_spec,
        requirements=forged_requirements,
    )

    result = evaluate_semantic_authority_checks(check_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_mismatched_vertical_semantic_input_fails_closed() -> None:
    binding = _vertical_binding()
    state = _state_with_binding_joins(binding)
    requirements = _validate(state)
    conventional = build_case(
        "SELECT o.id FROM orders o",
        (
            ItemSpec(
                source_id="id",
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="id",
            ),
        ),
    )
    forged_input = SemanticCheckInput(
        semantic_ast=conventional.check_input.semantic_ast,
        query_spec=state.query_spec,
        requirements=requirements,
    )

    result = evaluate_semantic_authority_checks(forged_input, state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


@pytest.mark.parametrize("recursive_copy", (False, True))
def test_requirements_cannot_replace_the_state_binding_column(
    recursive_copy: bool,
) -> None:
    case = build_case(
        "SELECT o.id FROM orders o",
        (
            ItemSpec(
                source_id="id",
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="id",
            ),
        ),
    )
    genuine = case.requirements.selected_bindings[0]
    assert type(genuine) is PhysicalColumnBinding
    assert genuine.status is BindingStatus.SUPPORTED
    secret = (
        genuine.physical_column.model_copy(update={"column": "secret"})
        if recursive_copy
        else column("orders", "secret")
    )
    forged_binding = genuine.model_copy(
        update={
            "columns": (secret,),
            "physical_column": secret,
        }
    )
    requirements = _forged_requirements(case, bindings=(forged_binding,))
    check_input = _check_input_for_sql(
        case,
        "SELECT o.secret FROM orders o",
        requirements,
    )

    result = evaluate_semantic_authority_checks(check_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_requirements_cannot_invent_binding_evidence_id() -> None:
    case = build_case(
        "SELECT o.id FROM orders o",
        (
            ItemSpec(
                source_id="id",
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="id",
            ),
        ),
    )
    genuine = case.requirements.selected_bindings[0]
    forged_binding = genuine.model_copy(update={"evidence_ids": ("forged-evidence",)})
    requirements = _forged_requirements(case, bindings=(forged_binding,))
    check_input = _check_input_for_sql(
        case,
        "SELECT o.id FROM orders o",
        requirements,
    )

    result = evaluate_semantic_authority_checks(check_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID


def test_requirements_cannot_replace_state_join_candidate() -> None:
    allowed = inner_join("orders", "customer_id", "customers", "id")
    case = build_case(
        "SELECT o.id, c.id FROM orders o JOIN customers c ON o.customer_id = c.id",
        (
            ItemSpec(
                source_id="order-id",
                kind=SemanticItemKind.DIMENSION,
                table="orders",
                column="id",
            ),
            ItemSpec(
                source_id="customer-id",
                kind=SemanticItemKind.DIMENSION,
                table="customers",
                column="id",
                join_path=(allowed,),
            ),
        ),
    )
    genuine_join = case.requirements.eligible_validated_joins[0]
    forged_join = genuine_join.model_copy(update={"join_id": "forged-join"})
    requirements = _forged_requirements(case, joins=(forged_join,))
    check_input = _check_input_for_sql(
        case,
        "SELECT o.id, c.id FROM orders o JOIN customers c ON o.customer_id = c.id",
        requirements,
    )

    result = evaluate_semantic_authority_checks(check_input, case.state, POSTGRES_DSN)

    assert result.status is CheckStatus.INCONCLUSIVE
    assert result.failure_code is CheckFailureCode.CHECK_INPUT_INVALID
