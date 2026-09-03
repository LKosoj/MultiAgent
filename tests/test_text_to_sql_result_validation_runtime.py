"""RED runtime handoff contract for R4c-2 result validation."""

from __future__ import annotations

import builtins
import json
from dataclasses import replace

import pytest
from custom_tools.text_to_sql.adaptive._policy_config import (
    load_adaptive_policy_config,
)
from custom_tools.text_to_sql.adaptive.models import CheckKind
from custom_tools.text_to_sql.adaptive.result_review import (
    evaluate_result_review_capability,
)
from custom_tools.text_to_sql.adaptive.result_review_runtime import (
    INVALID_RESULT_REVIEW_RUNTIME,
    build_result_review_runtime,
)
from custom_tools.text_to_sql.adaptive.result_validation import (
    RESULT_VALIDATION_RUNTIME_KEY,
    evaluate_result_validation_capability,
)
from custom_tools.text_to_sql.adaptive.result_validation_runtime import (
    INVALID_RESULT_VALIDATION_RUNTIME,
    build_result_validation_runtime,
)
from test_text_to_sql_pre_execution_gate import VALID_SQL, _on_runtime
from test_text_to_sql_solver_runner import _check
from workflow.adaptive_solver_checkpoint import AdaptiveSolverCheckpointStore
from workflow.deadline import DeadlineBudget
from workflow.enhanced_engine import EnhancedWorkflowEngine


def test_result_validation_runtime_exports_closed_factory_boundary() -> None:
    assert RESULT_VALIDATION_RUNTIME_KEY == "text_to_sql_result_validation"
    assert callable(build_result_validation_runtime)
    assert callable(evaluate_result_validation_capability)


def test_invalid_result_validation_runtime_is_present_named_sentinel() -> None:
    assert INVALID_RESULT_VALIDATION_RUNTIME is not None
    assert INVALID_RESULT_VALIDATION_RUNTIME is not object()


def _successful_execution():
    return {
        "success": True,
        "data": [["paid"]],
        "columns": ["status"],
        "rows_affected": 1,
        "execution_time_ms": 1,
        "error_message": None,
        "dry_run_only": False,
        "skipped_execution": False,
        "sql_query": VALID_SQL,
        "applied_row_limit": 10,
    }


