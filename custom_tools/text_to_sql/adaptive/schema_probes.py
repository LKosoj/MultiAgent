"""Internal bounded schema probes backed by the trusted scoped loader."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
import re
import threading
import time
import uuid
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import field_validator

from db_plugins.base import (
    CapabilityState,
    DatabaseCapabilityError,
    DatabaseCapabilities,
    RequiredDatabaseCapabilities,
    validate_required_capabilities,
)
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

from ..schema_loader import LoadedSchema
from ..schema_metadata import get_foreign_key_constraints, is_pk
from ..schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from ..utils import get_table_columns
from .models import (
    ColumnRef,
    Digest,
    DocumentRef,
    EvidenceCost,
    Id,
    NonEmptyText,
    ResearchAction,
    ResearchActionKind,
    ResearchState,
    StrictModel,
    TableRef,
    TargetRef,
)
from .policy import (
    AdaptivePolicyConfig,
    BudgetLedger,
    BudgetReservation,
    ProbeExecutionFailure,
    execute_probe_with_budget,
)
from .probes import ProbeResult, ProbeStatus, build_probe_result
from .serialization import canonical_digest, canonical_json_bytes


MAX_SCHEMA_PROBE_TOP_K = 50
MAX_SCHEMA_PROBE_DEPTH = 4
MAX_CONTAINMENT_SAMPLE_SIZE = 1_000
MAX_AMBIGUITY_PATHS_PER_TABLE = 2
RRF_K = 60
# Only the top-3 lexical matches act as FK-adjacency anchors: this keeps the
# structural tie-break focused on plausible target tables (it mirrors the
# legacy max()-based top-3 that originally seeded the F05 ambiguity check)
# instead of letting every distant, low-scoring table pull in FK evidence.
FK_ANCHOR_CANDIDATE_COUNT = 3


class ScopedSchemaLoader(Protocol):
    def load_scoped_schema(
        self,
        schema_info: dict[str, Any],
        dsn: str,
        scope: SchemaScope,
    ) -> LoadedSchema: ...


class SchemaEvidenceDocument(StrictModel):
    """One trusted structural document selectable by an internal probe."""

    document_id: Id
    namespace: NonEmptyText
    schema_namespace_version: Digest
    source_version: NonEmptyText
    valid_until: datetime | None = None
    title: NonEmptyText
    content: NonEmptyText
    target: TableRef | ColumnRef | None

    @field_validator("valid_until")
    @classmethod
    def validate_valid_until(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("valid_until must be a UTC timestamp")
        return value


@dataclass(frozen=True, slots=True)
class SchemaProbeRuntime:
    """Trusted schema access that is never populated from model arguments."""

    loader: ScopedSchemaLoader
    dsn: str
    scope: SchemaScope
    namespace: SchemaNamespace
    table_namespace: str
    deadline: DeadlineBudget
    documents: tuple[SchemaEvidenceDocument, ...] = ()
    get_plugin: Callable[[str], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    containment_probe: Callable[..., float | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    containment_sample_size: int | None = None
    monotonic_ns: Callable[[], int] = field(
        default=time.monotonic_ns,
        repr=False,
        compare=False,
    )
    utc_now: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC),
        repr=False,
        compare=False,
    )
    loaded_schema: LoadedSchema | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(getattr(self.loader, "load_scoped_schema", None)):
            raise TypeError("loader must provide load_scoped_schema")
        if type(self.dsn) is not str or not self.dsn.strip():
            raise ValueError("dsn must be trusted non-empty text")
        if not isinstance(self.scope, SchemaScope):
            raise TypeError("scope must be SchemaScope")
        if not isinstance(self.namespace, SchemaNamespace):
            raise TypeError("namespace must be SchemaNamespace")
        if type(self.table_namespace) is not str or not self.table_namespace:
            raise ValueError("table_namespace must be non-empty text")
        if not isinstance(self.deadline, DeadlineBudget):
            raise TypeError("deadline must be DeadlineBudget")
        if self.loaded_schema is not None and not isinstance(
            self.loaded_schema, LoadedSchema
        ):
            raise TypeError("loaded_schema must be LoadedSchema or null")
        if type(self.documents) is not tuple or not all(
            isinstance(document, SchemaEvidenceDocument) for document in self.documents
        ):
            raise TypeError("documents must be SchemaEvidenceDocument values")
        document_ids = [document.document_id for document in self.documents]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("schema evidence document IDs must be unique")
        for callback, name in (
            (self.get_plugin, "get_plugin"),
            (self.containment_probe, "containment_probe"),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or null")
        if self.containment_sample_size is not None and (
            type(self.containment_sample_size) is not int
            or not 1 <= self.containment_sample_size <= MAX_CONTAINMENT_SAMPLE_SIZE
        ):
            raise ValueError("containment_sample_size is outside its closed bound")
        if self.containment_probe is not None and self.containment_sample_size is None:
            raise ValueError("containment_probe requires containment_sample_size")
        if not callable(self.monotonic_ns) or not callable(self.utc_now):
            raise TypeError("schema probe clocks must be callable")
        if self.loaded_schema is not None:
            _validate_loaded_schema_snapshot(
                self.loaded_schema,
                namespace=self.namespace,
                scope=self.scope,
            )


@dataclass(frozen=True, slots=True)
class SchemaProbeBudgetRuntime:
    """Trusted W2-03 admission and accounting inputs for one probe."""

    state: ResearchState
    action: ResearchAction
    maximum_cost: EvidenceCost
    config: AdaptivePolicyConfig
    ledger: BudgetLedger
    invocation_id: str
    monotonic_ns: Callable[[], int] = field(
        default=time.monotonic_ns,
        repr=False,
        compare=False,
    )
    utc_now: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC),
        repr=False,
        compare=False,
    )
    claim_now_ns: Callable[[], int] = field(
        default=time.time_ns,
        repr=False,
        compare=False,
    )
    owner_token_factory: Callable[[], str] = field(
        default=lambda: uuid.uuid4().hex,
        repr=False,
        compare=False,
    )
    wait: Callable[[float], None] = field(
        default=time.sleep,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.state, ResearchState):
            raise TypeError("state must be ResearchState")
        if not isinstance(self.action, ResearchAction):
            raise TypeError("action must be ResearchAction")
        if not isinstance(self.maximum_cost, EvidenceCost):
            raise TypeError("maximum_cost must be EvidenceCost")
        if not isinstance(self.config, AdaptivePolicyConfig):
            raise TypeError("config must be AdaptivePolicyConfig")
        for method_name in (
            "load_records",
            "record_reservation",
            "claim_execution",
            "record_result",
            "record_reconciliation",
        ):
            if not callable(getattr(self.ledger, method_name, None)):
                raise TypeError(f"ledger must provide {method_name}")
        if type(self.invocation_id) is not str or not self.invocation_id:
            raise ValueError("invocation_id must be non-empty text")
        for callback, name in (
            (self.monotonic_ns, "monotonic_ns"),
            (self.utc_now, "utc_now"),
            (self.claim_now_ns, "claim_now_ns"),
            (self.owner_token_factory, "owner_token_factory"),
            (self.wait, "wait"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")


@dataclass(frozen=True, slots=True)
class _Observation:
    payload: dict[str, object]
    rows: int
    truncated: bool
    summary: str


class _SchemaProbeFailure(RuntimeError):
    def __init__(
        self,
        failure_code: str,
        summary: str,
        *,
        status: ProbeStatus = ProbeStatus.FAILED,
        rows: int = 0,
        bytes_: int = 0,
    ) -> None:
        self.failure_code = failure_code
        self.summary = summary
        self.status = status
        self.rows = rows
        self.bytes = bytes_
        super().__init__(summary)


def search_schema_catalog(
    target: TableRef,
    top_k: int,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    """Search structural names and descriptions within one trusted scope."""

    def observe(schema: dict[str, object], _: DatabaseCapabilities) -> _Observation:
        query = target.table.strip().casefold()
        candidates: list[tuple[int, str, list[str], list[str]]] = []
        name_scores: dict[str, int] = {}
        description_scores: dict[str, int] = {}
        column_scores: dict[str, int] = {}
        for table_name in sorted(schema, key=str.casefold):
            table_body = schema[table_name]
            if not isinstance(table_body, Mapping):
                raise _SchemaProbeFailure(
                    "schema_invalid",
                    "schema catalog table metadata is invalid",
                )
            matched_fields: list[str] = []
            table_score = _text_match_score(query, table_name)
            if table_score:
                matched_fields.append("table")
                name_scores[table_name] = table_score
            description = table_body.get("description", "")
            description_score = (
                _text_match_score(query, description)
                if isinstance(description, str)
                else 0
            )
            if description_score:
                matched_fields.append("table_description")
                description_scores[table_name] = description_score
            matched_columns: list[str] = []
            column_score = 0
            for column_name, metadata in get_table_columns(table_body).items():
                score = _text_match_score(query, column_name)
                if isinstance(metadata, Mapping):
                    column_description = metadata.get("description", "")
                    if isinstance(column_description, str):
                        score = max(
                            score,
                            _text_match_score(query, column_description),
                        )
                if score:
                    matched_columns.append(column_name)
                    column_score = max(column_score, score)
            if column_score:
                column_scores[table_name] = column_score
            score = max(table_score, description_score, column_score)
            if score:
                candidates.append(
                    (
                        score,
                        table_name,
                        sorted(matched_columns, key=str.casefold),
                        matched_fields,
                    )
                )
        # Recall-first ranking (W3-3.1): fuse the three independent lexical
        # signals (table name / description / column) via Reciprocal Rank
        # Fusion instead of collapsing them with max(), so a table that
        # matches several signals can rise above one that only matches a
        # single, higher-scoring signal. The structural FK-adjacency signal
        # is deliberately excluded from that sum (see
        # ``_rank_catalog_candidates``): it only breaks ties among
        # candidates whose lexical RRF is exactly equal, so it can never
        # displace a candidate with a strictly stronger lexical match. Only
        # tables that already have a lexical match (score > 0) can appear in
        # results, which keeps total_matches/truncated and the pinned F05
        # ambiguity ordering identical to the legacy behaviour when every
        # table matches the same single signal.
        ranked_candidates, is_ambiguous = _rank_catalog_candidates(
            candidates,
            name_scores,
            description_scores,
            column_scores,
            _cached_relationship_edges(schema, runtime),
        )
        status = (
            "missing"
            if not ranked_candidates
            else "ambiguous"
            if is_ambiguous
            else "matched"
        )
        selected = ranked_candidates[:top_k]
        results = [
            {
                "match_fields": fields,
                "matched_columns": columns,
                "score": score,
                "table": _table_ref(table_name, runtime).model_dump(
                    mode="json",
                    by_alias=True,
                ),
            }
            for score, table_name, columns, fields in selected
        ]
        return _Observation(
            payload=_payload(
                runtime,
                status=status,
                query=target.table,
                results=results,
                total_matches=len(candidates),
            ),
            rows=len(results),
            truncated=len(candidates) > top_k,
            summary=f"schema catalog search: {status}",
        )

    return _run_schema_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.INSPECT_CATALOG,
        parameters={"top_k": top_k},
        top_k=top_k,
        observe=observe,
    )


def inspect_table(
    target: TableRef,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    """Return one resolved table's structural metadata, never row data."""

    def observe(
        schema: dict[str, object], capabilities: DatabaseCapabilities
    ) -> _Observation:
        status, matches = _resolve_table(target, schema, runtime)
        if status != "matched":
            return _resolution_observation(runtime, target, status, matches)
        table_name = matches[0]
        table_body = schema[table_name]
        if not isinstance(table_body, Mapping):
            raise _SchemaProbeFailure(
                "schema_invalid",
                "table metadata is invalid",
            )
        columns = []
        for column_name, metadata in sorted(
            get_table_columns(table_body).items(),
            key=lambda item: (item[0].casefold(), item[0]),
        ):
            if not isinstance(metadata, Mapping):
                raise _SchemaProbeFailure(
                    "schema_invalid",
                    "column metadata is invalid",
                )
            columns.append(
                {
                    "constraint_type": str(metadata.get("constraint_type", "")),
                    "description": str(metadata.get("description", "")),
                    "name": column_name,
                    "not_null": str(metadata.get("not_null", "")),
                    "type": str(metadata.get("type", "")),
                }
            )
        declared = [
            _declared_relationship(table_name, constraint, table_body)
            for constraint in get_foreign_key_constraints(table_name, schema)
        ]
        _require_composite_capability(declared, capabilities)
        declared.sort(key=lambda edge: str(edge["relationship_id"]))
        inferred = [
            edge
            for edge in _relationship_edges(schema)
            if edge["relationship_kind"] == "inferred"
            and table_name in {edge["from_table"], edge["to_table"]}
        ]
        return _Observation(
            payload=_payload(
                runtime,
                status="matched",
                table=_table_ref(table_name, runtime).model_dump(
                    mode="json",
                    by_alias=True,
                ),
                description=str(table_body.get("description", "")),
                columns=columns,
                relationships=declared + inferred,
            ),
            rows=1,
            truncated=False,
            summary="table schema inspected",
        )

    return _run_schema_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.INSPECT_TABLE,
        parameters={},
        observe=observe,
    )


