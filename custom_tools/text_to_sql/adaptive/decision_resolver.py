"""Trusted resolution and one-shot execution of one parsed research decision."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import inspect
import math
from threading import Lock
from typing import cast

from pydantic import ValidationError

from workflow.deadline import DeadlineBudget

from ..schema_loader import LoadedSchema
from ..schema_metadata import get_foreign_key_constraints
from ..utils import get_table_columns
from ._decision_resolver_validation import (
    ModelDecisionReferenceError,
    ResolutionInputError,
    validate_resolution_inputs,
)
from .controller import (
    AdaptiveLoopActionError,
    AdaptiveLoopKind,
    JsonValue,
    NormalizedToolResult,
    ToolCall,
    ToolInvocation,
    parse_model_action,
)
from ._decision_resolver_seal import (
    TrustedExecutionSeal,
    capture_trusted_execution_seal,
    deadline_identity_payload,
    trusted_execution_digest,
    validate_trusted_execution_seal,
)
from .freshness import FreshnessContext
from .models import (
    ColumnRef,
    DerivedExpressionBinding,
    DocumentRef,
    DiscriminatorValueBinding,
    JoinCandidate,
    JoinEdge,
    JoinType,
    PhysicalColumnBinding,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    StrictModel,
    TableRef,
    VerticalAttributeBinding,
)
from .probes import ProbeResult, ProbeStatus
from .research_decision import (
    BindingAssessment,
    DerivedExpressionCandidate,
    DocumentRuleCandidate,
    ExistingBindingRef,
    ExistingJoinRef,
    JoinAssessment,
    LogicalColumnRef,
    LogicalColumnTarget,
    LogicalDocumentTarget,
    LogicalTableRef,
    LogicalTableTarget,
    NewBindingProposal,
    NewJoinProposal,
    ResearchDecisionV1,
    ToolIntent,
)
from .semantic_reducer import (
    ResolvedColumn,
    ResolvedTable,
    SemanticReducerError,
    SemanticTurnAdmission,
    TrustedSemanticBatch,
    TrustedToolClaim,
    _Resolver as _SemanticResolver,
    _new_binding,
    _new_join,
    admit_semantic_turn,
)
from .serialization import canonical_digest, canonical_json_bytes
from .tool_registry import AdaptiveResearchToolRegistry, resolve_research_tool_claim


class DecisionResolverError(ValueError):
    """A parsed decision cannot be trusted in the supplied current context."""


class UnresolvableModelDecisionError(DecisionResolverError):
    """The model supplied a reference or semantic claim that cannot be resolved."""

    def __init__(
        self,
        message: str,
        *,
        exact_column: ColumnRef | None = None,
    ) -> None:
        self.exact_column = exact_column
        super().__init__(message)


class DuplicateExistingBindingProposalError(UnresolvableModelDecisionError):
    """A new-binding proposal resolves to an already durable binding."""

    def __init__(
        self, existing_binding_ids_by_proposal_key: tuple[tuple[str, str], ...]
    ) -> None:
        self.existing_binding_ids_by_proposal_key = (
            existing_binding_ids_by_proposal_key
        )
        super().__init__("new binding already exists")


class DuplicateResearchActionError(DecisionResolverError):
    """The proposal repeats an action already present in immutable history."""

    def __init__(self, action: ResearchAction) -> None:
        self.action = action
        super().__init__("resolved action duplicates prior semantics")


class DecisionExecutionError(RuntimeError):
    """The one admitted adapter call did not return its exact typed result."""


class DecisionAlreadyExecutedError(DecisionExecutionError):
    """A resolved tool decision has already consumed its one execution chance."""


class _ExecutionGate:
    def __init__(self) -> None:
        self._lock = Lock()
        self._consumed = False

    def consume(self) -> None:
        with self._lock:
            if self._consumed:
                raise DecisionAlreadyExecutedError(
                    "resolved tool decision cannot be executed or recovered twice"
                )
            self._consumed = True


@dataclass(frozen=True, slots=True)
class ResolvedResearchDecision:
    """Sealed pure admission for an inert stop or one exact tool invocation."""

    decision: ResearchDecisionV1
    decision_digest: str
    state_digest: str
    semantic_batch: TrustedSemanticBatch
    tool_claim: TrustedToolClaim | None
    admission: SemanticTurnAdmission
    invocation: ToolInvocation | None
    resolution_digest: str
    registry: AdaptiveResearchToolRegistry = field(repr=False, compare=False)
    _trusted_execution_seal: TrustedExecutionSeal = field(repr=False, compare=False)
    _trusted_execution_digest: str = field(repr=False, compare=False)
    _execution_gate: _ExecutionGate = field(
        default_factory=_ExecutionGate,
        repr=False,
        compare=False,
    )

    @property
    def is_stop(self) -> bool:
        return self.invocation is None


def _validate_trusted_execution_seal(
    resolved: ResolvedResearchDecision,
    registry: AdaptiveResearchToolRegistry,
) -> None:
    try:
        validate_trusted_execution_seal(
            registry,
            resolved._trusted_execution_seal,
            resolved._trusted_execution_digest,
        )
    except Exception as exc:
        raise DecisionExecutionError(
            "trusted adaptive runtime identity was changed"
        ) from exc


def _capture_resolution_execution_seal(
    registry: AdaptiveResearchToolRegistry,
) -> tuple[TrustedExecutionSeal, str]:
    try:
        seal = capture_trusted_execution_seal(registry)
        return seal, trusted_execution_digest(seal)
    except Exception as exc:
        raise DecisionResolverError("trusted adaptive runtime is invalid") from exc


class _ScopedResolver:
    def __init__(
        self,
        loaded_schema: LoadedSchema,
        *,
        table_namespace: str,
        documents: tuple[object, ...],
        schema_namespace_version: str,
    ) -> None:
        self._schema = loaded_schema.schema
        self._table_namespace = table_namespace
        self._schema_version = schema_namespace_version
        self._table_names = self._validated_table_names()
        self._documents = self._validated_documents(documents)
        self._resolved_tables: dict[str, TableRef] = {}
        self._resolved_columns: dict[tuple[str, str], ColumnRef] = {}
        self._resolved_documents: dict[str, DocumentRef] = {}
        self._schema_columns: list[ColumnRef] = []
        self._declared_join_ids: set[str] = set()

    def _validated_table_names(self) -> tuple[str, ...]:
        if not isinstance(self._schema, Mapping):
            raise DecisionResolverError("loaded scoped schema must be a mapping")
        names: list[str] = []
        for name in self._schema:
            if (
                type(name) is not str
                or not name
                or name.startswith(".")
                or name.endswith(".")
            ):
                raise DecisionResolverError(
                    "scoped schema contains an invalid table name"
                )
            names.append(name)
        if len(names) != len(set(names)):
            raise DecisionResolverError("scoped schema table names must be unique")
        return tuple(sorted(names))

    def _validated_documents(
        self, documents: tuple[object, ...]
    ) -> dict[str, DocumentRef]:
        if type(documents) is not tuple:
            raise DecisionResolverError("trusted schema documents must be a tuple")
        resolved: dict[str, DocumentRef] = {}
        for document in documents:
            document_id = getattr(document, "document_id", None)
            namespace = getattr(document, "namespace", None)
            version = getattr(document, "schema_namespace_version", None)
            if (
                type(document_id) is not str
                or type(namespace) is not str
                or version != self._schema_version
                or namespace != self._table_namespace
            ):
                raise DecisionResolverError(
                    "trusted schema document has the wrong namespace or version"
                )
            if document_id in resolved:
                raise DecisionResolverError("trusted schema document IDs are ambiguous")
            resolved[document_id] = DocumentRef(
                document_id=document_id,
                namespace=namespace,
            )
        return resolved

    def table(self, logical_table: str) -> TableRef:
        cached = self._resolved_tables.get(logical_table)
        if cached is not None:
            return cached
        if type(logical_table) is not str or not logical_table:
            raise UnresolvableModelDecisionError(
                "logical table must be exact non-empty text"
            )
        qualifier, separator, table_name = logical_table.rpartition(".")
        if separator:
            if not qualifier or not table_name:
                raise UnresolvableModelDecisionError(
                    "qualified table reference is invalid"
                )
            exact = [name for name in self._table_names if name == logical_table]
            folded = [
                name
                for name in self._table_names
                if name.casefold() == logical_table.casefold()
            ]
        else:
            exact = [
                name
                for name in self._table_names
                if name.rsplit(".", 1)[-1] == logical_table
            ]
            folded = [
                name
                for name in self._table_names
                if name.rsplit(".", 1)[-1].casefold() == logical_table.casefold()
            ]
        if len(exact) != 1 or len(folded) != 1:
            raise UnresolvableModelDecisionError(
                "logical table is missing, ambiguous, or differs by case"
            )
        physical_name = exact[0]
        schema_name, has_schema, physical_table = physical_name.rpartition(".")
        resolved = TableRef(
            namespace=self._table_namespace,
            schema=schema_name if has_schema else None,
            table=physical_table if has_schema else physical_name,
        )
        self._resolved_tables[logical_table] = resolved
        return resolved

    def column(
        self,
        logical_table: str | LogicalColumnRef,
        logical_column: str | None = None,
    ) -> ColumnRef:
        if isinstance(logical_table, LogicalColumnRef):
            logical_table, logical_column = logical_table.table, logical_table.column
        if logical_column is None:
            raise UnresolvableModelDecisionError(
                "logical column must be exact non-empty text"
            )
        key = (logical_table, logical_column)
        cached = self._resolved_columns.get(key)
        if cached is not None:
            return cached
        table = self.table(logical_table)
        table_name = _physical_table_name(table)
        table_body = self._schema.get(table_name)
        if not isinstance(table_body, Mapping):
            raise DecisionResolverError("resolved table metadata is invalid")
        try:
            columns = get_table_columns(table_body)
        except (TypeError, ValueError) as exc:
            raise DecisionResolverError("resolved table columns are invalid") from exc
        if not isinstance(columns, Mapping):
            raise DecisionResolverError("resolved table columns are invalid")
        exact = [name for name in columns if name == logical_column]
        folded = [
            name
            for name in columns
            if type(name) is str and name.casefold() == logical_column.casefold()
        ]
        if len(exact) != 1 or len(folded) != 1:
            raise UnresolvableModelDecisionError(
                "logical column is missing, ambiguous, or differs by case",
                exact_column=(
                    ColumnRef(table=table, column=folded[0])
                    if not exact and len(folded) == 1
                    else None
                ),
            )
        resolved = ColumnRef(table=table, column=exact[0])
        self._resolved_columns[key] = resolved
        self._record_schema_column(resolved)
        return resolved

    def _record_schema_column(self, column: ColumnRef) -> None:
        if column not in self._schema_columns:
            self._schema_columns.append(column)

    def record_existing_physical_column(self, column: ColumnRef) -> None:
        if column.table.namespace != self._table_namespace:
            return
        table_body = self._schema.get(_physical_table_name(column.table))
        if not isinstance(table_body, Mapping):
            return
        try:
            columns = get_table_columns(table_body)
        except (TypeError, ValueError) as exc:
            raise DecisionResolverError("resolved table columns are invalid") from exc
        if column.column in columns:
            self._record_schema_column(column)

    def record_declared_join(self, join: JoinCandidate) -> None:
        for source_name in self._table_names:
            source_body = self._schema[source_name]
            if not isinstance(source_body, Mapping):
                raise DecisionResolverError("resolved table metadata is invalid")
            for constraint in get_foreign_key_constraints(source_name, self._schema):
                target_name = constraint["to_table"]
                pairs = constraint["column_pairs"]
                if not isinstance(target_name, str) or not isinstance(pairs, list):
                    raise DecisionResolverError("resolved foreign key metadata is invalid")
                source = self._physical_table_ref(source_name)
                target = self._physical_table_ref(target_name)
                forward = tuple(
                    JoinEdge(
                        left=ColumnRef(table=source, column=pair["from_column"]),
                        right=ColumnRef(table=target, column=pair["to_column"]),
                        join_type=join.join_type,
                    )
                    for pair in pairs
                    if isinstance(pair, Mapping)
                    and type(pair.get("from_column")) is str
                    and type(pair.get("to_column")) is str
                )
                if len(forward) != len(pairs):
                    raise DecisionResolverError("resolved foreign key metadata is invalid")
                reverse = tuple(
                    JoinEdge(
                        left=edge.right,
                        right=edge.left,
                        join_type=JoinType.INNER,
                    )
                    for edge in forward
                )
                if (
                    join.left == forward[0].left
                    and join.right == forward[0].right
                    and join.path == forward
                ) or (
                    join.join_type is JoinType.INNER
                    and join.left == reverse[0].left
                    and join.right == reverse[0].right
                    and join.path == reverse
                ):
                    self._declared_join_ids.add(join.join_id)
                    return

    def _physical_table_ref(self, name: str) -> TableRef:
        schema_name, has_schema, table_name = name.rpartition(".")
        return TableRef(
            namespace=self._table_namespace,
            schema=schema_name if has_schema else None,
            table=table_name if has_schema else name,
        )

    def document(self, document_id: str) -> DocumentRef:
        try:
            resolved = self._documents[document_id]
        except KeyError as exc:
            raise UnresolvableModelDecisionError(
                "schema document is not uniquely available in this runtime"
            ) from exc
        self._resolved_documents[document_id] = resolved
        return resolved

    def batch(self) -> TrustedSemanticBatch:
        return TrustedSemanticBatch(
            schema_namespace_version=self._schema_version,
            tables=tuple(
                ResolvedTable(logical_table=logical, physical_table=physical)
                for logical, physical in sorted(self._resolved_tables.items())
            ),
            columns=tuple(
                ResolvedColumn(
                    logical_table=logical_table,
                    logical_column=logical_column,
                    physical_column=physical,
                )
                for (logical_table, logical_column), physical in sorted(
                    self._resolved_columns.items()
                )
            ),
            documents=tuple(
                document for _, document in sorted(self._resolved_documents.items())
            ),
            schema_columns=tuple(self._schema_columns),
            declared_join_ids=tuple(sorted(self._declared_join_ids)),
        )


def resolve_research_decision(
    state: ResearchState,
    decision: ResearchDecisionV1,
    *,
    loaded_schema: LoadedSchema,
    freshness_context: FreshnessContext,
    registry: AdaptiveResearchToolRegistry,
    deadline: DeadlineBudget | None = None,
) -> ResolvedResearchDecision:
    """Resolve and seal one parsed decision without executing or mutating state."""

    try:
        current, parsed = validate_resolution_inputs(
            state,
            decision,
            loaded_schema=loaded_schema,
            freshness_context=freshness_context,
            registry=registry,
        )
    except ModelDecisionReferenceError as exc:
        raise UnresolvableModelDecisionError(str(exc)) from exc
    except ResolutionInputError as exc:
        raise DecisionResolverError(str(exc)) from exc
    trusted_execution_seal, execution_seal_digest = _capture_resolution_execution_seal(
        registry
    )

    context = registry.context
    schema_runtime = context.schema_runtime
    resolver = _ScopedResolver(
        loaded_schema,
        table_namespace=cast(str, getattr(schema_runtime, "table_namespace")),
        documents=cast(tuple[object, ...], getattr(schema_runtime, "documents")),
        schema_namespace_version=current.schema_namespace_version,
    )
    for proposal in parsed.proposals:
        _resolve_semantic_value(proposal, resolver)
    bindings_by_id = {item.binding_id: item for item in current.bindings}
    joins_by_id = {item.join_id: item for item in current.join_candidates}
    proposed_join_ids: dict[str, str] = {}
    proposed_joins: dict[str, JoinCandidate] = {}
    for proposal in parsed.proposals:
        if (
            isinstance(proposal, BindingAssessment)
            and isinstance(proposal.subject, ExistingBindingRef)
        ):
            binding = bindings_by_id[proposal.subject.binding_id]
            for column in _structural_binding_columns(binding):
                resolver.record_existing_physical_column(column)
        elif (
            isinstance(proposal, JoinAssessment)
            and isinstance(proposal.subject, ExistingJoinRef)
        ):
            join = joins_by_id[proposal.subject.join_id]
            for edge in join.path:
                resolver.record_existing_physical_column(edge.left)
                resolver.record_existing_physical_column(edge.right)
            resolver.record_declared_join(join)
        elif isinstance(proposal, NewJoinProposal):
            join = _new_join(proposal, resolver, current.schema_namespace_version)
            resolver.record_declared_join(join)
            proposed_join_ids[proposal.proposal_key] = join.join_id
            proposed_joins[join.join_id] = join
    decision_digest = canonical_digest(
        parsed.model_dump(mode="json", by_alias=True, warnings="error")
    )
    tool_claim: TrustedToolClaim | None = None
    invocation: ToolInvocation | None = None
    if isinstance(parsed.next, ToolIntent):
        arguments = _resolve_tool_arguments(parsed.next, resolver)
        try:
            resolved_claim = resolve_research_tool_claim(
                parsed.next.intent.tool_name,
                arguments,
                context,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise DecisionResolverError(
                "resolved tool request cannot form a trusted physical claim"
            ) from exc
        action_id = _stable_id(
            "action",
            current,
            decision_digest,
            {
                "kind": resolved_claim.kind.value,
                "target": resolved_claim.target.model_dump(mode="json", by_alias=True),
                "parameters": resolved_claim.parameters,
            },
        )
        tool_claim = TrustedToolClaim(
            action_id=action_id,
            tool_name=parsed.next.intent.tool_name,
            target=resolved_claim.target,
            parameters=resolved_claim.parameters,
        )
        tool_call_id = _stable_id(
            "tool-call",
            current,
            decision_digest,
            {
                "tool_name": parsed.next.intent.tool_name,
                "arguments": resolved_claim.arguments,
            },
        )
        try:
            parsed_call = parse_model_action(
                {
                    "kind": "tool",
                    "tool_call_id": tool_call_id,
                    "tool_name": parsed.next.intent.tool_name,
                    "arguments": resolved_claim.arguments,
                }
            )
        except (AdaptiveLoopActionError, TypeError, ValueError) as exc:
            raise DecisionResolverError(
                "resolved decision exceeds the closed tool-call contract"
            ) from exc
        if not isinstance(parsed_call, ToolCall):
            raise DecisionResolverError("resolved decision did not form one tool call")
        invocation_id = _stable_id(
            "invocation",
            current,
            decision_digest,
            {"tool_call_id": tool_call_id},
        )
        remaining_seconds = _remaining_seconds(deadline)
        invocation = ToolInvocation(
            run_id=current.run_id,
            run_incarnation=current.run_incarnation,
            loop_kind=AdaptiveLoopKind.RESEARCH,
            revision=current.revision,
            tool_call=parsed_call,
            invocation_id=invocation_id,
            remaining_seconds=remaining_seconds,
            deadline=deadline,
        )

    batch = resolver.batch()
    try:
        semantic_resolver = _SemanticResolver(batch)
        all_joins = {**joins_by_id, **proposed_joins}
        semantic_items_by_source = {
            item.source_id: item for item in current.query_spec.semantic_items
        }
        duplicate_existing_binding_ids_by_proposal_key: list[tuple[str, str]] = []
        for proposal in parsed.proposals:
            if isinstance(proposal, NewBindingProposal):
                binding = _new_binding(
                    proposal,
                    semantic_resolver,
                    all_joins,
                    proposed_join_ids,
                    current.schema_namespace_version,
                    semantic_items_by_source.get(proposal.source_id),
                )
                if binding.binding_id in bindings_by_id:
                    duplicate_existing_binding_ids_by_proposal_key.append(
                        (proposal.proposal_key, binding.binding_id)
                    )
        if duplicate_existing_binding_ids_by_proposal_key:
            raise DuplicateExistingBindingProposalError(
                tuple(sorted(duplicate_existing_binding_ids_by_proposal_key))
            )
        admission = admit_semantic_turn(
            current,
            parsed,
            batch=batch,
            freshness_context=freshness_context,
            tool_claim=tool_claim,
            budget_state=current.budget_state,
        )
    except DuplicateExistingBindingProposalError:
        raise
    except SemanticReducerError as exc:
        baseline_next = parsed.next
        if isinstance(baseline_next, ToolIntent):
            baseline_next = baseline_next.model_copy(
                update={"hypothesis_ref": None}
            )
        baseline = parsed.model_copy(
            update={"proposals": (), "next": baseline_next}
        )
        try:
            admit_semantic_turn(
                current,
                baseline,
                batch=batch,
                freshness_context=freshness_context,
                tool_claim=tool_claim,
                budget_state=current.budget_state,
            )
        except (TypeError, ValueError, ValidationError):
            raise DecisionResolverError(
                "semantic decision admission failed"
            ) from exc
        raise UnresolvableModelDecisionError(
            "model semantic decision is not admissible"
        ) from exc
    except (TypeError, ValueError, ValidationError) as exc:
        raise DecisionResolverError("semantic decision admission failed") from exc
    _validate_admitted_identity(current, admission, invocation)
    state_digest = _state_digest(current)
    resolution_digest = _resolution_digest(
        parsed,
        decision_digest,
        state_digest,
        batch,
        tool_claim,
        admission,
        invocation,
        execution_seal_digest,
    )
    current_execution_seal, current_execution_digest = (
        _capture_resolution_execution_seal(registry)
    )
    if (
        current_execution_seal != trusted_execution_seal
        or current_execution_digest != execution_seal_digest
    ):
        raise DecisionResolverError(
            "trusted adaptive runtime changed during decision resolution"
        )
    return ResolvedResearchDecision(
        decision=parsed,
        decision_digest=decision_digest,
        state_digest=state_digest,
        semantic_batch=batch,
        tool_claim=tool_claim,
        admission=admission,
        invocation=invocation,
        resolution_digest=resolution_digest,
        registry=registry,
        _trusted_execution_seal=trusted_execution_seal,
        _trusted_execution_digest=execution_seal_digest,
    )


def _structural_binding_columns(binding: object) -> tuple[ColumnRef, ...]:
    if isinstance(binding, PhysicalColumnBinding):
        return (binding.physical_column,)
    if isinstance(binding, DiscriminatorValueBinding):
        return binding.columns
    if isinstance(binding, DerivedExpressionBinding):
        return binding.input_columns
    if isinstance(binding, VerticalAttributeBinding):
        return (
            binding.entity_key,
            binding.attribute_catalog_key,
            binding.attribute_name_predicate.left,
            binding.value_entity_key,
            binding.value_attribute_key,
            binding.value_predicate.left,
        )
    return ()


def execute_resolved_research_decision(
    resolved: ResolvedResearchDecision,
    registry: AdaptiveResearchToolRegistry,
    *,
    recover: bool = False,
) -> ProbeResult | None:
    """Execute or recover exactly once and return the exact typed probe result."""

    if not isinstance(resolved, ResolvedResearchDecision):
        raise TypeError("resolved must be ResolvedResearchDecision")
    if not isinstance(registry, AdaptiveResearchToolRegistry):
        raise TypeError("registry must be AdaptiveResearchToolRegistry")
    if type(recover) is not bool:
        raise TypeError("recover must be a boolean")
    _validate_resolution_seal(resolved)
    if registry is not resolved.registry:
        raise DecisionExecutionError("resolved decision registry was swapped")
    _validate_trusted_execution_seal(resolved, registry)
    resolved._execution_gate.consume()
    invocation = resolved.invocation
    if invocation is None:
        return None
    action = resolved.admission.action
    if action is None:
        raise DecisionExecutionError("tool invocation has no admitted action")

    try:
        adapter = registry.resolve(invocation.tool_call.tool_name)
    except Exception as exc:
        raise DecisionExecutionError("adaptive tool resolution failed") from exc
    _validate_trusted_execution_seal(resolved, registry)
    if adapter is None:
        raise DecisionExecutionError("resolved adaptive tool is unavailable")
    try:
        adapter_methods = registry.bind_owned_adapter_methods(
            invocation.tool_call.tool_name,
            adapter,
        )
    except Exception as exc:
        raise DecisionExecutionError("adaptive tool ownership check failed") from exc
    _validate_trusted_execution_seal(resolved, registry)
    if adapter_methods is None:
        raise DecisionExecutionError("resolved adaptive tool is not registry-owned")
    execute_method, recover_method = adapter_methods
    try:
        if recover:
            normalized = recover_method(invocation)
            if normalized is None:
                raise DecisionExecutionError("exact prior invocation was not recovered")
        else:
            normalized = execute_method(invocation)
        if inspect.isawaitable(normalized):
            close = getattr(normalized, "close", None)
            if callable(close):
                close()
            raise DecisionExecutionError("adaptive tool returned an awaitable")
    except DecisionExecutionError:
        raise
    except Exception as exc:
        raise DecisionExecutionError(
            "adaptive tool failed before returning a typed result"
        ) from exc

    result = _parse_normalized_result(normalized)
    expected_status = "success" if result.status is ProbeStatus.SUCCESS else "error"
    if normalized.status != expected_status:
        raise DecisionExecutionError("normalized status contradicts ProbeResult status")
    if (
        result.run_id != invocation.run_id
        or result.run_incarnation != invocation.run_incarnation
        or result.revision != invocation.revision
        or result.schema_namespace_version
        != resolved.admission.state.schema_namespace_version
        or result.invocation_id != invocation.invocation_id
        or result.action_digest != action.action_digest
        or result.probe_kind is not action.kind
        or result.target != action.target
    ):
        raise DecisionExecutionError("ProbeResult identity does not match admission")
    return result


def _resolve_semantic_value(value: object, resolver: _ScopedResolver) -> None:
    if isinstance(value, LogicalColumnRef):
        resolver.column(value.table, value.column)
        return
    if isinstance(value, LogicalTableRef):
        resolver.table(value.table)
        return
    if isinstance(value, LogicalColumnTarget):
        resolver.column(value.table, value.column)
        return
    if isinstance(value, LogicalTableTarget):
        resolver.table(value.table)
        return
    if isinstance(value, LogicalDocumentTarget):
        resolver.document(value.document_id)
        return
    if isinstance(value, DocumentRuleCandidate):
        resolver.document(value.document_id)
    if isinstance(value, DerivedExpressionCandidate):
        resolver.document(value.document_id)
    if isinstance(value, StrictModel):
        for field_name in type(value).model_fields:
            _resolve_semantic_value(getattr(value, field_name), resolver)
        return
    if isinstance(value, tuple):
        for item in value:
            _resolve_semantic_value(item, resolver)


def _resolve_tool_arguments(
    next_step: ToolIntent,
    resolver: _ScopedResolver,
) -> dict[str, JsonValue]:
    name = next_step.intent.tool_name
    raw = next_step.intent.arguments.model_dump(
        mode="python", round_trip=True, warnings="error"
    )
    arguments = cast(dict[str, JsonValue], raw)
    if name in {"inspect_table", "inspect_relationships", "sample_rows"}:
        logical_table = cast(str, arguments["table"])
        table = resolver.table(logical_table)
        arguments["table"] = _physical_table_name(table)
        if name == "sample_rows":
            columns = cast(tuple[JsonValue, ...], arguments["columns"])
            arguments["columns"] = tuple(
                resolver.column(logical_table, cast(str, column)).column
                for column in columns
            )
    elif name in {
        "inspect_column",
        "profile_column",
        "search_value",
        "get_distinct_values",
    }:
        logical_table = cast(str, arguments["table"])
        logical_column = cast(str, arguments["column"])
        column = resolver.column(logical_table, logical_column)
        arguments["table"] = _physical_table_name(column.table)
        arguments["column"] = column.column
    elif name == "read_schema_evidence":
        document = resolver.document(cast(str, arguments["document_id"]))
        arguments["document_id"] = document.document_id
    return arguments


def _physical_table_name(table: TableRef) -> str:
    if table.schema_name is None:
        return table.table
    return f"{table.schema_name}.{table.table}"


def _remaining_seconds(deadline: DeadlineBudget | None) -> float | None:
    if deadline is None:
        return None
    if not isinstance(deadline, DeadlineBudget):
        raise DecisionResolverError("deadline must be DeadlineBudget or null")
    remaining = deadline.remaining_seconds()
    if not math.isfinite(remaining) or remaining <= 0:
        raise DecisionResolverError("deadline is exhausted")
    return remaining


def _stable_id(
    prefix: str,
    state: ResearchState,
    decision_digest: str,
    payload: Mapping[str, object],
) -> str:
    digest = canonical_digest(
        {
            "identity_version": 1,
            "run_id": state.run_id,
            "run_incarnation": state.run_incarnation,
            "revision": state.revision,
            "schema_namespace_version": state.schema_namespace_version,
            "decision_digest": decision_digest,
            "payload": payload,
        }
    ).split(":", 1)[1]
    return f"{prefix}:{digest}"


def _validate_admitted_identity(
    state: ResearchState,
    admission: SemanticTurnAdmission,
    invocation: ToolInvocation | None,
) -> None:
    action = admission.action
    if invocation is None:
        if isinstance(admission.state, ResearchState) and action is not None:
            if action.kind is not ResearchActionKind.SEMANTIC_COMMIT:
                raise DecisionResolverError(
                    "non-tool admission unexpectedly created a probe action"
                )
            if action.action_id in {item.action_id for item in state.action_history}:
                raise DecisionResolverError("resolved action_id is already present")
            if action.action_digest in {
                item.action_digest for item in state.action_history
            }:
                raise DuplicateResearchActionError(action)
        return
    if action is None:
        raise DecisionResolverError("tool admission did not create an action")
    if action.action_id in {item.action_id for item in state.action_history}:
        raise DecisionResolverError("resolved action_id is already present")
    if action.action_digest in {item.action_digest for item in state.action_history}:
        raise DuplicateResearchActionError(action)
    if invocation.invocation_id in {item.evidence_id for item in state.evidence}:
        raise DecisionResolverError("resolved invocation was already observed")
    identifiers = {
        action.action_id,
        invocation.tool_call.tool_call_id,
        invocation.invocation_id,
    }
    if len(identifiers) != 3:
        raise DecisionResolverError("resolved tool identifiers must be distinct")


def _state_digest(state: ResearchState) -> str:
    return canonical_digest(
        state.model_dump(mode="json", by_alias=True, warnings="error")
    )


def _deadline_payload(value: object) -> object:
    try:
        return deadline_identity_payload(value)
    except Exception as exc:
        raise DecisionResolverError("invocation deadline is invalid") from exc


def _resolution_payload(
    decision: ResearchDecisionV1,
    decision_digest: str,
    state_digest: str,
    batch: TrustedSemanticBatch,
    claim: TrustedToolClaim | None,
    admission: SemanticTurnAdmission,
    invocation: ToolInvocation | None,
    trusted_execution_digest: str,
) -> dict[str, object]:
    invocation_value = None
    if invocation is not None:
        invocation_value = {
            "run_id": invocation.run_id,
            "run_incarnation": invocation.run_incarnation,
            "loop_kind": invocation.loop_kind.value,
            "revision": invocation.revision,
            "tool_call": {
                "tool_call_id": invocation.tool_call.tool_call_id,
                "tool_name": invocation.tool_call.tool_name,
                "arguments": invocation.tool_call.arguments,
            },
            "invocation_id": invocation.invocation_id,
            "remaining_seconds": invocation.remaining_seconds,
            "deadline": _deadline_payload(invocation.deadline),
        }
    return {
        "decision": decision.model_dump(mode="json", by_alias=True, warnings="error"),
        "decision_digest": decision_digest,
        "state_digest": state_digest,
        "batch": batch.model_dump(mode="json", by_alias=True, warnings="error"),
        "claim": (
            None
            if claim is None
            else claim.model_dump(mode="json", by_alias=True, warnings="error")
        ),
        "action": (
            None
            if admission.action is None
            else admission.action.model_dump(
                mode="json", by_alias=True, warnings="error"
            )
        ),
        "invocation": invocation_value,
        "trusted_execution_digest": trusted_execution_digest,
    }


def _resolution_digest(
    decision: ResearchDecisionV1,
    decision_digest: str,
    state_digest: str,
    batch: TrustedSemanticBatch,
    claim: TrustedToolClaim | None,
    admission: SemanticTurnAdmission,
    invocation: ToolInvocation | None,
    trusted_execution_digest: str,
) -> str:
    return canonical_digest(
        _resolution_payload(
            decision,
            decision_digest,
            state_digest,
            batch,
            claim,
            admission,
            invocation,
            trusted_execution_digest,
        )
    )


def _validate_resolution_seal(resolved: ResolvedResearchDecision) -> None:
    if _state_digest(resolved.admission.state) != resolved.state_digest:
        raise DecisionExecutionError("resolved state identity was changed")
    actual_decision_digest = canonical_digest(
        resolved.decision.model_dump(mode="json", by_alias=True, warnings="error")
    )
    if actual_decision_digest != resolved.decision_digest:
        raise DecisionExecutionError("resolved decision identity was changed")
    actual_resolution_digest = _resolution_digest(
        resolved.decision,
        resolved.decision_digest,
        resolved.state_digest,
        resolved.semantic_batch,
        resolved.tool_claim,
        resolved.admission,
        resolved.invocation,
        resolved._trusted_execution_digest,
    )
    if actual_resolution_digest != resolved.resolution_digest:
        raise DecisionExecutionError("resolved tool identity was changed")


def _parse_normalized_result(value: object) -> ProbeResult:
    if not isinstance(value, NormalizedToolResult):
        raise DecisionExecutionError("adapter did not return NormalizedToolResult")
    if value.status not in {"success", "error"}:
        raise DecisionExecutionError("normalized tool status is invalid")
    try:
        encoded = canonical_json_bytes(value.value)
        result = ProbeResult.model_validate_json(encoded)
        round_trip = canonical_json_bytes(
            result.model_dump(mode="json", by_alias=True, warnings="error")
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise DecisionExecutionError(
            "normalized tool value is not an exact ProbeResult"
        ) from exc
    if round_trip != encoded:
        raise DecisionExecutionError(
            "normalized ProbeResult did not round-trip losslessly"
        )
    return result


__all__ = [
    "DecisionAlreadyExecutedError",
    "DecisionExecutionError",
    "DecisionResolverError",
    "DuplicateResearchActionError",
    "ResolvedResearchDecision",
    "UnresolvableModelDecisionError",
    "execute_resolved_research_decision",
    "resolve_research_decision",
]