def _runtime_with_persisted_candidate(tmp_path, *, deadline=None):
    """Build the same checked candidate plus its durable proposal input."""

    from custom_tools.text_to_sql.adaptive.models import SolverState
    from custom_tools.text_to_sql.adaptive.solver_loop import apply_solver_proposal
    from custom_tools.text_to_sql.adaptive.solver_protocol import (
        SolverProposalV1,
        SqlCandidateProposal,
    )
    from custom_tools.text_to_sql.adaptive.solver_results import (
        append_solver_check_result,
    )
    from custom_tools.text_to_sql.adaptive.semantic_coverage import (
        validate_coverage_inputs,
    )
    from workflow._text_to_sql_document_authority import (
        solver_document_freshness_reference,
    )

    tmp_path.mkdir(exist_ok=True)
    runtime = _on_runtime(terminal=True, deadline=deadline)
    research = runtime.verified_research_state
    requirements = validate_coverage_inputs(
        research,
        solver_document_freshness_reference(runtime, research),
        research.run_id,
        research.run_incarnation,
    )
    initial = SolverState(
        run_id=research.run_id,
        run_incarnation=research.run_incarnation,
        revision=research.revision,
        schema_namespace_version=research.schema_namespace_version,
        query_spec=research.query_spec,
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    proposal = SolverProposalV1(
        proposal_version=1,
        proposal=SqlCandidateProposal(proposal_kind="sql_candidate", sql=VALID_SQL),
    )
    transition = apply_solver_proposal(
        initial,
        proposal,
        base_revision=initial.revision,
        dsn=runtime.dsn,
        table_namespace="main",
        requirements=requirements,
        id_factory=iter(("candidate-1", "plan-1")).__next__,
    )
    store = AdaptiveSolverCheckpointStore(tmp_path / "solver.sqlite")
    store.initialize(initial)
    checkpoint = store.commit_non_execution(
        initial,
        transition.state,
        action_revision=0,
        action=transition.action.model_dump(mode="json"),
        replay_input=transition.replay_input,
    )
    state = checkpoint.state
    for kind in (
        CheckKind.SAFETY,
        CheckKind.SCHEMA,
        CheckKind.SEMANTIC,
        CheckKind.EXPLAIN,
    ):
        checked = append_solver_check_result(
            state,
            _check(state.sql_candidates[-1].candidate_id, kind),
            base_revision=state.revision,
        )
        checkpoint = store.commit_non_execution(
            state,
            checked.state,
            action_revision=checkpoint.cursor.next_action_revision,
            action={
                "kind": "solver_check",
                "check": checked.check_result.model_dump(mode="json"),
            },
        )
        state = checkpoint.state
    runtime.verified_solver_state = state
    runtime.verified_solver_candidate_id = state.sql_candidates[-1].candidate_id
    runtime.solver_checkpoint_store = store
    return runtime, transition.replay_input.requirements


def test_exact_runtime_builds_capability_accepted_for_canonical_execution(tmp_path) -> None:
    runtime, _ = _runtime_with_persisted_candidate(tmp_path)

    capability = build_result_validation_runtime(runtime, sql_query=VALID_SQL)
    receipt = evaluate_result_validation_capability(
        capability,
        expected_run_id=runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    )

    assert capability is not INVALID_RESULT_VALIDATION_RUNTIME
    assert receipt is None


def test_post_execution_capabilities_keep_selected_proposal_requirements(tmp_path) -> None:
    runtime, persisted = _runtime_with_persisted_candidate(tmp_path)
    runtime.verified_research_policy = load_adaptive_policy_config()

    validation = build_result_validation_runtime(runtime, sql_query=VALID_SQL)
    review = build_result_review_runtime(runtime, sql_query=VALID_SQL)

    assert validation is not INVALID_RESULT_VALIDATION_RUNTIME
    assert review is not INVALID_RESULT_REVIEW_RUNTIME
    assert validation.requirements == persisted
    assert review.requirements == persisted


@pytest.mark.parametrize(
    "runtime_builder,invalid",
    (
        (build_result_validation_runtime, INVALID_RESULT_VALIDATION_RUNTIME),
        (build_result_review_runtime, INVALID_RESULT_REVIEW_RUNTIME),
    ),
)
def test_post_execution_capabilities_do_not_revalidate_live_authority(
    monkeypatch,
    tmp_path,
    runtime_builder,
    invalid,
) -> None:
    import workflow._text_to_sql_document_authority as authority

    runtime, _ = _runtime_with_persisted_candidate(tmp_path)
    runtime.verified_research_policy = load_adaptive_policy_config()

    def live_revalidation_must_not_run(*_args, **_kwargs):
        raise AssertionError("final capability must use persisted freshness")

    monkeypatch.setattr(
        authority,
        "live_solver_document_freshness_context",
        live_revalidation_must_not_run,
    )

    assert runtime_builder(runtime, sql_query=VALID_SQL) is not invalid


@pytest.mark.parametrize(
    "runtime_builder,invalid",
    (
        (build_result_validation_runtime, INVALID_RESULT_VALIDATION_RUNTIME),
        (build_result_review_runtime, INVALID_RESULT_REVIEW_RUNTIME),
    ),
)
def test_post_execution_capabilities_reject_missing_or_multiple_replay_inputs(
    monkeypatch,
    tmp_path,
    runtime_builder,
    invalid,
) -> None:
    missing = _on_runtime(terminal=True)
    missing.verified_research_policy = load_adaptive_policy_config()
    assert runtime_builder(missing, sql_query=VALID_SQL) is invalid

    runtime, _ = _runtime_with_persisted_candidate(tmp_path)
    runtime.verified_research_policy = load_adaptive_policy_config()
    store = runtime.solver_checkpoint_store
    original = store.load_replay_chain

    def multiple(*args):
        chain = original(*args)
        return replace(chain, actions=(*chain.actions, chain.actions[0]))

    monkeypatch.setattr(store, "load_replay_chain", multiple)
    assert runtime_builder(runtime, sql_query=VALID_SQL) is invalid


@pytest.mark.parametrize(
    "runtime_builder,invalid",
    (
        (build_result_validation_runtime, INVALID_RESULT_VALIDATION_RUNTIME),
        (build_result_review_runtime, INVALID_RESULT_REVIEW_RUNTIME),
    ),
)
def test_post_execution_capabilities_reject_forged_solver_state(
    tmp_path,
    runtime_builder,
    invalid,
) -> None:
    runtime, _ = _runtime_with_persisted_candidate(tmp_path)
    runtime.verified_research_policy = load_adaptive_policy_config()
    runtime.verified_solver_state = runtime.verified_solver_state.model_copy(
        update={"revision": runtime.verified_solver_state.revision + 1}
    )

    assert runtime_builder(runtime, sql_query=VALID_SQL) is invalid


@pytest.mark.parametrize(
    "runtime_builder,invalid",
    (
        (build_result_validation_runtime, INVALID_RESULT_VALIDATION_RUNTIME),
        (build_result_review_runtime, INVALID_RESULT_REVIEW_RUNTIME),
    ),
)
def test_post_execution_capabilities_reject_forged_persisted_requirements(
    monkeypatch,
    tmp_path,
    runtime_builder,
    invalid,
) -> None:
    import custom_tools.text_to_sql.adaptive.result_review_runtime as review_runtime
    import custom_tools.text_to_sql.adaptive.result_validation_runtime as validation_runtime
    from custom_tools.text_to_sql.adaptive.semantic_coverage import CoverageRequirements
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest

    runtime, persisted = _runtime_with_persisted_candidate(tmp_path)
    runtime.verified_research_policy = load_adaptive_policy_config()
    payload = persisted.model_dump(mode="python", round_trip=True)
    payload["freshness_digest"] = "sha256:" + "0" * 64
    payload["requirements_digest"] = canonical_digest(
        {key: value for key, value in payload.items() if key != "requirements_digest"}
    )
    forged = CoverageRequirements.model_validate(payload)

    monkeypatch.setattr(
        validation_runtime,
        "_persisted_sql_proposal_requirements",
        lambda *_args: forged,
    )
    monkeypatch.setattr(
        review_runtime,
        "_persisted_sql_proposal_requirements",
        lambda *_args: forged,
    )

    assert runtime_builder(runtime, sql_query=VALID_SQL) is invalid


@pytest.mark.parametrize(
    "runtime_builder,invalid",
    (
        (build_result_validation_runtime, INVALID_RESULT_VALIDATION_RUNTIME),
        (build_result_review_runtime, INVALID_RESULT_REVIEW_RUNTIME),
    ),
)
def test_post_execution_capabilities_reject_forged_persisted_freshness_context(
    monkeypatch,
    tmp_path,
    runtime_builder,
    invalid,
) -> None:
    from datetime import timedelta

    import workflow._text_to_sql_document_authority as authority

    runtime, _ = _runtime_with_persisted_candidate(tmp_path)
    runtime.verified_research_policy = load_adaptive_policy_config()
    persisted = authority.solver_document_freshness_reference(
        runtime,
        runtime.verified_research_state,
    )
    forged = persisted.model_copy(
        update={"evaluated_at": persisted.evaluated_at + timedelta(seconds=1)}
    )
    monkeypatch.setattr(
        authority,
        "solver_document_freshness_reference",
        lambda *_args: forged,
    )

    assert runtime_builder(runtime, sql_query=VALID_SQL) is invalid


def test_runtime_builder_rejects_sql_or_verified_candidate_mismatch(tmp_path) -> None:
    sql_runtime, _ = _runtime_with_persisted_candidate(tmp_path / "sql")
    candidate_runtime, _ = _runtime_with_persisted_candidate(tmp_path / "candidate")
    candidate_runtime.verified_solver_candidate_id = "foreign-candidate"

    assert (
        build_result_validation_runtime(sql_runtime, sql_query="SELECT 1")
        is INVALID_RESULT_VALIDATION_RUNTIME
    )
    assert (
        build_result_validation_runtime(candidate_runtime, sql_query=VALID_SQL)
        is INVALID_RESULT_VALIDATION_RUNTIME
    )


def test_runtime_builder_does_not_depend_on_solver_runtime_import(monkeypatch, tmp_path) -> None:
    runtime, _ = _runtime_with_persisted_candidate(tmp_path)
    real_import = builtins.__import__

    def deny_solver_runtime(name, *args, **kwargs):
        if name == "workflow.text_to_sql_adaptive_solver":
            raise AssertionError("result validation must not import solver runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_solver_runtime)
    capability = build_result_validation_runtime(runtime, sql_query=VALID_SQL)

    assert capability is not INVALID_RESULT_VALIDATION_RUNTIME
    assert evaluate_result_validation_capability(
        capability,
        expected_run_id=runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    ) is None


def test_result_review_runtime_forwards_remaining_deadline_and_skips_expired_call(
    monkeypatch,
    tmp_path,
) -> None:
    import agent_command
    import utils

    clock = {"now": 100.0}
    deadline = DeadlineBudget.from_duration(
        5,
        monotonic=lambda: clock["now"],
        wall_time=lambda: clock["now"],
    )
    runtime, _ = _runtime_with_persisted_candidate(tmp_path / "active", deadline=deadline)
    runtime.verified_research_policy = load_adaptive_policy_config()
    runtime.loaded_schema = replace(
        runtime.loaded_schema,
        schema={
            "main.orders": {
                "description": "Event-specific order records.",
                "columns": {
                    "status": {"type": "TEXT", "description": "Order status."},
                    "account_id": {
                        "type": "INTEGER",
                        "constraint_type": "FK",
                        "references": "accounts.id",
                    },
                },
            },
            "main.accounts": {
                "description": "Permanent account attributes.",
                "columns": {
                    "id": {"type": "INTEGER", "constraint_type": "PK"},
                    "status": {
                        "type": "TEXT",
                        "description": "Permanent account status.",
                    },
                },
            },
            **{
                f"main.unrelated_{index}": {
                    "description": "Unrelated table.",
                    "columns": {"id": {"type": "INTEGER"}},
                }
                for index in range(12)
            },
        },
    )
    provider_calls: list[dict[str, object]] = []
    provider_names: list[str] = []
    review_calls: list[object] = []

    monkeypatch.setattr(
        agent_command,
        "create_text_to_sql_model",
        lambda name, **kwargs: (
            provider_names.append(name), provider_calls.append(kwargs), object()
        )[-1],
    )
    monkeypatch.setattr(
        utils,
        "call_openai_api",
        lambda **_kwargs: review_calls.append(_kwargs) or '{"status":"consistent","reason":"matches"}',
    )

    capability = build_result_review_runtime(runtime, sql_query=VALID_SQL)
    assert capability is not INVALID_RESULT_REVIEW_RUNTIME
    assert provider_calls == []
    clock["now"] = 102.0
    receipt = evaluate_result_review_capability(
        capability,
        expected_run_id=runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    )

    assert receipt.verdict == "consistent"
    assert provider_names == ["model_hard"]
    assert provider_calls == [
        {
            "max_tokens": runtime.verified_research_policy.model_budget.output_tokens_per_call,
            "temperature": 0.3,
            "timeout_seconds": 3.0,
            "client_max_retries": 0,
        }
    ]
    assert len(review_calls) == 1
    review_prompt = json.loads(review_calls[0]["prompt"])
    assert set(review_prompt["schema"]) == {"main.orders", "main.accounts"}
    response_format = review_calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "ModelReviewResponse"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["required"] == [
        "status",
        "reason",
        "source_id",
        "repair_kind",
        "repair_binding_id",
    ]
    assert response_format["json_schema"]["schema"]["properties"]["source_id"] == {
        "anyOf": [
            {"enum": ["status"], "type": "string"},
            {"type": "null"},
        ],
        "default": None,
        "title": "Source Id",
    }

    expired_runtime, _ = _runtime_with_persisted_candidate(
        tmp_path / "expired",
        deadline=DeadlineBudget.from_duration(
            5,
            monotonic=lambda: clock["now"],
            wall_time=lambda: clock["now"],
        ),
    )
    expired_runtime.verified_research_policy = load_adaptive_policy_config()
    expired_capability = build_result_review_runtime(
        expired_runtime, sql_query=VALID_SQL
    )
    assert expired_capability is not INVALID_RESULT_REVIEW_RUNTIME
    clock["now"] = 108.0
    expired_receipt = evaluate_result_review_capability(
        expired_capability,
        expected_run_id=expired_runtime.run_id,
        expected_sql=VALID_SQL,
        execution=_successful_execution(),
    )
    assert expired_receipt.verdict == "timeout"
    assert expired_receipt.source_id is None
    assert expired_receipt.evidence_id is None
    assert len(provider_calls) == 1
    assert len(review_calls) == 1


def test_enhanced_engine_installs_result_validation_metadata_fail_closed(tmp_path) -> None:
    runtime, _ = _runtime_with_persisted_candidate(tmp_path)
    metadata: dict[str, object] = {}

    EnhancedWorkflowEngine._install_result_validation_runtime_metadata(
        metadata,
        runtime,
        sql_query=VALID_SQL,
        executor_dsn=runtime.dsn,
    )
    assert RESULT_VALIDATION_RUNTIME_KEY in metadata
    assert metadata[RESULT_VALIDATION_RUNTIME_KEY] is not INVALID_RESULT_VALIDATION_RUNTIME

    mismatch_metadata: dict[str, object] = {}
    EnhancedWorkflowEngine._install_result_validation_runtime_metadata(
        mismatch_metadata,
        runtime,
        sql_query="SELECT 1",
        executor_dsn=runtime.dsn,
    )
    assert mismatch_metadata[RESULT_VALIDATION_RUNTIME_KEY] is INVALID_RESULT_VALIDATION_RUNTIME

    direct_metadata: dict[str, object] = {}
    EnhancedWorkflowEngine._install_result_validation_runtime_metadata(
        direct_metadata,
        object(),
        sql_query=VALID_SQL,
        executor_dsn="sqlite:///unused.db",
    )
    assert RESULT_VALIDATION_RUNTIME_KEY not in direct_metadata