def inspect_column(
    target: ColumnRef,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    """Return one resolved column's declared structural metadata."""

    def observe(
        schema: dict[str, object], capabilities: DatabaseCapabilities
    ) -> _Observation:
        table_status, table_matches = _resolve_table(target.table, schema, runtime)
        if table_status != "matched":
            return _column_resolution_observation(
                runtime,
                target,
                table_status,
                table_matches=table_matches,
            )
        table_name = table_matches[0]
        table_body = schema[table_name]
        if not isinstance(table_body, Mapping):
            raise _SchemaProbeFailure("schema_invalid", "table metadata is invalid")
        columns = get_table_columns(table_body)
        column_status, column_matches = _resolve_column(target, columns)
        if column_status != "matched":
            return _column_resolution_observation(
                runtime,
                target,
                column_status,
                table_name=table_name,
                column_matches=column_matches,
            )
        column_name = column_matches[0]
        metadata = columns[column_name]
        if not isinstance(metadata, Mapping):
            raise _SchemaProbeFailure("schema_invalid", "column metadata is invalid")
        outgoing, incoming = _declared_column_relationships(
            table_name,
            column_name,
            schema,
        )
        _require_composite_capability(outgoing + incoming, capabilities)
        return _Observation(
            payload=_payload(
                runtime,
                status="matched",
                column=_column_ref(
                    table_name,
                    column_name,
                    runtime,
                ).model_dump(mode="json", by_alias=True),
                metadata={
                    "constraint": _metadata_text(metadata.get("constraint_type")),
                    "default": _metadata_text(metadata.get("default_value")),
                    "description": _metadata_text(metadata.get("description")),
                    "is_primary_key": is_pk(dict(metadata)),
                    "not_null": _metadata_text(metadata.get("not_null")),
                    "type": _metadata_text(metadata.get("type")),
                },
                outgoing_relationships=outgoing,
                incoming_relationships=incoming,
            ),
            rows=1,
            truncated=False,
            summary="column schema inspected",
        )

    return _run_schema_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.INSPECT_COLUMN,
        parameters={},
        observe=observe,
    )


