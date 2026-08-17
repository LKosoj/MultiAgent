"""Pure provenance freshness decisions and supported-update gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.freshness import (
    DataSnapshotStatus,
    DataSnapshotValidation,
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
    FreshnessDecision,
    FreshnessProjection,
    FreshnessReason,
    FreshnessStatus,
    FreshnessValidationError,
    evaluate_evidence_freshness,
    project_evidence_freshness,
    validate_supported_updates,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    BudgetState,
    ColumnRef,
    DocumentRef,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    Hypothesis,
    HypothesisStatus,
    PhysicalColumnBinding,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.provenance import ProbeProvenance
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)


RUN_ID = "run-1"
INCARNATION = "inc-1"
SCHEMA = "sha256:" + "a" * 64
OTHER_SCHEMA = "sha256:" + "b" * 64
ACTION_DIGEST = "sha256:" + "d" * 64
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _table() -> TableRef:
    return TableRef(namespace="main", schema=None, table="orders")


def _column() -> ColumnRef:
    return ColumnRef(table=_table(), column="status")


def _document() -> DocumentRef:
    return DocumentRef(document_id="orders-rule", namespace="main")


def _provenance(
    *,
    invocation_id: str = "evidence-1",
    kind: ResearchActionKind = ResearchActionKind.INSPECT_TABLE,
    target=None,
    source_version: str | None = None,
    valid_until: datetime | None = None,
    run_id: str = RUN_ID,
    incarnation: str = INCARNATION,
    action_digest: str = ACTION_DIGEST,
) -> ProbeProvenance:
    payload = _payload(kind, source_version, valid_until)
    return ProbeProvenance(
        provenance_version=1,
        run_id=run_id,
        run_incarnation=incarnation,
        invocation_id=invocation_id,
        action_digest=action_digest,
        probe_kind=kind,
        target=target or _table(),
        schema_namespace_version=SCHEMA,
        payload_digest=canonical_digest(payload),
        started_at=NOW,
        completed_at=NOW,
        source_version=source_version,
        valid_until=valid_until,
    )


def _payload(
    kind: ResearchActionKind,
    source_version: str | None,
    valid_until: datetime | None,
) -> dict[str, object]:
    if kind is ResearchActionKind.READ_DOCUMENT:
        return {
            "document": {
                "source_version": source_version,
                "valid_until": valid_until,
            }
        }
    return {}


def _observation(provenance: ProbeProvenance) -> str:
    payload = _payload(
        provenance.probe_kind,
        provenance.source_version,
        provenance.valid_until,
    )
    payload_bytes = canonical_json_bytes(payload)
    return canonical_json_bytes(
        {
            "artifact_reference": None,
            "byte_count": len(payload_bytes),
            "invocation_id": provenance.invocation_id,
            "observation_version": 1,
            "payload_digest": provenance.payload_digest,
            "probe_kind": provenance.probe_kind,
            "provenance": provenance,
            "payload": payload,
            "row_count": 0,
            "storage": "inline",
            "summary": "observation",
            "truncated": False,
        }
    ).decode("utf-8")


def _evidence(
    *,
    scope: EvidenceValidityScope = EvidenceValidityScope.SCHEMA_VERSION,
    source_kind: EvidenceSourceKind = EvidenceSourceKind.SCHEMA,
    provenance: ProbeProvenance | None = None,
    observation: str | None = None,
    snapshot_token: str | None = None,
    evidence_id: str = "evidence-1",
) -> EvidenceRecord:
    checked = provenance or _provenance(invocation_id=evidence_id)
    payload_bytes = canonical_json_bytes(
        _payload(
            checked.probe_kind,
            checked.source_version,
            checked.valid_until,
        )
    )
    return EvidenceRecord(
        run_id=checked.run_id,
        run_incarnation=checked.run_incarnation,
        revision=1,
        schema_namespace_version=checked.schema_namespace_version,
        evidence_id=evidence_id,
        source_kind=source_kind,
        target=checked.target,
        action_digest=checked.action_digest,
        observation=observation if observation is not None else _observation(checked),
        validity_scope=scope,
        data_snapshot_token=snapshot_token,
        observed_at=checked.completed_at,
        strength=1.0,
        created_at=checked.completed_at,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=len(payload_bytes),
        ),
    )


def _context(
    *,
    evaluated_at: datetime = NOW,
    run_id: str = RUN_ID,
    incarnation: str = INCARNATION,
    schema: str = SCHEMA,
    documents: tuple[DocumentSourceState, ...] = (),
    snapshots: tuple[DataSnapshotValidation, ...] = (),
) -> FreshnessContext:
    return FreshnessContext(
        evaluated_at=evaluated_at,
        run_id=run_id,
        run_incarnation=incarnation,
        schema_namespace_version=schema,
        document_sources=documents,
        data_snapshots=snapshots,
    )


def test_schema_and_run_only_freshness_are_exact_and_fail_closed() -> None:
    schema_evidence = _evidence()
    assert evaluate_evidence_freshness(schema_evidence, _context()).status is FreshnessStatus.FRESH
    changed = evaluate_evidence_freshness(
        schema_evidence,
        _context(schema=OTHER_SCHEMA),
    )
    assert (changed.status, changed.reason) == (
        FreshnessStatus.STALE,
        FreshnessReason.SCHEMA_VERSION_CHANGED,
    )

    data_provenance = _provenance(
        kind=ResearchActionKind.PROFILE_COLUMN,
        target=_column(),
    )
    run_only = _evidence(
        scope=EvidenceValidityScope.RUN_ONLY,
        source_kind=EvidenceSourceKind.PROFILE,
        provenance=data_provenance,
    )
    assert evaluate_evidence_freshness(run_only, _context()).status is FreshnessStatus.FRESH
    cross_run = evaluate_evidence_freshness(run_only, _context(run_id="run-2"))
    assert (cross_run.status, cross_run.reason) == (
        FreshnessStatus.REVALIDATION_REQUIRED,
        FreshnessReason.RUN_CONTEXT_CHANGED,
    )


@pytest.mark.parametrize(
    ("offset", "status"),
    [
        (timedelta(microseconds=-1), FreshnessStatus.FRESH),
        (timedelta(0), FreshnessStatus.REVALIDATION_REQUIRED),
        (timedelta(microseconds=1), FreshnessStatus.REVALIDATION_REQUIRED),
    ],
)
def test_document_expiry_boundary_requires_revalidation(offset, status) -> None:
    valid_until = NOW + timedelta(hours=1)
    provenance = _provenance(
        kind=ResearchActionKind.READ_DOCUMENT,
        target=_document(),
        source_version="sha256:" + "1" * 64,
        valid_until=valid_until,
    )
    evidence = _evidence(
        source_kind=EvidenceSourceKind.DOCUMENT,
        provenance=provenance,
    )
    context = _context(
        evaluated_at=valid_until + offset,
        documents=(
            DocumentSourceState(
                document_id="orders-rule",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version=provenance.source_version,
            ),
        ),
    )
    decision = evaluate_evidence_freshness(evidence, context)
    assert decision.status is status
    assert decision.reason is (
        FreshnessReason.VALID
        if status is FreshnessStatus.FRESH
        else FreshnessReason.SOURCE_EXPIRED
    )


@pytest.mark.parametrize(
    ("source", "status", "reason"),
    [
        (
            DocumentSourceState(
                document_id="orders-rule",
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="sha256:" + "2" * 64,
            ),
            FreshnessStatus.STALE,
            FreshnessReason.SOURCE_VERSION_CHANGED,
        ),
        (
            DocumentSourceState(
                document_id="orders-rule",
                availability=DocumentSourceAvailability.REMOVED,
                source_version=None,
            ),
            FreshnessStatus.STALE,
            FreshnessReason.SOURCE_REMOVED,
        ),
        (
            DocumentSourceState(
                document_id="orders-rule",
                availability=DocumentSourceAvailability.UNAVAILABLE,
                source_version=None,
            ),
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.SOURCE_UNAVAILABLE,
        ),
    ],
)
def test_document_changed_removed_and_unavailable_are_distinct(source, status, reason) -> None:
    provenance = _provenance(
        kind=ResearchActionKind.READ_DOCUMENT,
        target=_document(),
        source_version="sha256:" + "1" * 64,
    )
    evidence = _evidence(
        source_kind=EvidenceSourceKind.DOCUMENT,
        provenance=provenance,
    )
    decision = evaluate_evidence_freshness(
        evidence,
        _context(documents=(source,)),
    )
    assert (decision.status, decision.reason) == (status, reason)


def test_legacy_malformed_and_mismatched_provenance_never_become_fresh() -> None:
    legacy = _evidence(observation="legacy observation without provenance")
    decision = evaluate_evidence_freshness(legacy, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.REVALIDATION_REQUIRED,
        FreshnessReason.LEGACY_PROVENANCE,
    )

    legacy_json = _evidence(
        observation=canonical_json_bytes(
            {"payload": {}, "summary": "legacy JSON without provenance"}
        ).decode("utf-8")
    )
    decision = evaluate_evidence_freshness(legacy_json, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.REVALIDATION_REQUIRED,
        FreshnessReason.LEGACY_PROVENANCE,
    )

    malformed_mapping = json.loads(_observation(_provenance()))
    malformed_mapping["provenance"]["unexpected"] = True
    malformed = _evidence(
        observation=canonical_json_bytes(malformed_mapping).decode("utf-8")
    )
    decision = evaluate_evidence_freshness(malformed, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.MALFORMED_PROVENANCE,
    )

    malformed_json = _evidence(observation='{"provenance":')
    decision = evaluate_evidence_freshness(malformed_json, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.MALFORMED_PROVENANCE,
    )

    mismatched = _evidence(
        provenance=_provenance(action_digest="sha256:" + "e" * 64),
    ).model_copy(update={"action_digest": ACTION_DIGEST})
    decision = evaluate_evidence_freshness(mismatched, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.PROVENANCE_MISMATCH,
    )


def test_provenance_observation_rejects_noncanonical_or_inconsistent_inline_data() -> None:
    evidence = _evidence()
    original = json.loads(evidence.observation)
    invalid_observations = []

    changed_payload = dict(original)
    changed_payload["payload"] = {"changed": True}
    invalid_observations.append(canonical_json_bytes(changed_payload).decode("utf-8"))

    changed_digest = dict(original)
    changed_digest["payload_digest"] = "sha256:" + "e" * 64
    invalid_observations.append(canonical_json_bytes(changed_digest).decode("utf-8"))

    changed_invocation = dict(original)
    changed_invocation["invocation_id"] = "other-invocation"
    invalid_observations.append(
        canonical_json_bytes(changed_invocation).decode("utf-8")
    )

    changed_kind = dict(original)
    changed_kind["probe_kind"] = ResearchActionKind.INSPECT_COLUMN
    invalid_observations.append(canonical_json_bytes(changed_kind).decode("utf-8"))

    missing_version = dict(original)
    missing_version.pop("observation_version")
    invalid_observations.append(canonical_json_bytes(missing_version).decode("utf-8"))

    for version in (None, 0, 2):
        changed_observation_version = dict(original)
        changed_observation_version["observation_version"] = version
        invalid_observations.append(
            canonical_json_bytes(changed_observation_version).decode("utf-8")
        )

    for version in (1, None, 0, 2):
        invalid_observations.append(
            canonical_json_bytes(
                {"observation_version": version, "payload": {}}
            ).decode("utf-8")
        )

    for version in (None, 0, 2):
        changed_provenance_version = json.loads(evidence.observation)
        if version is None:
            changed_provenance_version["provenance"].pop("provenance_version")
        else:
            changed_provenance_version["provenance"]["provenance_version"] = version
        invalid_observations.append(
            canonical_json_bytes(changed_provenance_version).decode("utf-8")
        )

    missing_payload = dict(original)
    missing_payload.pop("payload")
    invalid_observations.append(canonical_json_bytes(missing_payload).decode("utf-8"))

    wrong_storage = dict(original)
    wrong_storage["storage"] = "artifact"
    invalid_observations.append(canonical_json_bytes(wrong_storage).decode("utf-8"))

    unexpected_artifact = dict(original)
    unexpected_artifact["artifact_reference"] = {
        "artifact_id": "artifact-1",
        "byte_count": original["byte_count"],
        "digest": original["payload_digest"],
    }
    invalid_observations.append(
        canonical_json_bytes(unexpected_artifact).decode("utf-8")
    )

    normalized_number = dict(original)
    normalized_number["row_count"] = 0.0
    invalid_observations.append(
        canonical_json_bytes(normalized_number).decode("utf-8")
    )

    invalid_observations.append(json.dumps(original, indent=2, sort_keys=True))
    invalid_observations.append(
        evidence.observation.replace(
            '{"artifact_reference":null,',
            '{"artifact_reference":null,"artifact_reference":null,',
            1,
        )
    )

    for observation in invalid_observations:
        decision = evaluate_evidence_freshness(
            evidence.model_copy(update={"observation": observation}),
            _context(),
        )
        assert (decision.status, decision.reason) == (
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.MALFORMED_PROVENANCE,
        )


@pytest.mark.parametrize(
    ("timestamp", "expected_status", "expected_reason"),
    [
        (
            "2026-07-30T12:00:00.000000Z",
            FreshnessStatus.FRESH,
            FreshnessReason.VALID,
        ),
        (
            "2026-07-30T12:00:00Z",
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.MALFORMED_PROVENANCE,
        ),
        (
            "2026-07-30T12:00:00+00:00",
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.MALFORMED_PROVENANCE,
        ),
        (
            "2026-07-30T12:00:00.000000+00:00",
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.MALFORMED_PROVENANCE,
        ),
        (
            "2026-07-30 12:00:00.000000Z",
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.MALFORMED_PROVENANCE,
        ),
    ],
)
def test_nested_provenance_timestamp_requires_model_canonical_spelling(
    timestamp,
    expected_status,
    expected_reason,
) -> None:
    evidence = _evidence()
    mapping = json.loads(evidence.observation)
    mapping["provenance"]["started_at"] = timestamp

    decision = evaluate_evidence_freshness(
        evidence.model_copy(
            update={"observation": canonical_json_bytes(mapping).decode("utf-8")}
        ),
        _context(),
    )
    assert (decision.status, decision.reason) == (
        expected_status,
        expected_reason,
    )


def test_nested_document_expiry_requires_model_canonical_spelling() -> None:
    source_version = "sha256:" + "1" * 64
    provenance = _provenance(
        kind=ResearchActionKind.READ_DOCUMENT,
        target=_document(),
        source_version=source_version,
        valid_until=NOW + timedelta(hours=1),
    )
    evidence = _evidence(
        source_kind=EvidenceSourceKind.DOCUMENT,
        provenance=provenance,
    )
    mapping = json.loads(evidence.observation)
    mapping["provenance"]["valid_until"] = "2026-07-30T13:00:00Z"

    decision = evaluate_evidence_freshness(
        evidence.model_copy(
            update={"observation": canonical_json_bytes(mapping).decode("utf-8")}
        ),
        _context(
            documents=(
                DocumentSourceState(
                    document_id="orders-rule",
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version=source_version,
                ),
            ),
        ),
    )
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.MALFORMED_PROVENANCE,
    )


def test_provenance_observation_matches_evidence_cost_and_both_timestamps() -> None:
    evidence = _evidence()
    mapping = json.loads(evidence.observation)
    mapping["row_count"] = 1
    mismatched_rows = evidence.model_copy(
        update={"observation": canonical_json_bytes(mapping).decode("utf-8")}
    )
    mismatched_bytes = evidence.model_copy(
        update={"cost": evidence.cost.model_copy(update={"bytes": 3})}
    )
    mismatched_created_at = evidence.model_copy(
        update={"created_at": evidence.created_at + timedelta(microseconds=1)}
    )
    mismatched_observed_at = evidence.model_copy(
        update={"observed_at": evidence.observed_at + timedelta(microseconds=1)}
    )

    for mismatched in (
        mismatched_rows,
        mismatched_bytes,
        mismatched_created_at,
        mismatched_observed_at,
    ):
        decision = evaluate_evidence_freshness(mismatched, _context())
        assert (decision.status, decision.reason) == (
            FreshnessStatus.UNAVAILABLE,
            FreshnessReason.PROVENANCE_MISMATCH,
        )


def test_artifact_observation_requires_matching_reference_metadata() -> None:
    evidence = _evidence()
    mapping = json.loads(evidence.observation)
    mapping.update(
        {
            "artifact_reference": {
                "artifact_id": "artifact-1",
                "byte_count": mapping["byte_count"],
                "digest": mapping["payload_digest"],
            },
            "payload": None,
            "storage": "artifact",
        }
    )
    artifact_evidence = evidence.model_copy(
        update={"observation": canonical_json_bytes(mapping).decode("utf-8")}
    )
    assert (
        evaluate_evidence_freshness(artifact_evidence, _context()).status
        is FreshnessStatus.FRESH
    )

    mapping["artifact_reference"]["byte_count"] += 1
    tampered = artifact_evidence.model_copy(
        update={"observation": canonical_json_bytes(mapping).decode("utf-8")}
    )
    decision = evaluate_evidence_freshness(tampered, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.MALFORMED_PROVENANCE,
    )


@pytest.mark.parametrize("field", ["source_version", "valid_until"])
def test_inline_document_metadata_must_match_provenance(field) -> None:
    provenance = _provenance(
        kind=ResearchActionKind.READ_DOCUMENT,
        target=_document(),
        source_version="sha256:" + "1" * 64,
        valid_until=NOW + timedelta(hours=1),
    )
    evidence = _evidence(
        source_kind=EvidenceSourceKind.DOCUMENT,
        provenance=provenance,
    )
    mapping = json.loads(evidence.observation)
    mapping["payload"]["document"][field] = (
        "sha256:" + "2" * 64
        if field == "source_version"
        else "2026-07-30T14:00:00.000000Z"
    )
    rewritten_digest = canonical_digest(mapping["payload"])
    mapping["payload_digest"] = rewritten_digest
    mapping["provenance"]["payload_digest"] = rewritten_digest

    decision = evaluate_evidence_freshness(
        evidence.model_copy(
            update={"observation": canonical_json_bytes(mapping).decode("utf-8")}
        ),
        _context(),
    )
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.MALFORMED_PROVENANCE,
    )


@pytest.mark.parametrize(
    ("evidence",),
    [
        (
            _evidence(
                scope=EvidenceValidityScope.RUN_ONLY,
                provenance=_provenance(),
            ),
        ),
        (
            _evidence(
                scope=EvidenceValidityScope.SCHEMA_VERSION,
                source_kind=EvidenceSourceKind.PROFILE,
                provenance=_provenance(
                    kind=ResearchActionKind.PROFILE_COLUMN,
                    target=_column(),
                ),
            ),
        ),
    ],
)
def test_probe_kind_cannot_masquerade_as_another_source_or_scope(evidence) -> None:
    decision = evaluate_evidence_freshness(evidence, _context())
    assert (decision.status, decision.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.PROVENANCE_MISMATCH,
    )


def test_data_snapshot_cannot_be_added_outside_the_provenance_chain() -> None:
    evidence = _evidence(
        scope=EvidenceValidityScope.DATA_SNAPSHOT,
        source_kind=EvidenceSourceKind.PROBE,
        provenance=_provenance(kind=ResearchActionKind.EXECUTE_PROBE),
        snapshot_token="snapshot-1",
    )
    unavailable = evaluate_evidence_freshness(evidence, _context())
    assert (unavailable.status, unavailable.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.PROVENANCE_MISMATCH,
    )

    still_unavailable = evaluate_evidence_freshness(
        evidence,
        _context(
            snapshots=(
                DataSnapshotValidation(
                    token="snapshot-1",
                    status=DataSnapshotStatus.VALID,
                ),
            ),
        ),
    )
    assert (still_unavailable.status, still_unavailable.reason) == (
        FreshnessStatus.UNAVAILABLE,
        FreshnessReason.PROVENANCE_MISMATCH,
    )


def _budget() -> BudgetState:
    values = {}
    for name in ("wall_clock_ms", "model_calls", "model_tokens", "db_probe_ms", "rows", "bytes"):
        values[f"initial_{name}"] = 1
        values[f"used_{name}"] = 0
        values[f"remaining_{name}"] = 1
    return BudgetState(**values)


def _state(evidence: tuple[EvidenceRecord, ...]) -> ResearchState:
    item = SemanticItem(
        source_id="source-1",
        kind=SemanticItemKind.FILTER,
        source_text="orders",
        normalized_meaning="orders",
        required=True,
        operator=None,
        literal_or_reference=None,
        status=SemanticItemStatus.UNRESOLVED,
        binding_ids=(),
    )
    return ResearchState(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        query_spec=QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=None,
            query_id="query-1",
            original_text="orders",
            semantic_items=(item,),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=(),
        ),
        hypotheses=(),
        evidence=evidence,
        bindings=(),
        join_candidates=(),
        unresolved_items=("source-1",),
        action_history=(
            ResearchAction(
                action_id="freshness-action-1",
                kind=ResearchActionKind.INSPECT_TABLE,
                hypothesis_id=None,
                target=_table(),
                parameters=(),
                action_digest=ACTION_DIGEST,
                expected_revision=0,
            ),
        ),
        result_expectations=(),
        budget_state=_budget(),
        stop_reason=None,
    )


def test_projection_is_sorted_and_gate_rejects_only_new_nonfresh_support() -> None:
    fresh = _evidence(evidence_id="evidence-a")
    legacy = _evidence(
        evidence_id="evidence-b",
        observation="legacy observation",
    )
    state = _state((legacy, fresh))
    projection = project_evidence_freshness(state.evidence, _context())
    assert tuple(item.evidence_id for item in projection.decisions) == (
        "evidence-a",
        "evidence-b",
    )

    supported_binding = PhysicalColumnBinding(
        binding_id="binding-1",
        source_id="source-1",
        tables=(_table(),),
        columns=(_column(),),
        predicates=(),
        join_path=(),
        evidence_ids=("evidence-b",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="freshness",
        physical_column=_column(),
    )
    with pytest.raises(FreshnessValidationError, match="fresh evidence"):
        validate_supported_updates(
            state,
            _context(),
            bindings=(supported_binding,),
        )

    supported_hypothesis = Hypothesis(
        hypothesis_id="hypothesis-1",
        source_ids=("source-1",),
        claim="orders contain the requested status",
        candidate_targets=(_table(),),
        status=HypothesisStatus.SUPPORTED,
        evidence_ids=("evidence-b",),
    )
    with pytest.raises(FreshnessValidationError, match="fresh evidence"):
        validate_supported_updates(
            state,
            _context(),
            hypotheses=(supported_hypothesis,),
        )
    assert state.bindings == ()
    assert state.hypotheses == ()

    fresh_binding = supported_binding.model_copy(
        update={"evidence_ids": ("evidence-a",)}
    )
    computed = validate_supported_updates(
        state,
        _context(),
        bindings=(fresh_binding,),
    )
    assert computed == projection

    state_with_supported_binding = state.model_copy(
        update={"bindings": (fresh_binding,)}
    )
    changed_supported_binding = fresh_binding.model_copy(
        update={"evidence_ids": ("evidence-b",)}
    )
    with pytest.raises(FreshnessValidationError, match="fresh evidence"):
        validate_supported_updates(
            state_with_supported_binding,
            _context(),
            bindings=(changed_supported_binding,),
        )

    fresh_hypothesis = supported_hypothesis.model_copy(
        update={"evidence_ids": ("evidence-a",)}
    )
    state_with_supported_hypothesis = state.model_copy(
        update={"hypotheses": (fresh_hypothesis,)}
    )
    changed_supported_hypothesis = fresh_hypothesis.model_copy(
        update={"evidence_ids": ("evidence-b",)}
    )
    with pytest.raises(FreshnessValidationError, match="fresh evidence"):
        validate_supported_updates(
            state_with_supported_hypothesis,
            _context(),
            hypotheses=(changed_supported_hypothesis,),
        )


def test_supported_update_gate_cannot_trust_forged_or_duplicate_evidence() -> None:
    fresh = _evidence(evidence_id="evidence-a")
    legacy = _evidence(
        evidence_id="evidence-b",
        observation="legacy observation",
    )
    malformed = _evidence(
        evidence_id="evidence-c",
        observation='{"provenance":',
    )
    state = _state((fresh, legacy, malformed))
    supported = PhysicalColumnBinding(
        binding_id="binding-forged",
        source_id="source-1",
        tables=(_table(),),
        columns=(_column(),),
        predicates=(),
        join_path=(),
        evidence_ids=("evidence-b",),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="freshness",
        physical_column=_column(),
    )

    for evidence_id in ("evidence-b", "evidence-c"):
        forged_projection = FreshnessProjection(
            decisions=(
                FreshnessDecision(
                    evidence_id=evidence_id,
                    status=FreshnessStatus.FRESH,
                    reason=FreshnessReason.VALID,
                    evaluated_at=NOW,
                ),
            )
        )
        with pytest.raises(FreshnessValidationError, match="fresh evidence"):
            validate_supported_updates(
                state,
                _context(),
                bindings=(
                    supported.model_copy(
                        update={"evidence_ids": (evidence_id,)}
                    ),
                ),
            )
        with pytest.raises(TypeError, match="context"):
            validate_supported_updates(
                state,
                forged_projection,  # type: ignore[arg-type]
                bindings=(
                    supported.model_copy(
                        update={"evidence_ids": (evidence_id,)}
                    ),
                ),
            )

    absent = supported.model_copy(update={"evidence_ids": ("missing",)})
    with pytest.raises(FreshnessValidationError, match="fresh evidence"):
        validate_supported_updates(state, _context(), bindings=(absent,))

    duplicate = supported.model_copy(
        update={"evidence_ids": ("evidence-a", "evidence-a")}
    )
    with pytest.raises(FreshnessValidationError, match="fresh evidence"):
        validate_supported_updates(state, _context(), bindings=(duplicate,))


def test_freshness_context_rejects_ambiguous_or_naive_inputs() -> None:
    source = DocumentSourceState(
        document_id="orders-rule",
        availability=DocumentSourceAvailability.AVAILABLE,
        source_version="v1",
    )
    with pytest.raises(ValidationError):
        _context(documents=(source, source))
    with pytest.raises(ValidationError, match="UTC"):
        _context(evaluated_at=datetime(2026, 7, 30, 12, 0))
