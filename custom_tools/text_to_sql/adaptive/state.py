"""Pure, immutable transitions for adaptive Text-to-SQL research state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from ._semantic_matching import predicate_matches
from .evidence import evidence_source_kind_for_action
from .models import (
    BindingBase,
    BindingStatus,
    BudgetState,
    DiscriminatorValueBinding,
    EvidenceRecord,
    Hypothesis,
    HypothesisStatus,
    JoinCandidate,
    JoinCandidateStatus,
    PredicateRef,
    ResearchAction,
    ResearchState,
    ResultExpectation,
    ResearchStopReason,
    QuerySpec,
    SemanticItem,
    SemanticItemKind,
    SemanticItemStatus,
    is_binding_free_semantic_item,
    is_structurally_resolved_limit,
)
from ._semantic_resolution import derive_semantic_resolution
from .policy import ResearchPolicyError, canonical_digest_for_action


class ResearchTransitionError(ValueError):
    """Base error for a rejected immutable research-state transition."""


class ResearchTransitionValidationError(ResearchTransitionError):
    """A state or transition value bypassed its strict model contract."""


class ResearchTransitionRevisionError(ResearchTransitionError):
    """An action does not apply to the current revision."""


class ResearchTransitionConflictError(ResearchTransitionError):
    """The transition conflicts with append-only action or entity history."""


class ResearchTransitionReferenceError(ResearchTransitionError):
    """A transition refers to a run, schema, action, or entity outside its state."""


class ResearchTransitionProtocolError(ResearchTransitionError):
    """A status transition or terminal-state rule is invalid."""


@dataclass(frozen=True, slots=True)
class ResearchNovelty:
    """Deterministic description of facts introduced by one transition."""

    is_novel: bool
    action_id: str
    action_digest: str
    added_evidence_ids: tuple[str, ...]
    added_hypothesis_ids: tuple[str, ...]
    updated_hypothesis_ids: tuple[str, ...]
    added_binding_ids: tuple[str, ...]
    updated_binding_ids: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    stop_reason: ResearchStopReason | None
    added_join_ids: tuple[str, ...] = ()
    updated_join_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchTransitionResult:
    """One accepted immutable state transition and its deterministic novelty."""

    state: ResearchState
    novelty: ResearchNovelty


_Model = TypeVar("_Model", bound=BaseModel)
_HYPOTHESIS_STATUS_TRANSITIONS = {
    HypothesisStatus.PROPOSED: frozenset(
        {
            HypothesisStatus.TESTING,
            HypothesisStatus.SUPPORTED,
            HypothesisStatus.REJECTED,
        }
    ),
    HypothesisStatus.TESTING: frozenset(
        {HypothesisStatus.SUPPORTED, HypothesisStatus.REJECTED}
    ),
    HypothesisStatus.SUPPORTED: frozenset(),
    HypothesisStatus.REJECTED: frozenset(),
}
_BINDING_STATUS_TRANSITIONS = {
    BindingStatus.CANDIDATE: frozenset(
        {BindingStatus.SUPPORTED, BindingStatus.REJECTED, BindingStatus.STALE}
    ),
    BindingStatus.SUPPORTED: frozenset({BindingStatus.STALE}),
    BindingStatus.REJECTED: frozenset(),
    BindingStatus.STALE: frozenset(),
}


def apply_research_transition(
    state: ResearchState,
    action: ResearchAction,
    *,
    evidence: Iterable[EvidenceRecord] = (),
    hypotheses: Iterable[Hypothesis] = (),
    bindings: Iterable[BindingBase] = (),
    join_candidates: Iterable[JoinCandidate] = (),
    result_expectations: Iterable[ResultExpectation] = (),
    budget_state: BudgetState | None = None,
    stop_reason: ResearchStopReason | None = None,
    declared_join_ids: tuple[str, ...] = (),
) -> ResearchTransitionResult:
    """Append one action and its derived facts without any runtime side effects."""

    current = _revalidate(state, ResearchState, "research state")
    next_action = _revalidate(action, ResearchAction, "research action")
    if current.stop_reason is not None:
        raise ResearchTransitionProtocolError(
            "stopped research state cannot accept another action"
        )
    if next_action.expected_revision != current.revision:
        raise ResearchTransitionRevisionError(
            "action expected_revision must equal state revision"
        )
    if next_action.action_digest != _canonical_action_digest(next_action):
        raise ResearchTransitionValidationError(
            "action_digest does not match canonical action semantics"
        )

    _validate_history(current)
    action_ids = {item.action_id for item in current.action_history}
    action_digests = {item.action_digest for item in current.action_history}
    if (
        next_action.action_id in action_ids
        or next_action.action_digest in action_digests
    ):
        raise ResearchTransitionConflictError("duplicate action_id or action_digest")

    next_revision = current.revision + 1
    new_evidence = _validated_unique(
        evidence, EvidenceRecord, "evidence", "evidence_id"
    )
    _validate_new_evidence(current, next_action, next_revision, new_evidence)
    evidence_ids = {item.evidence_id for item in current.evidence}
    if evidence_ids.intersection(item.evidence_id for item in new_evidence):
        raise ResearchTransitionConflictError("evidence_id is append-only")
    merged_evidence = (*current.evidence, *new_evidence)
    new_expectations = _validated_result_expectations(result_expectations)
    existing_expectation_keys = {_result_expectation_key(item) for item in current.result_expectations}
    if existing_expectation_keys.intersection(
        _result_expectation_key(item) for item in new_expectations
    ):
        raise ResearchTransitionConflictError("result expectations are append-only")
    merged_expectations = (*current.result_expectations, *new_expectations)

    incoming_hypotheses = _validated_unique(
        hypotheses, Hypothesis, "hypotheses", "hypothesis_id"
    )
    merged_hypotheses, added_hypotheses, updated_hypotheses = _merge_hypotheses(
        current.hypotheses,
        incoming_hypotheses,
    )
    incoming_bindings = _validated_bindings(bindings)
    _validate_new_predicate_bindings(
        current.query_spec,
        current.bindings,
        incoming_bindings,
    )
    merged_bindings, added_bindings, updated_bindings = _merge_bindings(
        current.bindings,
        incoming_bindings,
    )
    incoming_joins = _validated_unique(
        join_candidates,
        JoinCandidate,
        "join candidates",
        "join_id",
    )
    merged_joins, added_joins, updated_joins = _merge_joins(
        current.join_candidates,
        incoming_joins,
        declared_join_ids=declared_join_ids,
    )

    hypothesis_ids = {item.hypothesis_id for item in merged_hypotheses}
    if (
        next_action.hypothesis_id is not None
        and next_action.hypothesis_id not in hypothesis_ids
    ):
        raise ResearchTransitionReferenceError(
            "action hypothesis_id must exist in the resulting state"
        )
    _validate_references(current, merged_evidence, merged_hypotheses, merged_bindings)
    next_query_spec = _derive_query_spec(
        current.query_spec,
        merged_bindings,
        next_revision,
    )
    unresolved_items = _derive_unresolved_items(next_query_spec, merged_bindings)
    next_budget = (
        current.budget_state
        if budget_state is None
        else _revalidate(budget_state, BudgetState, "budget state")
    )
    _validate_budget_update(current.budget_state, next_budget)
    try:
        next_state = ResearchState(
            run_id=current.run_id,
            run_incarnation=current.run_incarnation,
            revision=next_revision,
            schema_namespace_version=current.schema_namespace_version,
            query_spec=next_query_spec,
            hypotheses=merged_hypotheses,
            evidence=merged_evidence,
            bindings=merged_bindings,
            join_candidates=merged_joins,
            unresolved_items=unresolved_items,
            action_history=(*current.action_history, next_action),
            result_expectations=merged_expectations,
            budget_state=next_budget,
            stop_reason=stop_reason,
        )
    except ValidationError as exc:
        raise ResearchTransitionValidationError(
            "resulting research state violates its contract"
        ) from exc

    return ResearchTransitionResult(
        state=next_state,
        novelty=ResearchNovelty(
            is_novel=bool(
                new_evidence
                or new_expectations
                or added_hypotheses
                or updated_hypotheses
                or added_bindings
                or updated_bindings
                or added_joins
                or updated_joins
                or next_query_spec != current.query_spec
                or stop_reason != current.stop_reason
                or unresolved_items != current.unresolved_items
            ),
            action_id=next_action.action_id,
            action_digest=next_action.action_digest,
            added_evidence_ids=tuple(item.evidence_id for item in new_evidence),
            added_hypothesis_ids=added_hypotheses,
            updated_hypothesis_ids=updated_hypotheses,
            added_binding_ids=added_bindings,
            updated_binding_ids=updated_bindings,
            unresolved_items=unresolved_items,
            stop_reason=stop_reason,
            added_join_ids=added_joins,
            updated_join_ids=updated_joins,
        ),
    )


def _revalidate(value: _Model, model_type: type[_Model], label: str) -> _Model:
    if not isinstance(value, model_type):
        raise ResearchTransitionValidationError(f"{label} has an invalid type")
    try:
        return model_type.model_validate(
            value.model_dump(mode="python", round_trip=True)
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise ResearchTransitionValidationError(
            f"{label} violates its strict contract"
        ) from exc


def _canonical_action_digest(action: ResearchAction) -> str:
    try:
        return canonical_digest_for_action(action)
    except ResearchPolicyError as exc:
        raise ResearchTransitionValidationError(
            "action cannot form canonical identity"
        ) from exc


def _validate_history(state: ResearchState) -> None:
    action_digests: set[str] = set()
    actions_by_digest: dict[str, ResearchAction] = {}
    for historical_action in state.action_history:
        checked = _revalidate(historical_action, ResearchAction, "historical action")
        if checked.action_digest != _canonical_action_digest(checked):
            raise ResearchTransitionValidationError(
                "historical action_digest is not canonical"
            )
        if checked.action_digest in action_digests:
            raise ResearchTransitionConflictError(
                "historical action_digest must be unique"
            )
        action_digests.add(checked.action_digest)
        actions_by_digest[checked.action_digest] = checked
    for record in state.evidence:
        _validate_existing_evidence(state, record, actions_by_digest)


def _validate_budget_update(current: BudgetState, replacement: BudgetState) -> None:
    for field_name in (
        "wall_clock_ms",
        "model_calls",
        "model_tokens",
        "db_probe_ms",
        "rows",
        "bytes",
    ):
        if getattr(replacement, f"initial_{field_name}") != getattr(
            current,
            f"initial_{field_name}",
        ):
            raise ResearchTransitionValidationError(
                "budget initial values are immutable"
            )
        if getattr(replacement, f"used_{field_name}") < getattr(
            current,
            f"used_{field_name}",
        ):
            raise ResearchTransitionValidationError("budget usage cannot be refunded")


def _validated_unique(
    values: Iterable[_Model],
    model_type: type[_Model],
    label: str,
    id_field: str,
) -> tuple[_Model, ...]:
    checked = tuple(_revalidate(value, model_type, label) for value in values)
    identifiers = [getattr(value, id_field) for value in checked]
    if len(identifiers) != len(set(identifiers)):
        raise ResearchTransitionConflictError(f"duplicate {label} identifiers")
    return tuple(sorted(checked, key=lambda item: str(getattr(item, id_field))))


def _validated_bindings(values: Iterable[BindingBase]) -> tuple[BindingBase, ...]:
    checked: list[BindingBase] = []
    for value in values:
        if not isinstance(value, BindingBase):
            raise ResearchTransitionValidationError("binding has an invalid type")
        checked.append(_revalidate(value, type(value), "binding"))
    identifiers = [item.binding_id for item in checked]
    if len(identifiers) != len(set(identifiers)):
        raise ResearchTransitionConflictError("duplicate binding identifiers")
    return tuple(sorted(checked, key=lambda item: item.binding_id))


def _validated_result_expectations(
    values: Iterable[ResultExpectation],
) -> tuple[ResultExpectation, ...]:
    checked = tuple(
        _revalidate(value, ResultExpectation, "result expectation") for value in values
    )
    keys = tuple(_result_expectation_key(item) for item in checked)
    if len(keys) != len(set(keys)):
        raise ResearchTransitionConflictError("duplicate result expectations")
    return tuple(sorted(checked, key=_result_expectation_key))


def _result_expectation_key(expectation: ResultExpectation) -> tuple[object, ...]:
    return (
        expectation.source_id,
        expectation.evidence_id,
        expectation.kind,
        expectation.column.table.namespace,
        expectation.column.table.schema_name,
        expectation.column.table.table,
        expectation.column.column,
    )


def _validate_new_evidence(
    state: ResearchState,
    action: ResearchAction,
    next_revision: int,
    evidence: tuple[EvidenceRecord, ...],
) -> None:
    for record in evidence:
        if (
            record.run_id != state.run_id
            or record.run_incarnation != state.run_incarnation
        ):
            raise ResearchTransitionReferenceError(
                "evidence must belong to the same run incarnation"
            )
        if record.schema_namespace_version != state.schema_namespace_version:
            raise ResearchTransitionReferenceError(
                "evidence schema namespace version must match state"
            )
        if record.revision != next_revision:
            raise ResearchTransitionRevisionError(
                "new evidence revision must equal the resulting state revision"
            )
        if record.action_digest != action.action_digest:
            raise ResearchTransitionReferenceError(
                "new evidence must be tied to the transition action"
            )
        if record.target != action.target:
            raise ResearchTransitionReferenceError(
                "new evidence target must match the transition action"
            )
        if record.source_kind is not evidence_source_kind_for_action(action.kind):
            raise ResearchTransitionReferenceError(
                "new evidence source must match the transition action"
            )


def _validate_existing_evidence(
    state: ResearchState,
    record: EvidenceRecord,
    actions_by_digest: dict[str, ResearchAction],
) -> None:
    if record.run_id != state.run_id or record.run_incarnation != state.run_incarnation:
        raise ResearchTransitionReferenceError(
            "existing evidence must belong to the same run incarnation"
        )
    if record.schema_namespace_version != state.schema_namespace_version:
        raise ResearchTransitionReferenceError(
            "existing evidence schema namespace version must match state"
        )
    producer = actions_by_digest.get(record.action_digest)
    if producer is None:
        raise ResearchTransitionReferenceError(
            "existing evidence action_digest must exist in action history"
        )
    if record.revision != producer.expected_revision + 1:
        raise ResearchTransitionRevisionError(
            "existing evidence revision must follow its producer action"
        )
    if record.target != producer.target:
        raise ResearchTransitionReferenceError(
            "existing evidence target must match its producer action"
        )
    if record.source_kind is not evidence_source_kind_for_action(producer.kind):
        raise ResearchTransitionReferenceError(
            "existing evidence source must match its producer action"
        )


def _merge_hypotheses(
    existing: tuple[Hypothesis, ...],
    incoming: tuple[Hypothesis, ...],
) -> tuple[tuple[Hypothesis, ...], tuple[str, ...], tuple[str, ...]]:
    by_id = {item.hypothesis_id: item for item in existing}
    replacements: dict[str, Hypothesis] = {}
    added: list[str] = []
    updated: list[str] = []
    for item in incoming:
        previous = by_id.get(item.hypothesis_id)
        if previous is None:
            if item.status is not HypothesisStatus.PROPOSED:
                raise ResearchTransitionProtocolError(
                    "new hypothesis must start as proposed"
                )
            replacements[item.hypothesis_id] = item
            added.append(item.hypothesis_id)
            continue
        _validate_status_update(
            previous,
            item,
            "hypothesis_id",
            _HYPOTHESIS_STATUS_TRANSITIONS,
            "hypothesis",
        )
        replacements[item.hypothesis_id] = item
        if item != previous:
            updated.append(item.hypothesis_id)
    merged = tuple(replacements.get(item.hypothesis_id, item) for item in existing)
    merged += tuple(replacements[item_id] for item_id in added)
    return merged, tuple(added), tuple(updated)


def _merge_bindings(
    existing: tuple[BindingBase, ...],
    incoming: tuple[BindingBase, ...],
) -> tuple[tuple[BindingBase, ...], tuple[str, ...], tuple[str, ...]]:
    by_id = {item.binding_id: item for item in existing}
    replacements: dict[str, BindingBase] = {}
    added: list[str] = []
    updated: list[str] = []
    for item in incoming:
        previous = by_id.get(item.binding_id)
        if previous is None:
            if item.status is not BindingStatus.CANDIDATE:
                raise ResearchTransitionProtocolError(
                    "new binding must start as candidate"
                )
            replacements[item.binding_id] = item
            added.append(item.binding_id)
            continue
        if type(previous) is not type(item):
            raise ResearchTransitionConflictError("binding kind is immutable")
        _validate_binding_update(previous, item)
        replacements[item.binding_id] = item
        if item != previous:
            updated.append(item.binding_id)
    merged = tuple(replacements.get(item.binding_id, item) for item in existing)
    merged += tuple(replacements[item_id] for item_id in added)
    return merged, tuple(added), tuple(updated)


def _validate_new_predicate_bindings(
    query_spec: QuerySpec,
    existing: tuple[BindingBase, ...],
    incoming: tuple[BindingBase, ...],
) -> None:
    existing_ids = {binding.binding_id for binding in existing}
    items = {item.source_id: item for item in query_spec.semantic_items}
    for binding in incoming:
        item = items.get(binding.source_id)
        if (
            binding.binding_id in existing_ids
            or not isinstance(binding, DiscriminatorValueBinding)
            or item is None
            or not item.required
            or item.kind not in {SemanticItemKind.FILTER, SemanticItemKind.TIME}
        ):
            continue
        primary_matches_column = (
            bool(binding.predicates)
            and binding.predicates[0] == binding.discriminator_predicate
            and binding.discriminator_predicate.left == binding.discriminator_column
        )
        exact_query_match = False
        if item.exact_physical_predicate and item.operator is not None:
            try:
                required_predicate = PredicateRef(
                    left=binding.discriminator_column,
                    operator=item.operator,
                    right=item.literal_or_reference,
                )
            except (TypeError, ValueError):
                pass
            else:
                exact_query_match = len(binding.predicates) == 1 and predicate_matches(
                    required_predicate,
                    binding.discriminator_predicate,
                )
        filter_matches_query = (
            item.kind is SemanticItemKind.FILTER
            and len(binding.predicates) == 1
            and item.operator is not None
            and binding.discriminator_predicate.operator is item.operator
            and (not item.exact_physical_predicate or exact_query_match)
        )
        time_has_physical_predicates = (
            item.kind is SemanticItemKind.TIME
            and item.operator is not None
            and bool(binding.predicates)
            and (not item.exact_physical_predicate or exact_query_match)
        )
        if not primary_matches_column or not (
            filter_matches_query or time_has_physical_predicates
        ):
            raise ResearchTransitionProtocolError(
                "new discriminator binding must match the required operator and column"
            )


def _validate_binding_update(previous: BindingBase, replacement: BindingBase) -> None:
    """Allow only the reducer's candidate-to-supported certificate annotation."""

    allowed_changed = {"status", "evidence_ids"}
    semantic_promotion = (
        previous.status is BindingStatus.CANDIDATE
        and replacement.status is BindingStatus.SUPPORTED
        and previous.confidence == 0.0
        and replacement.confidence == 1.0
        and previous.validator_rule is None
        and replacement.validator_rule is not None
        and replacement.validator_rule.startswith("semantic-certificate:v1:")
    )
    if semantic_promotion:
        allowed_changed.update({"confidence", "validator_rule"})
    immutable_previous = previous.model_dump(mode="python", exclude=allowed_changed)
    immutable_replacement = replacement.model_dump(
        mode="python", exclude=allowed_changed
    )
    if immutable_previous != immutable_replacement:
        raise ResearchTransitionConflictError("binding immutable fields cannot change")
    if replacement.evidence_ids[: len(previous.evidence_ids)] != previous.evidence_ids:
        raise ResearchTransitionConflictError("binding evidence_ids are append-only")
    if (
        replacement.status is not previous.status
        and replacement.status not in _BINDING_STATUS_TRANSITIONS[previous.status]
    ):
        raise ResearchTransitionProtocolError("invalid binding status transition")
    if previous.binding_id != replacement.binding_id:
        raise ResearchTransitionConflictError("binding identifier is immutable")