def inspect_relationships(
    target: TableRef,
    top_k: int,
    depth: int,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    """Inspect a bounded graph without promoting a heuristic to a fact."""

    def observe(
        schema: dict[str, object], capabilities: DatabaseCapabilities
    ) -> _Observation:
        status, matches = _resolve_table(target, schema, runtime)
        if status != "matched":
            return _resolution_observation(runtime, target, status, matches)
        root = matches[0]
        edges = _relationship_edges(schema)
        _require_composite_capability(edges, capabilities)
        visible, distances = _bounded_edges(root, edges, depth)
        visible_count = len(visible)
        visible = visible[:top_k]
        enriched = [_with_containment(edge, runtime) for edge in visible]
        paths = _bounded_paths(root, edges, depth)
        path_rows = []
        for table_name in sorted(
            paths,
            key=lambda item: (
                len(paths[item]) <= 1,
                item.casefold(),
                item,
            ),
        ):
            table_paths = paths[table_name]
            ambiguous = len(table_paths) > 1
            selected_ids = [] if ambiguous else table_paths[0]
            path_rows.append(
                {
                    "candidate_paths": table_paths[:top_k],
                    "preferred_path_signal": selected_ids,
                    "status": "ambiguous" if ambiguous else "connected",
                    "table": _table_ref(table_name, runtime).model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                }
            )
        disconnected = sorted(
            set(schema) - set(distances),
            key=lambda item: (item.casefold(), item),
        )
        truncated = (
            visible_count > top_k
            or len(path_rows) > top_k
            or any(len(value) > top_k for value in paths.values())
            or len(disconnected) > top_k
        )
        graph_status = (
            "ambiguous"
            if any(row["status"] == "ambiguous" for row in path_rows)
            else "isolated"
            if not enriched
            else "connected"
        )
        return _Observation(
            payload=_payload(
                runtime,
                status=graph_status,
                root=_table_ref(root, runtime).model_dump(
                    mode="json",
                    by_alias=True,
                ),
                depth=depth,
                relationships=enriched,
                paths=path_rows[:top_k],
                disconnected_tables=[
                    _table_ref(name, runtime).model_dump(mode="json", by_alias=True)
                    for name in disconnected[:top_k]
                ],
                disconnected_count=len(disconnected),
            ),
            rows=len(enriched),
            truncated=truncated,
            summary=f"relationship graph: {graph_status}",
        )

    return _run_schema_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.INSPECT_RELATIONSHIPS,
        parameters={"depth": depth, "top_k": top_k},
        top_k=top_k,
        depth=depth,
        observe=observe,
    )


def read_schema_evidence(
    target: DocumentRef,
    *,
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> ProbeResult:
    """Read one pre-authorized structural document from trusted runtime state."""

    def observe(_: dict[str, object], __: DatabaseCapabilities) -> _Observation:
        matches = [
            document
            for document in runtime.documents
            if document.document_id == target.document_id
        ]
        if len(matches) != 1:
            raise _SchemaProbeFailure(
                "schema_evidence_missing",
                "schema evidence document is unavailable",
            )
        document = matches[0]
        expected_version = _schema_namespace_version(runtime.namespace)
        if (
            document.namespace != runtime.table_namespace
            or document.schema_namespace_version != expected_version
        ):
            raise _SchemaProbeFailure(
                "schema_evidence_stale",
                "schema evidence document is outside the current namespace",
            )
        return _Observation(
            payload=_payload(
                runtime,
                status="matched",
                document=document.model_dump(mode="json", by_alias=True),
            ),
            rows=1,
            truncated=False,
            summary="schema evidence document read",
        )

    return _run_schema_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.READ_DOCUMENT,
        parameters={},
        observe=observe,
    )


