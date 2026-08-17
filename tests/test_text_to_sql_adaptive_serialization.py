"""Проверки закрытой канонической сериализации adaptive-контрактов."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    CheckFailureCode,
    CheckKind,
    CheckRepair,
    CheckResult,
    CheckStatus,
    ColumnRef,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    MissingEvidenceRequest,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    QuerySpec,
    ResearchState,
    ResearchAction,
    ResearchActionKind,
    ResearchReentryRecord,
    ResearchReentryStatus,
    SolverAction,
    SolverActionKind,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    SolverState,
    SolverStopReason,
    SqlCandidate,
    TableRef,
    RepairKind,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    ArtifactReference,
    ArtifactReferenceError,
    CanonicalJsonError,
    ContractDecodeError,
    ContractMigrationError,
    ContractValidationError,
    ContractVersionError,
    FutureContractVersionError,
    InlineRowsLimitError,
    SerializationLimits,
    StateSizeLimitError,
    UnsupportedContractVersionError,
    canonical_digest,
    canonical_json_bytes,
    deserialize_as,
    deserialize_contract,
    migrate_contract_mapping,
    serialize_contract,
    verify_artifact_reference,
)


RUN_ID = "run-1"
INCARNATION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
SCHEMA = "schema:0123456789abcdef"
DIGEST = "sha256:0123456789abcdef"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _table() -> TableRef:
    return TableRef(namespace="main", schema=None, table="orders")


def _column(name: str = "status") -> ColumnRef:
    return ColumnRef(table=_table(), column=name)


def _predicate() -> PredicateRef:
    return PredicateRef(left=_column(), operator=PredicateOperator.EQ, right="paid")


def _query_spec(*, resolved: bool = False) -> QuerySpec:
    return QuerySpec(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=None,
        query_id="query-1",
        original_text="sales",
        semantic_items=(
            SemanticItem(
                source_id="source-1",
                kind=SemanticItemKind.FILTER,
                source_text="sales",
                normalized_meaning="sales",
                required=True,
                operator=PredicateOperator.EQ,
                literal_or_reference="paid",
                status=SemanticItemStatus.RESOLVED
                if resolved
                else SemanticItemStatus.UNRESOLVED,
                binding_ids=("binding-1",) if resolved else (),
            ),
        ),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )


def _evidence() -> EvidenceRecord:
    return EvidenceRecord(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        evidence_id="evidence-1",
        source_kind=EvidenceSourceKind.SCHEMA,
        target=_table(),
        action_digest=DIGEST,
        observation="orders table exists",
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=NOW,
        strength=1.0,
        created_at=NOW,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=0,
        ),
    )


def _binding() -> PhysicalColumnBinding:
    return PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(_table(),),
        columns=(_column(),),
        predicates=(_predicate(),),
        join_path=(),
        evidence_ids=("evidence-1",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="schema-check",
        physical_column=_column(),
    )


def _budget() -> dict[str, int]:
    return {
        "initial_wall_clock_ms": 10,
        "used_wall_clock_ms": 0,
        "remaining_wall_clock_ms": 10,
        "initial_model_calls": 1,
        "used_model_calls": 0,
        "remaining_model_calls": 1,
        "initial_model_tokens": 1,
        "used_model_tokens": 0,
        "remaining_model_tokens": 1,
        "initial_db_probe_ms": 1,
        "used_db_probe_ms": 0,
        "remaining_db_probe_ms": 1,
        "initial_rows": 1,
        "used_rows": 0,
        "remaining_rows": 1,
        "initial_bytes": 1,
        "used_bytes": 0,
        "remaining_bytes": 1,
    }


def _missing_request() -> MissingEvidenceRequest:
    return MissingEvidenceRequest(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        missing_evidence_request_id="request-1",
        source_id="source-1",
        question="Which table stores status?",
        candidate_targets=(_table(),),
        required_evidence_kind=EvidenceSourceKind.SCHEMA,
        reason="missing binding",
    )


def _contracts() -> tuple[object, ...]:
    query_spec = _query_spec()
    return (
        query_spec,
        _evidence(),
        # Fixture reconciliation: revision 1 requires its completed research action.
        ResearchState(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=1,
            schema_namespace_version=SCHEMA,
            query_spec=_query_spec(resolved=True),
            hypotheses=(),
            evidence=(_evidence(),),
            bindings=(_binding(),),
            join_candidates=(),
            unresolved_items=(),
            action_history=(
                ResearchAction(
                    action_id="research-action-1",
                    kind=ResearchActionKind.INSPECT_TABLE,
                    hypothesis_id=None,
                    target=_table(),
                    parameters=(),
                    action_digest=DIGEST,
                    expected_revision=0,
                ),
            ),
            result_expectations=(),
            budget_state=_budget(),
            stop_reason=None,
        ),
        _missing_request(),
        SolverState(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=1,
            schema_namespace_version=SCHEMA,
            query_spec=query_spec.model_copy(
                update={"schema_namespace_version": SCHEMA}
            ),
            sql_candidates=(),
            check_results=(),
            execution_results=(),
            missing_evidence_requests=(),
            action_history=(),
            selected_candidate_id=None,
            stop_reason=None,
        ),
    )


def test_solver_state_typed_check_round_trips_and_legacy_repair_remains_untyped() -> (
    None
):
    candidate = SqlCandidate(
        candidate_id="candidate-1",
        sql="SELECT status FROM orders",
        normalized_ast_digest=DIGEST,
        revision=1,
    )
    typed_check = CheckResult(
        check_id="check-1",
        candidate_id=candidate.candidate_id,
        check_kind=CheckKind.SAFETY,
        status=CheckStatus.FAILED,
        failure_code=CheckFailureCode.SAFETY_REJECTED,
        affected_source_ids=(),
        affected_ast_node_ids=(),
        observed_error=None,
        repair=CheckRepair(kind=RepairKind.REVISE_SQL),
    )
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(candidate,),
        check_results=(typed_check,),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(
            SolverAction(
                action_id="action-1",
                kind=SolverActionKind.SQL_CANDIDATE,
                base_revision=0,
                candidate_id=candidate.candidate_id,
                missing_evidence_request_id=None,
            ),
        ),
        selected_candidate_id=None,
        stop_reason=None,
    )

    payload = serialize_contract(state)
    assert deserialize_contract(payload) == state

    legacy = json.loads(payload)
    legacy_check = legacy["check_results"][0]
    legacy_check["repair"] = None
    legacy_check["required_change"] = "revise SQL"
    decoded = deserialize_contract(canonical_json_bytes(legacy))

    assert isinstance(decoded, SolverState)
    assert decoded.check_results[0].repair is None
    assert decoded.check_results[0].required_change == "revise SQL"


def test_empty_legacy_solver_state_adds_action_history() -> None:
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    legacy = json.loads(serialize_contract(state))
    legacy.pop("action_history")

    assert deserialize_contract(canonical_json_bytes(legacy)) == state


def test_v1_solver_state_missing_research_reentries_adds_only_empty_list() -> None:
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    legacy = json.loads(serialize_contract(state))
    legacy.pop("research_reentries")

    migrated = migrate_contract_mapping(legacy)

    assert migrated["research_reentries"] == []
    assert deserialize_contract(canonical_json_bytes(legacy)) == state


def test_solver_state_research_reentry_round_trips() -> None:
    request = _missing_request()
    record = ResearchReentryRecord(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=2,
        schema_namespace_version=SCHEMA,
        research_reentry_id="reentry-1",
        missing_evidence_request_id=request.missing_evidence_request_id,
        source_id=request.source_id,
        ordinal=1,
        research_base_revision=1,
        baseline_evidence_ids=("evidence-1",),
        status=ResearchReentryStatus.TOOL_FAILURE,
        research_result_revision=None,
        evidence_ids=(),
    )
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=3,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(request,),
        research_reentries=(record,),
        action_history=(
            SolverAction(
                action_id="action-1",
                kind=SolverActionKind.MISSING_EVIDENCE,
                base_revision=0,
                candidate_id=None,
                missing_evidence_request_id=request.missing_evidence_request_id,
            ),
        ),
        selected_candidate_id=None,
        stop_reason=SolverStopReason.MISSING_EVIDENCE,
    )

    assert deserialize_contract(serialize_contract(state)) == state


class _EqualEmpty:
    def __eq__(self, other: object) -> bool:
        return other == []


class _EqualEmptyMapping(dict):
    def __eq__(self, other: object) -> bool:
        return other == []


class _EmptyListSubclass(list):
    pass


@pytest.mark.parametrize("version", (0, 1))
@pytest.mark.parametrize(
    "weird_empty",
    (_EqualEmpty(), _EqualEmptyMapping(), _EmptyListSubclass()),
    ids=("equal-object", "mapping", "list-subclass"),
)
def test_direct_legacy_migration_requires_literal_empty_lists(
    version: int,
    weird_empty: object,
) -> None:
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    legacy = json.loads(serialize_contract(state))
    legacy["contract_version"] = version
    legacy.pop("action_history")
    legacy["sql_candidates"] = weird_empty

    with pytest.raises(ContractMigrationError, match="action_history"):
        migrate_contract_mapping(legacy)


@pytest.mark.parametrize("version", (0, 1))
def test_direct_normal_empty_legacy_mapping_still_migrates(version: int) -> None:
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    legacy = json.loads(serialize_contract(state))
    legacy["contract_version"] = version
    legacy.pop("action_history")

    assert migrate_contract_mapping(legacy)["action_history"] == []


def test_nonempty_legacy_solver_state_without_action_history_fails_closed() -> None:
    candidate = SqlCandidate(
        candidate_id="candidate-1",
        sql="SELECT status FROM orders",
        normalized_ast_digest=DIGEST,
        revision=1,
    )
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(candidate,),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(
            SolverAction(
                action_id="action-1",
                kind=SolverActionKind.SQL_CANDIDATE,
                base_revision=0,
                candidate_id=candidate.candidate_id,
                missing_evidence_request_id=None,
            ),
        ),
        selected_candidate_id=None,
        stop_reason=None,
    )
    legacy = json.loads(serialize_contract(state))
    legacy.pop("action_history")

    with pytest.raises(ContractMigrationError, match="action_history"):
        deserialize_contract(canonical_json_bytes(legacy))


def test_query_plan_persistence_is_rejected_without_migration() -> None:
    state = SolverState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=_query_spec().model_copy(
            update={"schema_namespace_version": SCHEMA}
        ),
        sql_candidates=(),
        check_results=(),
        execution_results=(),
        missing_evidence_requests=(),
        action_history=(),
        selected_candidate_id=None,
        stop_reason=None,
    )
    legacy_state = json.loads(serialize_contract(state))
    legacy_state["query_plan_candidates"] = []

    with pytest.raises(ContractValidationError):
        deserialize_contract(canonical_json_bytes(legacy_state))
    with pytest.raises(ContractDecodeError, match="unknown"):
        deserialize_contract(
            b'{"contract_name":"query_plan","contract_version":1}'
        )


def test_canonical_json_is_stable_utf8_sorted_and_sha256() -> None:
    first = {"b": 1, "é": "значение", "a": [True, None]}
    second = {"a": [True, None], "é": "значение", "b": 1}
    expected = '{"a":[true,null],"b":1,"é":"значение"}'.encode("utf-8")

    assert canonical_json_bytes(first) == expected == canonical_json_bytes(second)
    assert canonical_digest(first) == f"sha256:{hashlib.sha256(expected).hexdigest()}"
    with pytest.raises(CanonicalJsonError):
        canonical_json_bytes({"nan": float("nan")})


@pytest.mark.parametrize("contract", _contracts())
def test_each_w1_top_level_contract_round_trips(contract: object) -> None:
    assert deserialize_contract(serialize_contract(contract)) == contract  # type: ignore[arg-type]


def test_datetime_is_normalized_to_utc_z_and_validated_in_json_context() -> None:
    payload = serialize_contract(_evidence())
    assert b"2026-07-30T12:00:00.000000Z" in payload
    decoded = deserialize_contract(payload)
    assert isinstance(decoded, EvidenceRecord)

    mapping = json.loads(payload)
    mapping["observed_at"] = "2026-07-30T12:00:00+00:00"
    with pytest.raises(ContractValidationError, match="timestamp"):
        deserialize_contract(json.dumps(mapping))


def test_deserialize_as_uses_strict_model_validation() -> None:
    payload = canonical_json_bytes(
        {"namespace": "main", "schema": None, "table": "orders"}
    )
    assert deserialize_as(payload, TableRef) == _table()

    with pytest.raises(ContractValidationError):
        deserialize_as(
            b'{"namespace":"main","schema":null,"table":"orders","extra":1}', TableRef
        )
    with pytest.raises(ContractValidationError):
        deserialize_contract(
            canonical_json_bytes(
                {**json.loads(serialize_contract(_query_spec())), "revision": "0"}
            )
        )
    with pytest.raises(TypeError, match="supported immutable W1"):
        deserialize_as(b"{}", SemanticItem)


def test_decode_rejects_duplicate_keys_non_object_and_unknown_contract() -> None:
    with pytest.raises(ContractDecodeError, match="valid JSON"):
        deserialize_contract(
            b'{"contract_name":"query_spec","contract_name":"query_spec"}'
        )
    with pytest.raises(ContractDecodeError, match="root"):
        deserialize_contract(b"[]")
    with pytest.raises(ContractDecodeError, match="unknown"):
        deserialize_contract(b'{"contract_name":"unknown","contract_version":1}')


def test_public_decode_errors_do_not_echo_payload_content() -> None:
    secret = "top-secret-value"
    payloads = (
        b'{"secret":"top-secret-value","secret":"again"}',
        b'{"secret":"top-secret-value",}',
        canonical_json_bytes(
            {
                **json.loads(serialize_contract(_query_spec())),
                "unexpected": secret,
            }
        ),
    )

    for payload in payloads:
        with pytest.raises(ContractDecodeError) as exc_info:
            deserialize_contract(payload)
        assert secret not in str(exc_info.value)
        assert "unexpected" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None


def test_only_exact_v0_predecessor_migrates_without_mutating_input() -> None:
    original = json.loads(serialize_contract(_query_spec()))
    original["contract_version"] = 0
    migrated = migrate_contract_mapping(original)

    assert original["contract_version"] == 0
    assert migrated["contract_version"] == 1
    assert deserialize_contract(canonical_json_bytes(original)) == _query_spec()

    with pytest.raises(ContractMigrationError):
        migrate_contract_mapping({**original, "guessed_field": True})
    with pytest.raises(ContractVersionError):
        migrate_contract_mapping(
            {key: value for key, value in original.items() if key != "contract_version"}
        )
    with pytest.raises(UnsupportedContractVersionError):
        migrate_contract_mapping({**original, "contract_version": -1})
    with pytest.raises(FutureContractVersionError):
        migrate_contract_mapping({**original, "contract_version": 2})


def test_state_and_inline_rows_limits_apply_to_encode_and_decode() -> None:
    with pytest.raises(StateSizeLimitError):
        serialize_contract(
            _query_spec(),
            limits=SerializationLimits(max_state_bytes=1, max_inline_rows=5),
        )
    with pytest.raises(InlineRowsLimitError):
        canonical_json_bytes(
            {"rows": [{"id": 1}, {"id": 2}]}, limits=SerializationLimits(100, 1)
        )
    payload_with_rows = {
        **json.loads(serialize_contract(_query_spec())),
        "rows": [1, 2],
    }
    with pytest.raises(InlineRowsLimitError):
        deserialize_contract(
            json.dumps(payload_with_rows), limits=SerializationLimits(1_000, 1)
        )


def test_nesting_depth_is_closed_and_never_leaks_recursion_error() -> None:
    for invalid in (0, 65, -1, True, "64"):
        with pytest.raises(ValueError, match="max_nesting_depth"):
            SerializationLimits(max_nesting_depth=invalid)  # type: ignore[arg-type]

    nested = "[" * 500 + "0" + "]" * 500
    payload = '{"contract_name":"unknown","contract_version":1,"nested":' + nested + "}"
    with pytest.raises(ContractDecodeError, match="max_nesting_depth") as exc_info:
        deserialize_contract(payload)
    assert not isinstance(exc_info.value, RecursionError)

    value: object = 0
    for _ in range(65):
        value = [value]
    with pytest.raises(CanonicalJsonError, match="max_nesting_depth"):
        canonical_json_bytes(value)


def test_spoofed_transient_model_never_imports_decision_module() -> None:
    script = """