def _merge_joins(
    existing: tuple[JoinCandidate, ...],
    incoming: tuple[JoinCandidate, ...],
    *,
    declared_join_ids: tuple[str, ...] = (),
) -> tuple[tuple[JoinCandidate, ...], tuple[str, ...], tuple[str, ...]]:
    """Merge append-only join candidates without changing their physical shape."""

    by_id = {item.join_id: item for item in existing}
    replacements: dict[str, JoinCandidate] = {}
    added: list[str] = []
    updated: list[str] = []
    for item in incoming:
        previous = by_id.get(item.join_id)
        if previous is None:
            if item.status is JoinCandidateStatus.VALIDATED:
                if item.join_id not in declared_join_ids:
                    raise ResearchTransitionProtocolError(
                        "new validated join requires a trusted declaration"
                    )
            elif item.status is not JoinCandidateStatus.CANDIDATE:
                raise ResearchTransitionProtocolError(
                    "new join must start as candidate"
                )
            replacements[item.join_id] = item
            added.append(item.join_id)
            continue
        immutable_previous = previous.model_dump(
            mode="python", exclude={"status", "evidence_ids"}
        )
        immutable_replacement = item.model_dump(
            mode="python", exclude={"status", "evidence_ids"}
        )
        if immutable_previous != immutable_replacement:
            raise ResearchTransitionConflictError("join immutable fields cannot change")
        if item.evidence_ids[: len(previous.evidence_ids)] != previous.evidence_ids:
            raise ResearchTransitionConflictError("join evidence_ids are append-only")
        allowed = {
            JoinCandidateStatus.CANDIDATE: frozenset(
                {JoinCandidateStatus.VALIDATED, JoinCandidateStatus.REJECTED}
            ),
            JoinCandidateStatus.VALIDATED: frozenset(),
            JoinCandidateStatus.REJECTED: frozenset(),
        }
        if (
            item.status is not previous.status
            and item.status not in allowed[previous.status]
        ):
            raise ResearchTransitionProtocolError("invalid join status transition")
        replacements[item.join_id] = item
        if item != previous:
            updated.append(item.join_id)
    merged = tuple(replacements.get(item.join_id, item) for item in existing)
    merged += tuple(replacements[item_id] for item_id in added)
    return merged, tuple(added), tuple(updated)