def _run_schema_probe(
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
    *,
    target: TargetRef,
    kind: ResearchActionKind,
    parameters: dict[str, int],
    observe: Callable[[dict[str, object], DatabaseCapabilities], _Observation],
    top_k: int | None = None,
    depth: int | None = None,
) -> ProbeResult:
    if not isinstance(runtime, SchemaProbeRuntime):
        raise TypeError("runtime must be SchemaProbeRuntime")
    if not isinstance(budget, SchemaProbeBudgetRuntime):
        raise TypeError("budget must be SchemaProbeBudgetRuntime")

    def execute(reservation: BudgetReservation) -> ProbeResult:
        started_ns = _clock_ns(runtime.monotonic_ns)
        try:
            _validate_call(runtime, budget, target, kind, parameters, top_k, depth)
            runtime.deadline.require_remaining("schema probe introspection")
            schema, capabilities = _load_current_schema(runtime, budget)
            observation = observe(schema, capabilities)
            runtime.deadline.require_remaining("schema probe result")
            payload_bytes = canonical_json_bytes(observation.payload)
            cost = _cost(
                started_ns,
                runtime.monotonic_ns,
                rows=observation.rows,
                bytes_=len(payload_bytes),
            )
            if (
                observation.rows > reservation.maximum_cost.rows
                or len(payload_bytes) > reservation.maximum_cost.bytes
            ):
                raise ProbeExecutionFailure(
                    status=ProbeStatus.FAILED,
                    actual_cost=cost,
                    failure_code="schema_result_limit_exceeded",
                    summary="schema probe result exceeded its reserved bound",
                )
            observed_at = _utc(runtime.utc_now)
            return build_probe_result(
                run_id=reservation.run_id,
                run_incarnation=reservation.run_incarnation,
                revision=reservation.revision,
                schema_namespace_version=reservation.schema_namespace_version,
                invocation_id=budget.invocation_id,
                action_digest=reservation.action_digest,
                probe_kind=reservation.probe_kind,
                status=ProbeStatus.SUCCESS,
                target=reservation.target,
                started_at=observed_at,
                completed_at=observed_at,
                summary=observation.summary,
                cost=cost,
                row_count=observation.rows,
                truncated=observation.truncated,
                payload=observation.payload,
            )
        except ProbeExecutionFailure:
            raise
        except WorkflowDeadlineExceeded as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.TIMED_OUT,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code="schema_deadline_exceeded",
                summary="schema probe deadline expired",
            ) from exc
        except TimeoutError as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.TIMED_OUT,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code="schema_probe_timed_out",
                summary="schema probe timed out",
            ) from exc
        except _SchemaProbeFailure as exc:
            raise ProbeExecutionFailure(
                status=exc.status,
                actual_cost=_cost(
                    started_ns,
                    runtime.monotonic_ns,
                    rows=exc.rows,
                    bytes_=exc.bytes,
                ),
                failure_code=exc.failure_code,
                summary=exc.summary,
            ) from exc
        except Exception as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.FAILED,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code="schema_probe_failed",
                summary="schema probe failed",
            ) from exc

    result, _ = execute_probe_with_budget(
        budget.state,
        budget.action,
        budget.maximum_cost,
        execute,
        config=budget.config,
        ledger=budget.ledger,
        invocation_id=budget.invocation_id,
        monotonic_ns=budget.monotonic_ns,
        utc_now=budget.utc_now,
        claim_now_ns=budget.claim_now_ns,
        owner_token_factory=budget.owner_token_factory,
        wait=budget.wait,
    )
    return result


def _validate_call(
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
    target: TargetRef,
    kind: ResearchActionKind,
    parameters: dict[str, int],
    top_k: int | None,
    depth: int | None,
) -> None:
    if runtime.namespace.scope != runtime.scope:
        raise _SchemaProbeFailure(
            "schema_scope_mismatch",
            "trusted schema scope does not match its namespace",
        )
    target_namespace = (
        target.table.namespace if isinstance(target, ColumnRef) else target.namespace
    )
    if target_namespace != runtime.table_namespace:
        raise _SchemaProbeFailure(
            "target_namespace_mismatch",
            "probe target is outside the current table namespace",
        )
    if (
        budget.action.kind is not kind
        or budget.action.target != target
        or dict(budget.action.parameters) != parameters
    ):
        raise _SchemaProbeFailure(
            "action_mismatch",
            "probe call does not match its canonical research action",
        )
    expected_version = _schema_namespace_version(runtime.namespace)
    if budget.state.schema_namespace_version != expected_version:
        raise _SchemaProbeFailure(
            "schema_namespace_mismatch",
            "research state does not match the trusted schema namespace",
        )
    if top_k is not None and (
        type(top_k) is not int
        or top_k < 1
        or top_k > MAX_SCHEMA_PROBE_TOP_K
        or top_k > budget.maximum_cost.rows
    ):
        raise _SchemaProbeFailure(
            "top_k_out_of_bounds",
            "schema probe top_k exceeds its closed bound",
        )
    if depth is not None and (
        type(depth) is not int or depth < 1 or depth > MAX_SCHEMA_PROBE_DEPTH
    ):
        raise _SchemaProbeFailure(
            "depth_out_of_bounds",
            "schema probe depth exceeds its closed bound",
        )


def _load_current_schema(
    runtime: SchemaProbeRuntime,
    budget: SchemaProbeBudgetRuntime,
) -> tuple[dict[str, object], DatabaseCapabilities]:
    get_plugin = runtime.get_plugin
    if get_plugin is None:
        from db_plugins import get_plugin as default_get_plugin

        get_plugin = default_get_plugin
    plugin = get_plugin(runtime.dsn)
    get_capabilities = getattr(plugin, "get_capabilities", None)
    if not callable(get_capabilities):
        raise _SchemaProbeFailure(
            "schema_introspection_unavailable",
            "database plugin does not declare introspection capability",
        )
    capabilities = get_capabilities(runtime.dsn)
    if not isinstance(capabilities, DatabaseCapabilities):
        raise _SchemaProbeFailure(
            "schema_introspection_unavailable",
            "database plugin returned invalid capabilities",
        )
    try:
        validate_required_capabilities(
            capabilities,
            RequiredDatabaseCapabilities(
                read_only=False,
                statement_timeout=False,
                cancellation=False,
                explain=False,
                introspection=True,
                composite_fk_introspection=False,
                parameter_binding=False,
            ),
        )
    except DatabaseCapabilityError as exc:
        raise _SchemaProbeFailure(
            "schema_introspection_unavailable",
            "database plugin cannot introspect the trusted schema",
        ) from exc
    try:
        loaded = runtime.loaded_schema
        if loaded is None:
            loaded = runtime.loader.load_scoped_schema({}, runtime.dsn, runtime.scope)
            _validate_loaded_schema_integrity(loaded, scope=runtime.scope)
        else:
            _validate_loaded_schema_snapshot(
                loaded,
                namespace=runtime.namespace,
                scope=runtime.scope,
            )
    except (TypeError, ValueError) as exc:
        raise _SchemaProbeFailure(
            "schema_introspection_invalid",
            "scoped schema loader returned an invalid result",
        ) from exc
    schema = dict(loaded.schema)
    if (
        _schema_namespace_version(loaded.namespace)
        != budget.state.schema_namespace_version
    ):
        raise _SchemaProbeFailure(
            "schema_stale",
            "live schema does not match the admitted schema namespace",
        )
    return schema, capabilities


