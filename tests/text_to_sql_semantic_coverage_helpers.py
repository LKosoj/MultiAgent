"""Shared builders for W5-00 semantic coverage tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from custom_tools.text_to_sql.adaptive.freshness import (
    DataSnapshotValidation,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.models import (
    BindingStatus,
    BudgetState,
    ColumnRef,
    DocumentRef,
    DocumentRuleBinding,
    EvidenceCost,
    EvidenceRecord,
    EvidenceSourceKind,
    EvidenceValidityScope,
    ExpectedResultShape,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    PhysicalColumnBinding,
    PredicateRef,
    QuerySpec,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    ResearchStopReason,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    TableRef,
)
from custom_tools.text_to_sql.adaptive.provenance import ProbeProvenance
from custom_tools.text_to_sql.adaptive.policy import canonical_action_digest
from custom_tools.text_to_sql.adaptive.semantic_coverage import (
    CoverageInputError,
    CoverageInputErrorCode,
    validate_coverage_inputs,
)
from custom_tools.text_to_sql.adaptive.serialization import (
    canonical_digest,
    canonical_json_bytes,
)

RUN_ID = "coverage-run"
INCARNATION = "coverage-incarnation"
SCHEMA = "sha256:" + "a" * 64
OTHER_SCHEMA = "sha256:" + "b" * 64
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _table(name: str) -> TableRef:
    return TableRef(namespace="main", schema=None, table=name)


def _column(table: str, name: str) -> ColumnRef:
    return ColumnRef(table=_table(table), column=name)


def _budget() -> BudgetState:
    values: dict[str, int] = {}
    for name in (
        "wall_clock_ms",
        "model_calls",
        "model_tokens",
        "db_probe_ms",
        "rows",
        "bytes",
    ):
        values[f"initial_{name}"] = 1
        values[f"used_{name}"] = 0
        values[f"remaining_{name}"] = 1
    return BudgetState(**values)


def _observation(
    provenance: ProbeProvenance,
    payload: object,
    *,
    row_count: int = 0,
) -> str:
    payload_bytes = canonical_json_bytes(payload)
    return canonical_json_bytes(
        {
            "artifact_reference": None,
            "byte_count": len(payload_bytes),
            "invocation_id": provenance.invocation_id,
            "observation_version": 1,
            "payload": payload,
            "payload_digest": provenance.payload_digest,
            "probe_kind": provenance.probe_kind,
            "provenance": provenance,
            "row_count": row_count,
            "storage": "inline",
            "summary": "coverage evidence",
            "truncated": False,
        }
    ).decode("utf-8")


def _schema_evidence(
    evidence_id: str,
    target: TableRef | ColumnRef,
    *,
    kind: ResearchActionKind = ResearchActionKind.INSPECT_COLUMN,
    completed_at: datetime = NOW,
    run_id: str = RUN_ID,
    run_incarnation: str = INCARNATION,
    revision: int = 1,
    provenance_run_id: str | None = None,
    provenance_run_incarnation: str | None = None,
    provenance_schema: str = SCHEMA,
) -> EvidenceRecord:
    payload: dict[str, object] = (
        {
            "status": "matched",
            "column": target.model_dump(mode="json", by_alias=True),
        }
        if kind is ResearchActionKind.INSPECT_COLUMN and isinstance(target, ColumnRef)
        else {}
    )
    provenance = ProbeProvenance(
        provenance_version=1,
        run_id=provenance_run_id or run_id,
        run_incarnation=provenance_run_incarnation or run_incarnation,
        invocation_id=evidence_id,
        action_digest=canonical_digest({"action": evidence_id}),
        probe_kind=kind,
        target=target,
        schema_namespace_version=provenance_schema,
        payload_digest=canonical_digest(payload),
        started_at=completed_at,
        completed_at=completed_at,
    )
    return EvidenceRecord(
        run_id=run_id,
        run_incarnation=run_incarnation,
        revision=revision,
        schema_namespace_version=SCHEMA,
        evidence_id=evidence_id,
        source_kind=EvidenceSourceKind.SCHEMA,
        target=target,
        action_digest=provenance.action_digest,
        observation=_observation(provenance, payload),
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=completed_at,
        strength=1.0,
        created_at=completed_at,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=0,
            bytes=len(canonical_json_bytes(payload)),
        ),
    )


def _value_evidence(
    evidence_id: str,
    column: ColumnRef,
    value: str | int | float | bool | None,
    *,
    kind: ResearchActionKind = ResearchActionKind.SEARCH_VALUE,
    completed_at: datetime = NOW,
) -> EvidenceRecord:
    payload: dict[str, object] = {
        "columns": [column.column],
        "rows": [[value]],
    }
    provenance = ProbeProvenance(
        provenance_version=1,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        invocation_id=evidence_id,
        action_digest=canonical_digest({"action": evidence_id}),
        probe_kind=kind,
        target=column,
        schema_namespace_version=SCHEMA,
        payload_digest=canonical_digest(payload),
        started_at=completed_at,
        completed_at=completed_at,
    )
    return EvidenceRecord(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        evidence_id=evidence_id,
        source_kind=EvidenceSourceKind.VALUE_SEARCH,
        target=column,
        action_digest=provenance.action_digest,
        observation=_observation(provenance, payload, row_count=1),
        validity_scope=EvidenceValidityScope.RUN_ONLY,
        data_snapshot_token=None,
        observed_at=completed_at,
        strength=1.0,
        created_at=completed_at,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=1,
            rows=1,
            bytes=len(canonical_json_bytes(payload)),
        ),
    )


def _document_evidence(
    evidence_id: str,
    *,
    completed_at: datetime = NOW,
    valid_until: datetime | None = None,
    content: str = "Documented formula rule.",
) -> EvidenceRecord:
    document = DocumentRef(document_id="coverage-document", namespace="main")
    payload = {
        "document": {
            "source_version": "v1",
            "valid_until": valid_until,
            "content": content,
        }
    }
    provenance = ProbeProvenance(
        provenance_version=1,
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        invocation_id=evidence_id,
        action_digest=canonical_digest({"action": evidence_id}),
        probe_kind=ResearchActionKind.READ_DOCUMENT,
        target=document,
        schema_namespace_version=SCHEMA,
        payload_digest=canonical_digest(payload),
        started_at=completed_at,
        completed_at=completed_at,
        source_version="v1",
        valid_until=valid_until,
    )
    return EvidenceRecord(
        run_id=RUN_ID,
        run_incarnation=INCARNATION,
        revision=1,
        schema_namespace_version=SCHEMA,
        evidence_id=evidence_id,
        source_kind=EvidenceSourceKind.DOCUMENT,
        target=document,
        action_digest=provenance.action_digest,
        observation=_observation(provenance, payload),
        validity_scope=EvidenceValidityScope.SCHEMA_VERSION,
        data_snapshot_token=None,
        observed_at=completed_at,
        strength=1.0,
        created_at=completed_at,
        cost=EvidenceCost(
            wall_clock_ms=1,
            model_calls=0,
            model_tokens=0,
            db_probe_ms=0,
            rows=0,
            bytes=len(canonical_json_bytes(payload)),
        ),
    )


def _physical_binding(
    source_id: str,
    binding_id: str,
    evidence_id: str,
    *,
    status: BindingStatus = BindingStatus.SUPPORTED,
    join_path: tuple[JoinEdge, ...] = (),
) -> PhysicalColumnBinding:
    column = _column(f"table-{source_id}", f"column-{source_id}")
    return PhysicalColumnBinding(
        binding_id=binding_id,
        source_id=source_id,
        tables=(column.table,),
        columns=(column,),
        predicates=(),
        join_path=join_path,
        evidence_ids=(evidence_id,),
        confidence=1.0,
        status=status,
        validator_rule="coverage" if status is BindingStatus.SUPPORTED else None,
        physical_column=column,
    )


def _document_binding(
    source_id: str,
    binding_id: str,
    evidence_id: str,
) -> DocumentRuleBinding:
    return DocumentRuleBinding(
        binding_id=binding_id,
        source_id=source_id,
        tables=(),
        columns=(),
        predicates=(),
        join_path=(),
        evidence_ids=(evidence_id,),
        confidence=1.0,
        status=BindingStatus.SUPPORTED,
        validator_rule="coverage",
        document=DocumentRef(document_id="coverage-document", namespace="main"),
        rule_id="coverage-rule",
        rule_text="validated rule",
    )


def _join_candidate(
    join_id: str,
    path: tuple[JoinEdge, ...],
    evidence_id: str,
    *,
    status: JoinCandidateStatus = JoinCandidateStatus.VALIDATED,
) -> JoinCandidate:
    first = path[0]
    return JoinCandidate(
        join_id=join_id,
        left=first.left,
        right=first.right,
        join_type=first.join_type,
        path=path,
        status=status,
        evidence_ids=(evidence_id,) if status is JoinCandidateStatus.VALIDATED else (),
    )


def _state(
    *,
    item_specs: tuple[tuple[str, bool, SemanticItemStatus, tuple[str, ...]], ...] = (
        ("source-a", True, SemanticItemStatus.RESOLVED, ("binding-a",)),
    ),
    bindings: tuple[object, ...] | None = None,
    evidence: tuple[EvidenceRecord, ...] | None = None,
    joins: tuple[JoinCandidate, ...] = (),
    unresolved_items: tuple[str, ...] = (),
    global_constraints: tuple[PredicateRef, ...] = (),
    stop_reason: ResearchStopReason | None = None,
    action_history: tuple[ResearchAction, ...] | None = None,
    revision: int = 1,
) -> ResearchState:
    if action_history is None:
        action_history = tuple(
            _action((("revision", index),), index=index) for index in range(revision)
        )
    source_texts = [spec[0] for spec in item_specs]
    original_text = " ".join(source_texts)
    semantic_items = []
    for source_id, required, status, binding_ids in item_specs:
        semantic_items.append(
            SemanticItem(
                source_id=source_id,
                kind=SemanticItemKind.DIMENSION,
                source_text=source_id,
                normalized_meaning=source_id,
                required=required,
                operator=None,
                literal_or_reference=None,
                status=status,
                binding_ids=binding_ids,
            )
        )
    if evidence is None:
        evidence = (
            _schema_evidence(
                "evidence-a",
                _column("table-source-a", "column-source-a"),
            ),
        )
    if bindings is None:
        bindings = (_physical_binding("source-a", "binding-a", "evidence-a"),)
    values = {
        "run_id": RUN_ID,
        "run_incarnation": INCARNATION,
        "revision": revision,
        "schema_namespace_version": SCHEMA,
        "query_spec": QuerySpec(
            run_id=RUN_ID,
            run_incarnation=INCARNATION,
            revision=0,
            schema_namespace_version=SCHEMA,
            query_id="coverage-query",
            original_text=original_text,
            semantic_items=tuple(semantic_items),
            expected_result_shape=ExpectedResultShape.ROWS,
            global_constraints=global_constraints,
        ),
        "hypotheses": (),
        "evidence": evidence,
        "bindings": bindings,
        "join_candidates": joins,
        "result_expectations": (),
        "unresolved_items": unresolved_items,
        "action_history": action_history,
        "budget_state": _budget(),
        "stop_reason": stop_reason,
    }
    try:
        return ResearchState(**values)
    except ValidationError:
        return ResearchState.model_construct(**values)


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


def _action(
    parameters: tuple[tuple[str, str | int | float | bool | None], ...],
    *,
    index: int = 0,
) -> ResearchAction:
    target = _table("orders")
    return ResearchAction(
        action_id=f"coverage-action-{index}",
        kind=ResearchActionKind.INSPECT_TABLE,
        hypothesis_id=None,
        target=target,
        parameters=parameters,
        action_digest=canonical_action_digest(
            kind=ResearchActionKind.INSPECT_TABLE,
            hypothesis_id=None,
            target=target,
            parameters=parameters,
            expected_revision=index,
        ),
        expected_revision=index,
    )


def _validate(
    state: ResearchState,
    context: FreshnessContext | None = None,
):
    return validate_coverage_inputs(
        state,
        context or _context(),
        RUN_ID,
        INCARNATION,
    )


def _assert_error(
    code: CoverageInputErrorCode,
    source_ids: tuple[str, ...],
    callback,
) -> None:
    with pytest.raises(CoverageInputError) as captured:
        callback()
    assert captured.value.code is code
    assert captured.value.affected_source_ids == source_ids
    assert str(captured.value) == code.value