def _validate_status_update(
    previous: BaseModel,
    replacement: BaseModel,
    id_field: str,
    transitions: dict[object, frozenset[object]],
    label: str,
) -> None:
    immutable_previous = previous.model_dump(
        mode="python", exclude={"status", "evidence_ids"}
    )
    immutable_replacement = replacement.model_dump(
        mode="python", exclude={"status", "evidence_ids"}
    )
    if immutable_previous != immutable_replacement:
        raise ResearchTransitionConflictError(f"{label} immutable fields cannot change")
    prefix_length = len(previous.evidence_ids)
    if replacement.evidence_ids[:prefix_length] != previous.evidence_ids:
        raise ResearchTransitionConflictError(f"{label} evidence_ids are append-only")
    previous_status = getattr(previous, "status")
    replacement_status = getattr(replacement, "status")
    if (
        replacement_status != previous_status
        and replacement_status not in transitions[previous_status]
    ):
        raise ResearchTransitionProtocolError(f"invalid {label} status transition")
    if getattr(previous, id_field) != getattr(replacement, id_field):
        raise ResearchTransitionConflictError(f"{label} identifier is immutable")


def _validate_references(
    state: ResearchState,
    evidence: tuple[EvidenceRecord, ...],
    hypotheses: tuple[Hypothesis, ...],
    bindings: tuple[BindingBase, ...],
) -> None:
    evidence_ids = {item.evidence_id for item in evidence}
    source_ids = {item.source_id for item in state.query_spec.semantic_items}
    for hypothesis in hypotheses:
        if not set(hypothesis.source_ids).issubset(source_ids):
            raise ResearchTransitionReferenceError(
                "hypothesis source_ids must exist in query spec"
            )
        if not set(hypothesis.evidence_ids).issubset(evidence_ids):
            raise ResearchTransitionReferenceError(
                "hypothesis evidence_ids must exist in state"
            )
    for binding in bindings:
        if binding.source_id not in source_ids:
            raise ResearchTransitionReferenceError(
                "binding source_id must exist in query spec"
            )
        if not set(binding.evidence_ids).issubset(evidence_ids):
            raise ResearchTransitionReferenceError(
                "binding evidence_ids must exist in state"
            )