def _validate_loaded_schema_snapshot(
    loaded: object,
    *,
    namespace: SchemaNamespace,
    scope: SchemaScope,
) -> LoadedSchema:
    """Validate one admitted snapshot; injected snapshots are never reloaded."""

    if not isinstance(loaded, LoadedSchema):
        raise TypeError("loaded_schema must be LoadedSchema")
    if loaded.namespace != namespace or loaded.namespace.scope != scope:
        raise ValueError("loaded schema namespace does not match probe runtime")
    if canonical_schema_fingerprint(loaded.schema) != namespace.schema_fingerprint:
        raise ValueError("loaded schema fingerprint does not match probe runtime")
    return loaded


def _validate_loaded_schema_integrity(
    loaded: object,
    *,
    scope: SchemaScope,
) -> LoadedSchema:
    """Check a reloaded schema before comparing its version to the admission."""

    if not isinstance(loaded, LoadedSchema):
        raise TypeError("loaded_schema must be LoadedSchema")
    if loaded.namespace.scope != scope:
        raise ValueError("loaded schema scope does not match probe runtime")
    if (
        canonical_schema_fingerprint(loaded.schema)
        != loaded.namespace.schema_fingerprint
    ):
        raise ValueError("loaded schema fingerprint is invalid")
    return loaded


def _resolve_table(
    target: TableRef,
    schema: Mapping[str, object],
    runtime: SchemaProbeRuntime,
) -> tuple[str, list[str]]:
    if target.schema_name is not None:
        wanted = f"{target.schema_name}.{target.table}"
        matches = [name for name in schema if name.casefold() == wanted.casefold()]
    else:
        matches = [
            name
            for name in schema
            if name.rsplit(".", 1)[-1].casefold() == target.table.casefold()
        ]
    matches.sort(key=lambda name: (name.casefold(), name))
    if not matches:
        return "missing", []
    if len(matches) > 1:
        return "ambiguous", matches
    resolved = _table_ref(matches[0], runtime)
    if resolved.namespace != target.namespace:
        return "missing", []
    return "matched", matches


def _resolve_column(
    target: ColumnRef,
    columns: Mapping[str, object],
) -> tuple[str, list[str]]:
    matches = [name for name in columns if name.casefold() == target.column.casefold()]
    matches.sort(key=lambda name: (name.casefold(), name))
    if not matches:
        return "missing", []
    if len(matches) > 1:
        return "ambiguous", matches
    return "matched", matches


def _resolution_observation(
    runtime: SchemaProbeRuntime,
    target: TableRef,
    status: str,
    matches: list[str],
) -> _Observation:
    return _Observation(
        payload=_payload(
            runtime,
            status=status,
            requested=target.model_dump(mode="json", by_alias=True),
            candidates=[
                _table_ref(name, runtime).model_dump(mode="json", by_alias=True)
                for name in matches
            ],
        ),
        rows=len(matches),
        truncated=False,
        summary=f"table resolution: {status}",
    )


def _column_resolution_observation(
    runtime: SchemaProbeRuntime,
    target: ColumnRef,
    status: str,
    *,
    table_matches: list[str] | None = None,
    table_name: str | None = None,
    column_matches: list[str] | None = None,
) -> _Observation:
    resolved_columns = column_matches or []
    return _Observation(
        payload=_payload(
            runtime,
            status=status,
            requested=target.model_dump(mode="json", by_alias=True),
            candidate_tables=[
                _table_ref(name, runtime).model_dump(mode="json", by_alias=True)
                for name in (table_matches or [])
            ],
            candidate_columns=[
                _column_ref(table_name, name, runtime).model_dump(
                    mode="json",
                    by_alias=True,
                )
                for name in resolved_columns
                if table_name is not None
            ],
        ),
        rows=len(table_matches or []) + len(resolved_columns),
        truncated=False,
        summary=f"column resolution: {status}",
    )