import sys
from pydantic import BaseModel
from custom_tools.text_to_sql.adaptive.serialization import deserialize_as

module_name = "custom_tools.text_to_sql.adaptive.research_decision"
sys.modules.pop(module_name, None)

class Spoof(BaseModel):
    pass

Spoof.__module__ = module_name
Spoof.__name__ = "ResearchDecisionV1"
try:
    deserialize_as(b"{}", Spoof)
except TypeError:
    pass
else:
    raise AssertionError("spoof model was accepted")
assert module_name not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_model_budget_decode_registration_is_lazy_and_identity_based() -> None:
    script = """
import sys
import types
from pydantic import BaseModel
from custom_tools.text_to_sql.adaptive.serialization import deserialize_as

module_name = "custom_tools.text_to_sql.adaptive.model_budget"
assert module_name not in sys.modules

class Spoof(BaseModel):
    pass

Spoof.__module__ = module_name
Spoof.__name__ = "ModelTokenUsage"
try:
    deserialize_as(b"{}", Spoof)
except TypeError:
    pass
else:
    raise AssertionError("pre-import spoof model was accepted")
assert module_name not in sys.modules

from custom_tools.text_to_sql.adaptive import model_budget

usage = deserialize_as(
    b'{"input_tokens":1,"output_tokens":2}',
    model_budget.ModelTokenUsage,
)
assert usage == model_budget.ModelTokenUsage(input_tokens=1, output_tokens=2)

sys.modules[module_name] = types.SimpleNamespace(ModelTokenUsage=Spoof)
assert deserialize_as(
    b'{"input_tokens":3,"output_tokens":4}',
    model_budget.ModelTokenUsage,
) == model_budget.ModelTokenUsage(input_tokens=3, output_tokens=4)
try:
    deserialize_as(b"{}", Spoof)
except TypeError:
    pass
else:
    raise AssertionError("post-import spoof model was accepted")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_decode_rechecks_limits_after_migration_and_model_normalization() -> None:
    original = json.loads(serialize_contract(_query_spec()))
    original["contract_version"] = 0
    encoded = canonical_json_bytes(original)
    with pytest.raises(StateSizeLimitError):
        migrate_contract_mapping(
            original,
            limits=SerializationLimits(
                max_state_bytes=len(encoded) - 1, max_inline_rows=5
            ),
        )

    alias_payload = b'{"namespace":"main","schema_name":null,"table":"orders"}'
    normalized = canonical_json_bytes(_table())
    assert len(alias_payload) > len(normalized)
    assert (
        deserialize_as(
            alias_payload,
            TableRef,
            limits=SerializationLimits(
                max_state_bytes=len(alias_payload), max_inline_rows=0
            ),
        )
        == _table()
    )


def test_artifact_reference_is_immutable_and_verified_by_read_only_callback() -> None:
    content = b"artifact bytes"
    reference = ArtifactReference(
        artifact_id="artifact-1",
        digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        byte_count=len(content),
    )
    seen: list[ArtifactReference] = []

    def read_bytes(value: ArtifactReference) -> bytes:
        seen.append(value)
        return content

    assert verify_artifact_reference(reference, read_bytes) == content
    assert seen == [reference]
    with pytest.raises(ValidationError):
        reference.byte_count = 0  # type: ignore[misc]
    with pytest.raises(ArtifactReferenceError, match="byte_count"):
        verify_artifact_reference(
            reference.model_copy(update={"byte_count": 1}), read_bytes
        )
    with pytest.raises(ArtifactReferenceError, match="digest does not match"):
        verify_artifact_reference(
            reference.model_copy(update={"digest": "sha256:" + "0" * 64}), read_bytes
        )


@pytest.mark.parametrize(
    "digest",
    (
        "md5:" + "0" * 32,
        "sha256:" + "A" * 64,
        "sha256:00",
    ),
)
def test_artifact_digest_must_be_exact_sha256_before_reader_is_called(
    digest: str,
) -> None:
    calls = 0
    reference = ArtifactReference(
        artifact_id="artifact-1",
        digest="sha256:" + "0" * 64,
        byte_count=0,
    ).model_copy(update={"digest": digest})

    def read_bytes(_value: ArtifactReference) -> bytes:
        nonlocal calls
        calls += 1
        return b""

    with pytest.raises(ArtifactReferenceError, match="exact sha256"):
        verify_artifact_reference(reference, read_bytes)
    assert calls == 0


def test_artifact_reference_rejects_invalid_digest_at_model_boundary() -> None:
    with pytest.raises(ValidationError, match="exact sha256"):
        ArtifactReference(
            artifact_id="artifact-1", digest="md5:" + "0" * 32, byte_count=0
        )


def test_artifact_reader_error_is_neutral_and_unchained() -> None:
    reference = ArtifactReference(
        artifact_id="artifact-1",
        digest="sha256:" + "0" * 64,
        byte_count=0,
    )

    def read_bytes(_value: ArtifactReference) -> bytes:
        raise RuntimeError("reader-secret-marker")

    with pytest.raises(
        ArtifactReferenceError, match="artifact reader failed"
    ) as exc_info:
        verify_artifact_reference(reference, read_bytes)
    assert "reader-secret-marker" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_query_spec_roundtrip_has_no_source_span_field() -> None:
    spec = QuerySpec(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=0,
        schema_namespace_version=None,
        query_id="query-unanchored",
        original_text="show the shortest player",
        semantic_items=(
            SemanticItem(
                source_id="source-unanchored",
                kind=SemanticItemKind.METRIC,
                source_text="minimum player height",
                normalized_meaning="minimum player height",
                required=True,
                operator=None,
                literal_or_reference=None,
                status=SemanticItemStatus.UNRESOLVED,
                binding_ids=(),
            ),
        ),
        expected_result_shape=ExpectedResultShape.ROWS,
        global_constraints=(),
    )

    payload = serialize_contract(spec)

    assert b"source_span" not in payload
    assert deserialize_contract(payload) == spec
