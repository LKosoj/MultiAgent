"""Canonical authorization footprints for semantic coverage readiness."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC
from typing import TypeVar

from pydantic import ValidationError

from .freshness import FreshnessContext
from .models import (
    Binding,
    BindingStatus,
    ColumnRef,
    DerivedExpressionBinding,
    DiscriminatorValueBinding,
    DocumentRuleBinding,
    Id,
    JoinCandidate,
    JoinCandidateStatus,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    PredicateOperator,
    PredicateRef,
    ResearchState,
    StrictModel,
    TableRef,
    VerticalAttributeBinding,
)
from .serialization import canonical_digest, canonical_json_bytes


_CanonicalValue = TypeVar("_CanonicalValue")

BINDING_TYPES = (
    PhysicalColumnBinding,
    VerticalAttributeBinding,
    DiscriminatorValueBinding,
    DerivedExpressionBinding,
    DocumentRuleBinding,
)


class FootprintError(ValueError):
    """A binding, join, or derived footprint is contradictory."""


class CoverageFootprint(StrictModel):
    """Everything later coverage evaluation may use."""

    eligible_validated_joins: tuple[JoinCandidate, ...]
    eligible_evidence_ids: tuple[Id, ...]
    allowed_tables: tuple[TableRef, ...]
    allowed_columns: tuple[ColumnRef, ...]
    allowed_predicates: tuple[PredicateRef, ...]
    allowed_join_paths: tuple[tuple[JoinEdge, ...], ...]


def disconnected_binding_source_ids(
    selected_bindings: tuple[Binding, ...],
    eligible_validated_joins: tuple[JoinCandidate, ...],
) -> tuple[str, ...]:
    """Return required binding sources whose tables lack one join component."""

    bindings = tuple(canonical_binding(item) for item in selected_bindings)
    if any(item.status is not BindingStatus.SUPPORTED for item in bindings):
        raise FootprintError("selected bindings must be supported")
    required_tables = {
        canonical_json_bytes(table) for binding in bindings for table in binding.tables
    }
    if len(required_tables) < 2:
        return ()

    adjacency: dict[bytes, set[bytes]] = {table: set() for table in required_tables}
    for raw_join in eligible_validated_joins:
        join = canonical_join(raw_join)
        if join.status is not JoinCandidateStatus.VALIDATED:
            raise FootprintError("eligible joins must be validated")
        for edge in join.path:
            left = canonical_json_bytes(edge.left.table)
            right = canonical_json_bytes(edge.right.table)
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)

    root = min(required_tables)
    seen = {root}
    pending = [root]
    while pending:
        table = pending.pop()
        for neighbor in adjacency[table]:
            if neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
    if required_tables.issubset(seen):
        return ()
    return tuple(sorted(binding.source_id for binding in bindings))


def derive_coverage_footprint(
    selected_bindings: tuple[Binding, ...],
    eligible_validated_joins: tuple[JoinCandidate, ...],
) -> CoverageFootprint:
    """Derive the only footprint authorized by selected bindings and joins."""

    bindings = tuple(canonical_binding(item) for item in selected_bindings)
    if any(item.status is not BindingStatus.SUPPORTED for item in bindings):
        raise FootprintError("selected bindings must be supported")

    joins_by_id: dict[str, JoinCandidate] = {}
    for raw_join in eligible_validated_joins:
        join = canonical_join(raw_join)
        if join.status is not JoinCandidateStatus.VALIDATED:
            raise FootprintError("eligible joins must be validated")
        existing = joins_by_id.get(join.join_id)
        if existing is not None and existing != join:
            raise FootprintError("eligible join IDs must identify one join")
        joins_by_id[join.join_id] = join
    joins = tuple(joins_by_id[key] for key in sorted(joins_by_id))

    used_join_ids: set[str] = set()
    for binding in bindings:
        if not binding.join_path:
            continue
        matching = tuple(
            (join, offsets)
            for join in joins
            if (offsets := matching_join_offsets(join, binding.join_path))
        )
        if len(covered_join_positions(matching)) != len(binding.join_path):
            raise FootprintError("eligible joins do not cover a selected join path")
        used_join_ids.update(join.join_id for join, _ in matching)
    return CoverageFootprint(
        eligible_validated_joins=joins,
        eligible_evidence_ids=tuple(
            sorted(
                {
                    evidence_id
                    for binding in bindings
                    for evidence_id in binding.evidence_ids
                }
                | {evidence_id for join in joins for evidence_id in join.evidence_ids}
            )
        ),
        allowed_tables=canonical_unique(
            table for binding in bindings for table in binding.tables
        ),
        allowed_columns=canonical_unique(
            column for binding in bindings for column in binding.columns
        ),
        allowed_predicates=canonical_unique(
            predicate for binding in bindings for predicate in binding.predicates
        ),
        allowed_join_paths=canonical_unique(
            (
                *(binding.join_path for binding in bindings if binding.join_path),
                *(join.path for join in joins if join.join_id not in used_join_ids),
            )
        ),
    )


def canonical_unique(
    values: Iterable[_CanonicalValue],
) -> tuple[_CanonicalValue, ...]:
    by_identity = {canonical_json_bytes(value): value for value in values}
    return tuple(by_identity[key] for key in sorted(by_identity))


def model_payload(value: StrictModel) -> dict[str, object]:
    return value.model_dump(
        mode="python",
        by_alias=True,
        exclude_none=False,
        round_trip=True,
        warnings="error",
    )


def canonical_predicate(predicate: PredicateRef) -> PredicateRef:
    if predicate.operator not in {PredicateOperator.IN, PredicateOperator.NOT_IN}:
        return predicate
    payload = model_payload(predicate)
    if isinstance(predicate.right, tuple):
        payload["right"] = canonical_unique(predicate.right)
    return PredicateRef.model_validate(payload)


def canonical_binding(binding: Binding) -> Binding:
    if type(binding) not in BINDING_TYPES:
        raise FootprintError("binding has an unsupported exact type")
    try:
        checked = type(binding).model_validate(model_payload(binding))
    except (ValidationError, ValueError, TypeError) as exc:
        raise FootprintError("binding is not a strict subtype") from exc

    expected_tables, expected_columns, expected_predicates = binding_footprint(checked)
    if type(checked) is DerivedExpressionBinding:
        columns_match = canonical_unique(checked.columns) == expected_columns
    else:
        columns_match = checked.columns == expected_columns
    if (
        checked.tables != expected_tables
        or not columns_match
        or checked.predicates != expected_predicates
    ):
        raise FootprintError("binding base footprint contradicts subtype fields")

    canonical_predicates = tuple(
        canonical_predicate(predicate) for predicate in expected_predicates
    )
    payload = model_payload(checked)
    payload.update(
        tables=expected_tables,
        columns=expected_columns,
        predicates=canonical_predicates,
        evidence_ids=tuple(sorted(checked.evidence_ids)),
    )
    if type(checked) is VerticalAttributeBinding:
        payload["attribute_name_predicate"] = canonical_predicates[0]
        payload["value_predicate"] = canonical_predicates[1]
    elif type(checked) is DiscriminatorValueBinding:
        payload["discriminator_predicate"] = canonical_predicates[0]
    return type(checked).model_validate(payload)


def binding_footprint(
    binding: Binding,
) -> tuple[tuple[TableRef, ...], tuple[ColumnRef, ...], tuple[PredicateRef, ...]]:
    if type(binding) is PhysicalColumnBinding:
        return (binding.physical_column.table,), (binding.physical_column,), ()
    if type(binding) is VerticalAttributeBinding:
        name_left = binding.attribute_name_predicate.left
        value_left = binding.value_predicate.left
        if (
            binding.entity_key.table != binding.entity_table
            or binding.attribute_catalog_key.table != binding.attribute_catalog_table
            or type(name_left) is not ColumnRef
            or name_left.table != binding.attribute_catalog_table
            or binding.value_entity_key.table != binding.value_table
            or binding.value_attribute_key.table != binding.value_table
            or type(value_left) is not ColumnRef
            or value_left.table != binding.value_table
        ):
            raise FootprintError("vertical binding fields use inconsistent tables")
        return (
            (
                binding.entity_table,
                binding.attribute_catalog_table,
                binding.value_table,
            ),
            (
                binding.entity_key,
                binding.attribute_catalog_key,
                name_left,
                binding.value_entity_key,
                binding.value_attribute_key,
                value_left,
            ),
            (binding.attribute_name_predicate, binding.value_predicate),
        )
    if type(binding) is DiscriminatorValueBinding:
        if (
            not binding.predicates
            or binding.predicates[0] != binding.discriminator_predicate
            or binding.discriminator_predicate.left != binding.discriminator_column
        ):
            raise FootprintError("discriminator predicate uses another column")
        predicates = (
            binding.discriminator_predicate,
            *canonical_unique(binding.predicates[1:]),
        )
        columns = tuple(dict.fromkeys(predicate.left for predicate in predicates))
        tables = tuple(dict.fromkeys(column.table for column in columns))
        return (
            tables,
            columns,
            predicates,
        )
    if type(binding) is DerivedExpressionBinding:
        input_columns = binding.input_columns
        canonical_input_columns = canonical_unique(input_columns)
        if len(input_columns) != len(canonical_input_columns):
            raise FootprintError("derived expression input columns must be unique")
        return (
            canonical_unique(column.table for column in canonical_input_columns),
            canonical_input_columns,
            (),
        )
    if type(binding) is DocumentRuleBinding:
        return (), (), ()
    raise FootprintError("binding has an unsupported exact type")


def canonical_join(candidate: JoinCandidate) -> JoinCandidate:
    if type(candidate) is not JoinCandidate:
        raise FootprintError("join has an unsupported exact type")
    try:
        checked = JoinCandidate.model_validate(model_payload(candidate))
    except (ValidationError, ValueError, TypeError) as exc:
        raise FootprintError("join is not strict") from exc
    if not checked.path or any(type(edge) is not JoinEdge for edge in checked.path):
        raise FootprintError("join path must contain typed edges")
    if any(edge.join_type is not checked.join_type for edge in checked.path):
        raise FootprintError("join type contradicts its path")

    first = checked.path[0]
    same_table_pair = all(
        {edge.left.table, edge.right.table} == {first.left.table, first.right.table}
        for edge in checked.path
    )
    if same_table_pair:
        if any(
            edge.left.table != first.left.table or edge.right.table != first.right.table
            for edge in checked.path
        ):
            raise FootprintError("composite join orientation is inconsistent")
        expected_left, expected_right = first.left, first.right
    else:
        if any(
            previous.right.table != following.left.table
            for previous, following in zip(
                checked.path,
                checked.path[1:],
                strict=False,
            )
        ):
            raise FootprintError("multi-hop join path is disconnected")
        expected_left, expected_right = first.left, checked.path[-1].right
    if checked.left != expected_left or checked.right != expected_right:
        raise FootprintError("join endpoints contradict its path")
    return JoinCandidate.model_validate(
        {
            **model_payload(checked),
            "evidence_ids": tuple(sorted(checked.evidence_ids)),
        }
    )


def matching_join_offsets(
    candidate: JoinCandidate,
    required_path: tuple[JoinEdge, ...],
) -> tuple[int, ...]:
    if not candidate.path or len(candidate.path) > len(required_path):
        return ()
    width = len(candidate.path)
    return tuple(
        start
        for start in range(len(required_path) - width + 1)
        if all(
            _join_edges_match(required, actual)
            for required, actual in zip(
                required_path[start : start + width],
                candidate.path,
                strict=True,
            )
        )
    )


def covered_join_positions(
    candidates: tuple[tuple[JoinCandidate, tuple[int, ...]], ...],
) -> frozenset[int]:
    return frozenset(
        position
        for candidate, offsets in candidates
        for offset in offsets
        for position in range(offset, offset + len(candidate.path))
    )


def _join_edges_match(required: JoinEdge, actual: JoinEdge) -> bool:
    if (
        required.operator is not actual.operator
        or required.join_type is not actual.join_type
    ):
        return False
    if required.join_type is JoinType.LEFT:
        return required.left == actual.left and required.right == actual.right
    return (required.left == actual.left and required.right == actual.right) or (
        required.left == actual.right and required.right == actual.left
    )


def normalized_freshness_digest(context: FreshnessContext) -> str:
    payload = model_payload(context)
    payload["evaluated_at"] = context.evaluated_at.astimezone(UTC)
    payload["document_sources"] = tuple(
        sorted(
            payload["document_sources"],
            key=lambda item: item["document_id"],
        )
    )
    payload["data_snapshots"] = tuple(
        sorted(payload["data_snapshots"], key=lambda item: item["token"])
    )
    return canonical_digest(payload)


def requirements_digest(requirements: StrictModel) -> str:
    return canonical_digest(
        requirements.model_dump(
            mode="python",
            by_alias=True,
            exclude={"requirements_digest"},
            exclude_none=False,
            round_trip=True,
            warnings="error",
        )
    )


def normalized_state_digest(state: ResearchState) -> str:
    payload = model_payload(state)
    query = payload["query_spec"]
    query["global_constraints"] = tuple(
        model_payload(predicate)
        for predicate in canonical_unique(
            canonical_predicate(predicate)
            for predicate in state.query_spec.global_constraints
        )
    )
    query["semantic_items"] = tuple(
        sorted(
            (
                _normalized_semantic_item_payload(item)
                for item in query["semantic_items"]
            ),
            key=lambda item: item["source_id"],
        )
    )
    payload["hypotheses"] = tuple(
        sorted(
            (
                {
                    **item,
                    "source_ids": tuple(sorted(set(item["source_ids"]))),
                    "candidate_targets": canonical_unique(item["candidate_targets"]),
                    "evidence_ids": tuple(sorted(set(item["evidence_ids"]))),
                }
                for item in payload["hypotheses"]
            ),
            key=lambda item: item["hypothesis_id"],
        )
    )
    payload["evidence"] = tuple(
        sorted(payload["evidence"], key=lambda item: item["evidence_id"])
    )
    payload["bindings"] = tuple(
        sorted(
            (model_payload(canonical_binding(item)) for item in state.bindings),
            key=lambda item: item["binding_id"],
        )
    )
    payload["join_candidates"] = tuple(
        sorted(
            (model_payload(canonical_join(item)) for item in state.join_candidates),
            key=lambda item: item["join_id"],
        )
    )
    payload["unresolved_items"] = tuple(sorted(set(payload["unresolved_items"])))
    payload["action_history"] = tuple(
        sorted(
            (
                {
                    **item,
                    "parameters": tuple(
                        sorted(item["parameters"], key=lambda pair: pair[0])
                    ),
                }
                for item in payload["action_history"]
            ),
            key=lambda item: (item["expected_revision"], item["action_id"]),
        )
    )
    return canonical_digest(payload)


def _normalized_semantic_item_payload(item: dict[str, object]) -> dict[str, object]:
    normalized = {
        **item,
        "binding_ids": tuple(sorted(set(item["binding_ids"]))),
    }
    if item["operator"] in {
        PredicateOperator.IN,
        PredicateOperator.NOT_IN,
    } and isinstance(item["literal_or_reference"], tuple):
        normalized["literal_or_reference"] = canonical_unique(
            item["literal_or_reference"]
        )
    return normalized


__all__ = [
    "BINDING_TYPES",
    "CoverageFootprint",
    "FootprintError",
    "canonical_binding",
    "canonical_join",
    "canonical_unique",
    "covered_join_positions",
    "derive_coverage_footprint",
    "matching_join_offsets",
    "model_payload",
    "normalized_freshness_digest",
    "normalized_state_digest",
    "requirements_digest",
]