def _declared_column_relationships(
    table_name: str,
    column_name: str,
    schema: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    outgoing: list[dict[str, object]] = []
    incoming: list[dict[str, object]] = []
    for source_table in sorted(
        schema,
        key=lambda name: (name.casefold(), name),
    ):
        table_body = schema[source_table]
        if not isinstance(table_body, Mapping):
            raise _SchemaProbeFailure("schema_invalid", "table metadata is invalid")
        for constraint in get_foreign_key_constraints(source_table, schema):
            edge = _declared_relationship(source_table, constraint, table_body)
            pairs = edge["column_pairs"]
            if source_table == table_name and any(
                pair["from_column"] == column_name for pair in pairs
            ):
                outgoing.append(edge)
            if edge["to_table"] == table_name and any(
                pair["to_column"] == column_name for pair in pairs
            ):
                incoming.append(edge)
    outgoing.sort(key=lambda edge: str(edge["relationship_id"]))
    incoming.sort(key=lambda edge: str(edge["relationship_id"]))
    return outgoing, incoming


def _relationship_edges(schema: Mapping[str, object]) -> list[dict[str, object]]:
    edges: list[dict[str, object]] = []
    declared_sources: set[tuple[str, str]] = set()
    for source_table in sorted(schema, key=lambda name: (name.casefold(), name)):
        table_body = schema[source_table]
        if not isinstance(table_body, Mapping):
            raise _SchemaProbeFailure("schema_invalid", "table metadata is invalid")
        for constraint in get_foreign_key_constraints(source_table, schema):
            edge = _declared_relationship(source_table, constraint, table_body)
            edges.append(edge)
            for pair in edge["column_pairs"]:
                declared_sources.add((source_table, pair["from_column"]))

    for source_table in sorted(schema, key=lambda name: (name.casefold(), name)):
        source_body = schema[source_table]
        if not isinstance(source_body, Mapping):
            raise _SchemaProbeFailure("schema_invalid", "table metadata is invalid")
        for source_column in sorted(
            get_table_columns(source_body),
            key=lambda name: (name.casefold(), name),
        ):
            if (
                source_table,
                source_column,
            ) in declared_sources or not source_column.casefold().endswith("_id"):
                continue
            base = source_column[:-3].casefold()
            targets: list[tuple[str, str]] = []
            for target_table in sorted(
                schema,
                key=lambda name: (name.casefold(), name),
            ):
                if target_table == source_table:
                    continue
                if target_table.rsplit(".", 1)[-1].casefold() != base:
                    continue
                target_body = schema[target_table]
                if not isinstance(target_body, Mapping):
                    continue
                target_columns = get_table_columns(target_body)
                primary_keys = [
                    name
                    for name, metadata in target_columns.items()
                    if isinstance(metadata, dict) and is_pk(metadata)
                ]
                if len(primary_keys) == 1:
                    targets.append((target_table, primary_keys[0]))
                elif not primary_keys and "id" in target_columns:
                    targets.append((target_table, "id"))
            for target_table, target_column in targets:
                edge = {
                    "column_pairs": [
                        {
                            "from_column": source_column,
                            "to_column": target_column,
                        }
                    ],
                    "constraint_id": None,
                    "from_table": source_table,
                    "metadata_format": "naming_convention",
                    "relationship_kind": "inferred",
                    "to_table": target_table,
                }
                edge["relationship_id"] = canonical_digest(edge)
                edges.append(edge)
    return sorted(
        edges,
        key=lambda edge: (
            str(edge["from_table"]).casefold(),
            str(edge["to_table"]).casefold(),
            str(edge["relationship_id"]),
        ),
    )


# Bounded memo for `_relationship_edges`, which is O(tables^2 * columns) and
# would otherwise be recomputed on every `search_schema_catalog` call within
# a research loop even though the schema has not changed. Keyed by
# `SchemaNamespace.version_key`, a hash of the schema's own canonical
# fingerprint plus scope, so distinct schema content never collides and a
# stale entry can never be read back for a schema that has since changed.
# Bounded to the most recently seen schemas so a long-lived process serving
# many databases cannot grow this without limit.
#
# Guarded by `_RELATIONSHIP_EDGES_CACHE_LOCK`: this cache is read and written
# from multiple `asyncio.to_thread` worker threads concurrently (the agui
# runner), and an unguarded plain dict raised
# `RuntimeError: dictionary changed size during iteration` when one thread's
# eviction (`next(iter(...))`/`pop`) raced another thread's read/write.
# `OrderedDict` + `move_to_end`/`popitem(last=False)` gives cheap LRU
# eviction once every mutation happens under the lock (mirrors the
# containment TTL cache in `join_containment.py`).
#
# Entries are stored as `tuple[Mapping[str, object], ...]` (each edge a
# `MappingProxyType`), not `list[dict]`: a plain list/dict is a shared
# mutable object handed out to every caller, so one caller's in-place
# mutation (e.g. `.append`/`edge["x"] = ...`) would silently corrupt the
# cache for every other reader of the same `version_key`. Neither of the
# three functions that consume edges (`_fk_anchor_link_counts`,
# `_rank_catalog_candidates`, `code_label_cascade._fk_lookup_candidates`)
# takes this lock, so a plain (non-reentrant) `Lock` is sufficient.
_RELATIONSHIP_EDGES_CACHE_MAXSIZE = 8
_RELATIONSHIP_EDGES_CACHE: "OrderedDict[str, tuple[Mapping[str, object], ...]]" = OrderedDict()
_RELATIONSHIP_EDGES_CACHE_LOCK = threading.Lock()
_RELATIONSHIP_EDGES_CACHE_MISS = object()


def _relationship_edges_cache_get(version_key: str) -> object:
    with _RELATIONSHIP_EDGES_CACHE_LOCK:
        edges = _RELATIONSHIP_EDGES_CACHE.get(version_key, _RELATIONSHIP_EDGES_CACHE_MISS)
        if edges is not _RELATIONSHIP_EDGES_CACHE_MISS:
            _RELATIONSHIP_EDGES_CACHE.move_to_end(version_key)
        return edges


def _relationship_edges_cache_put(
    version_key: str, edges: tuple[Mapping[str, object], ...]
) -> None:
    with _RELATIONSHIP_EDGES_CACHE_LOCK:
        _RELATIONSHIP_EDGES_CACHE[version_key] = edges
        _RELATIONSHIP_EDGES_CACHE.move_to_end(version_key)
        while len(_RELATIONSHIP_EDGES_CACHE) > _RELATIONSHIP_EDGES_CACHE_MAXSIZE:
            _RELATIONSHIP_EDGES_CACHE.popitem(last=False)


def _relationship_edges_from_cache(
    schema: Mapping[str, object],
    version_key: str,
) -> tuple[Mapping[str, object], ...]:
    cached = _relationship_edges_cache_get(version_key)
    if cached is not _RELATIONSHIP_EDGES_CACHE_MISS:
        return cached
    # Computed outside the lock: `_relationship_edges` is O(tables^2) and
    # must never hold `_RELATIONSHIP_EDGES_CACHE_LOCK` while running, or
    # concurrent lookups for *other* version_keys would serialize behind it.
    # Two threads racing the same miss may both compute once; the second
    # `_relationship_edges_cache_put` just overwrites with an equal value.
    # Frozen once here (not inside `_relationship_edges_cache_put`) so the
    # cache-miss return value is exactly what gets cached: no mutable list
    # ever escapes to a caller, on either the hit or the miss path.
    edges = tuple(_freeze_relationship_edge(edge) for edge in _relationship_edges(schema))
    _relationship_edges_cache_put(version_key, edges)
    return edges


def _freeze_relationship_edge(edge: Mapping[str, object]) -> Mapping[str, object]:
    """Deep-freeze one edge: ``column_pairs`` is a nested list of dicts, so a
    top-level ``MappingProxyType`` alone would still let a consumer mutate
    the shared cache through ``edge["column_pairs"].append(...)``."""
    frozen = dict(edge)
    pairs = frozen.get("column_pairs")
    if isinstance(pairs, list):
        frozen["column_pairs"] = tuple(
            MappingProxyType(dict(pair)) if isinstance(pair, Mapping) else pair
            for pair in pairs
        )
    return MappingProxyType(frozen)


def _cached_relationship_edges(
    schema: Mapping[str, object],
    runtime: SchemaProbeRuntime,
) -> tuple[Mapping[str, object], ...]:
    return _relationship_edges_from_cache(schema, runtime.namespace.version_key)


def relationship_edges_cached(
    schema: Mapping[str, object],
    version_key: str,
) -> tuple[Mapping[str, object], ...]:
    """Public wrapper over the same bounded relationship-edges cache used by
    schema probes (`_cached_relationship_edges`), so callers outside this
    module (e.g. the code<->label cascade hint) share one cache instead of
    each paying for their own recomputation of `_relationship_edges` per
    research iteration.
    """
    if not isinstance(version_key, str) or not version_key:
        raise TypeError("version_key must be a non-empty str")
    return _relationship_edges_from_cache(schema, version_key)


def _declared_relationship(
    source_table: str,
    constraint: Mapping[str, object],
    table_body: Mapping[str, object],
) -> dict[str, object]:
    pairs = [dict(pair) for pair in constraint["column_pairs"]]
    edge: dict[str, object] = {
        "column_pairs": pairs,
        "constraint_id": constraint["constraint_id"],
        "from_table": source_table,
        "metadata_format": ("constraint" if "foreign_keys" in table_body else "column"),
        "relationship_kind": "declared",
        "to_table": constraint["to_table"],
    }
    edge["relationship_id"] = canonical_digest(edge)
    return edge


def _bounded_edges(
    root: str,
    edges: list[dict[str, object]],
    depth: int,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    adjacency: dict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in edges:
        adjacency[str(edge["from_table"])].append(edge)
        adjacency[str(edge["to_table"])].append(edge)
    distances = {root: 0}
    queue = deque([root])
    while queue:
        node = queue.popleft()
        if distances[node] >= depth:
            continue
        for edge in adjacency[node]:
            other = _other_table(edge, node)
            if other not in distances:
                distances[other] = distances[node] + 1
                queue.append(other)
    visible = [
        edge
        for edge in edges
        if min(
            distances.get(str(edge["from_table"]), depth + 1),
            distances.get(str(edge["to_table"]), depth + 1),
        )
        < depth
    ]
    return visible, distances


def _bounded_paths(
    root: str,
    edges: list[dict[str, object]],
    depth: int,
) -> dict[str, list[list[str]]]:
    """Keep two deterministic simple paths per table for ambiguity detection.

    Each non-root table enters the queue at most twice, so edge scans remain
    bounded by the schema graph rather than by its number of simple paths.
    """
    adjacency: dict[str, list[dict[str, object]]] = defaultdict(list)
    for edge in edges:
        adjacency[str(edge["from_table"])].append(edge)
        adjacency[str(edge["to_table"])].append(edge)
    for values in adjacency.values():
        values.sort(key=lambda edge: str(edge["relationship_id"]))

    found: dict[str, list[list[str]]] = defaultdict(list)
    queue: deque[tuple[str, tuple[str, ...], tuple[str, ...]]] = deque(
        [(root, (root,), ())]
    )
    while queue:
        node, visited, path = queue.popleft()
        if len(path) >= depth:
            continue
        for edge in adjacency[node]:
            other = _other_table(edge, node)
            if other in visited:
                continue
            next_path = path + (str(edge["relationship_id"]),)
            table_paths = found[other]
            candidate_path = list(next_path)
            if (
                candidate_path in table_paths
                or len(table_paths) >= MAX_AMBIGUITY_PATHS_PER_TABLE
            ):
                continue
            table_paths.append(candidate_path)
            queue.append((other, visited + (other,), next_path))
    return dict(found)


def _with_containment(
    edge: dict[str, object],
    runtime: SchemaProbeRuntime,
) -> dict[str, object]:
    result = dict(edge)
    if runtime.containment_sample_size is None:
        result["containment"] = None
        return result
    pairs = edge["column_pairs"]
    if len(pairs) != 1:
        result["containment"] = {"status": "not_applicable_composite"}
        return result
    runtime.deadline.require_remaining("relationship containment")
    probe = runtime.containment_probe
    if probe is None:
        from ..join_containment import estimate_join_containment

        probe = estimate_join_containment
    pair = pairs[0]
    value = probe(
        runtime.dsn,
        edge["from_table"],
        pair["from_column"],
        edge["to_table"],
        pair["to_column"],
        sample_size=runtime.containment_sample_size,
    )
    runtime.deadline.require_remaining("relationship containment result")
    if value is None:
        raise _SchemaProbeFailure(
            "containment_unavailable",
            "relationship containment could not be established",
        )
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= 1
    ):
        raise _SchemaProbeFailure(
            "containment_invalid",
            "relationship containment returned an invalid signal",
        )
    result["containment"] = {
        "sample_size": runtime.containment_sample_size,
        "status": "observed",
        "value": float(value),
    }
    return result


def _require_composite_capability(
    edges: list[dict[str, object]],
    capabilities: DatabaseCapabilities,
) -> None:
    if any(len(edge["column_pairs"]) > 1 for edge in edges) and (
        capabilities.composite_fk_introspection.state is not CapabilityState.SUPPORTED
    ):
        raise _SchemaProbeFailure(
            "composite_fk_introspection_unavailable",
            "database plugin cannot prove composite foreign-key ordering",
        )


def _payload(runtime: SchemaProbeRuntime, **values: object) -> dict[str, object]:
    return {
        "schema_namespace_version": _schema_namespace_version(runtime.namespace),
        **values,
    }


def _table_ref(table_name: str, runtime: SchemaProbeRuntime) -> TableRef:
    qualifier, separator, table = table_name.rpartition(".")
    return TableRef(
        namespace=runtime.table_namespace,
        schema=qualifier if separator else None,
        table=table if separator else table_name,
    )


def _column_ref(
    table_name: str,
    column_name: str,
    runtime: SchemaProbeRuntime,
) -> ColumnRef:
    return ColumnRef(
        table=_table_ref(table_name, runtime),
        column=column_name,
    )


def _metadata_text(value: object) -> str:
    return "" if value is None else str(value)


def _schema_namespace_version(namespace: SchemaNamespace) -> str:
    return f"sha256:{namespace.version_key}"


def _text_match_score(query: str, value: str) -> int:
    normalized = value.strip().casefold()
    if not query or not normalized:
        return 0
    if normalized == query or normalized.rsplit(".", 1)[-1] == query:
        return 4
    if normalized.startswith(query) or normalized.rsplit(".", 1)[-1].startswith(query):
        return 3
    if query in normalized:
        return 2
    terms = [term for term in re.split(r"[^a-z0-9]+", query) if term]
    return 1 if terms and all(term in normalized for term in terms) else 0


def _dense_rank(scored_names: list[tuple[int, str]]) -> dict[str, int]:
    """Rank ``(score, name)`` pairs with ties sharing one rank (1 = best).

    Equal scores must resolve to the exact same rank so that Reciprocal
    Rank Fusion preserves an exact tie between tables that only match a
    single, identical-scoring signal (see the pinned F05 ambiguity test).
    """
    ordered = sorted(
        scored_names, key=lambda item: (-item[0], item[1].casefold(), item[1])
    )
    ranks: dict[str, int] = {}
    rank = 0
    previous_score: int | None = None
    for score, name in ordered:
        if score != previous_score:
            rank += 1
            previous_score = score
        ranks[name] = rank
    return ranks


def _fk_anchor_link_counts(
    top_tables: list[str],
    relationship_edges: Sequence[Mapping[str, object]],
    lexical_candidate_names: set[str],
) -> dict[str, int]:
    """Count, per table, how many FK edges connect it to ``top_tables``.

    Only tables that are already lexical candidates are eligible: a table
    with zero text-match score never becomes a result purely through FK
    adjacency. The raw count (not a rank) is returned because it is only
    ever used as a tie-break value, never summed into the lexical RRF — see
    ``_rank_catalog_candidates``.
    """
    top_set = set(top_tables)
    link_counts: dict[str, int] = {}
    for edge in relationship_edges:
        from_table = str(edge["from_table"])
        to_table = str(edge["to_table"])
        for anchor, neighbor in ((from_table, to_table), (to_table, from_table)):
            if (
                anchor in top_set
                and neighbor != anchor
                and neighbor in lexical_candidate_names
            ):
                link_counts[neighbor] = link_counts.get(neighbor, 0) + 1
    return link_counts


def _reciprocal_rank_fusion(*rank_maps: dict[str, int]) -> dict[str, Fraction]:
    """Fuse independent rank maps with RRF: score(t) = sum 1 / (RRF_K + rank)."""
    fused: dict[str, Fraction] = {}
    for ranks in rank_maps:
        for name, rank in ranks.items():
            fused[name] = fused.get(name, Fraction(0)) + Fraction(1, RRF_K + rank)
    return fused


def _rank_catalog_candidates(
    candidates: list[tuple[int, str, list[str], list[str]]],
    name_scores: dict[str, int],
    description_scores: dict[str, int],
    column_scores: dict[str, int],
    edges: Sequence[Mapping[str, object]],
) -> tuple[list[tuple[int, str, list[str], list[str]]], bool]:
    """Rank schema-catalog candidates; report whether the top rank ties.

    Only the three lexical signals (table name / description / column) are
    fused into ``lexical_rrf`` via Reciprocal Rank Fusion. The FK-adjacency
    signal is kept out of that sum on purpose: it is applied afterwards as a
    pure tie-break, active only among candidates whose ``lexical_rrf`` is
    *exactly* equal (``Fraction`` equality). This guarantees a structurally
    adjacent table can never outrank a table with a strictly stronger
    lexical match — it can only decide between candidates that are
    otherwise indistinguishable on lexical grounds.

    The result is "ambiguous" (second element ``True``) when two or more
    top-ranked candidates still share both an equal ``lexical_rrf`` and an
    equal FK tie-break value: a difference in either one is enough to
    resolve the tie deterministically.
    """
    if not candidates:
        return [], False

    legacy_order = sorted(
        candidates, key=lambda item: (-item[0], item[1].casefold(), item[1])
    )
    lexical_candidate_names = {item[1] for item in candidates}
    top_lexical_names = [
        item[1] for item in legacy_order[:FK_ANCHOR_CANDIDATE_COUNT]
    ]
    fk_link_counts = _fk_anchor_link_counts(
        top_lexical_names, edges, lexical_candidate_names
    )
    lexical_rrf = _reciprocal_rank_fusion(
        *(
            _dense_rank([(score, name) for name, score in scores.items()])
            for scores in (name_scores, description_scores, column_scores)
        )
    )
    ranked_candidates = sorted(
        candidates,
        key=lambda item: (
            -lexical_rrf[item[1]],
            -fk_link_counts.get(item[1], 0),
            item[1].casefold(),
            item[1],
        ),
    )
    top_name = ranked_candidates[0][1]
    top_tiebreak = (lexical_rrf[top_name], fk_link_counts.get(top_name, 0))
    tie_count = sum(
        1
        for item in ranked_candidates
        if (lexical_rrf[item[1]], fk_link_counts.get(item[1], 0)) == top_tiebreak
    )
    return ranked_candidates, tie_count > 1


def _other_table(edge: Mapping[str, object], table: str) -> str:
    left = str(edge["from_table"])
    return str(edge["to_table"]) if left == table else left


def _clock_ns(clock: Callable[[], int]) -> int:
    value = clock()
    if type(value) is not int or value < 0:
        raise TypeError("schema probe monotonic_ns must return a non-negative integer")
    return value


def _cost(
    started_ns: int,
    clock: Callable[[], int],
    *,
    rows: int = 0,
    bytes_: int = 0,
) -> EvidenceCost:
    completed_ns = _clock_ns(clock)
    if completed_ns < started_ns:
        raise ValueError("schema probe monotonic clock moved backwards")
    return EvidenceCost(
        wall_clock_ms=0,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=(completed_ns - started_ns + 999_999) // 1_000_000,
        rows=rows,
        bytes=bytes_,
    )


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise TypeError("schema probe utc_now must return a UTC datetime")
    return value


__all__ = [
    "MAX_SCHEMA_PROBE_DEPTH",
    "MAX_SCHEMA_PROBE_TOP_K",
    "SchemaEvidenceDocument",
    "SchemaProbeBudgetRuntime",
    "SchemaProbeRuntime",
    "inspect_column",
    "inspect_relationships",
    "inspect_table",
    "read_schema_evidence",
    "relationship_edges_cached",
    "search_schema_catalog",
]