def _derive_query_spec(
    query_spec: QuerySpec,
    bindings: tuple[BindingBase, ...],
    next_revision: int,
) -> QuerySpec:
    bindings_by_source: dict[str, list[BindingBase]] = {}
    for binding in bindings:
        bindings_by_source.setdefault(binding.source_id, []).append(binding)

    semantic_items = tuple(
        _derive_semantic_item(item, bindings_by_source.get(item.source_id, ()))
        for item in query_spec.semantic_items
    )
    if semantic_items == query_spec.semantic_items:
        return query_spec
    return query_spec.model_copy(
        update={"revision": next_revision, "semantic_items": semantic_items}
    )


def _derive_semantic_item(
    item: SemanticItem,
    bindings: Iterable[BindingBase],
) -> SemanticItem:
    bindings = tuple(bindings)
    if is_structurally_resolved_limit(item) and not bindings:
        return item.model_copy(
            update={"status": SemanticItemStatus.RESOLVED, "binding_ids": ()}
        )
    status, binding_ids = derive_semantic_resolution(item.status.value, bindings)
    return item.model_copy(
        update={"status": SemanticItemStatus(status), "binding_ids": binding_ids}
    )


def _derive_unresolved_items(
    query_spec: QuerySpec,
    bindings: tuple[BindingBase, ...],
) -> tuple[str, ...]:
    supported_by_source = {
        source_id: sum(
            binding.status is BindingStatus.SUPPORTED
            for binding in bindings
            if binding.source_id == source_id
        )
        for source_id in (item.source_id for item in query_spec.semantic_items)
    }
    return tuple(
        sorted(
            item.source_id
            for item in query_spec.semantic_items
            if (
                item.required
                and not is_binding_free_semantic_item(item, bindings)
                and supported_by_source[item.source_id] == 0
            )
        )
    )
