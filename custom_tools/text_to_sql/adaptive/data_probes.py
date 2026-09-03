"""Internal bounded data probes over trusted SQL templates and schema scope."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math
import re
import time

from db_plugins.base import (
    DatabaseCapabilities,
    DatabaseCapabilityError,
    RequiredDatabaseCapabilities,
    validate_required_capabilities,
)
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

from ..core._db_exec import QueryExecutionRequest, QueryExecutor, QueryPurpose
from ..schema_metadata import is_pk
from ..schema_loader import LoadedSchema
from ..schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from ..utils import get_table_columns
from .models import (
    ColumnRef,
    EvidenceCost,
    QueryProbeRef,
    ResearchActionKind,
    TableRef,
    TargetRef,
)
from .policy import (
    BudgetReservation,
    ProbeExecutionFailure,
    execute_probe_with_budget,
)
from .probes import ProbeResult, ProbeStatus, build_probe_result
from .research_query import (
    RawResearchQuery,
    ResearchQueryAdmissionError,
    admit_research_query,
    dialect_for_plugin,
    research_query_action_parameters,
)
from .schema_probes import SchemaProbeBudgetRuntime, ScopedSchemaLoader
from .serialization import canonical_json_bytes


MAX_DATA_PROBE_TOP_K = 50
MAX_DATA_PROBE_COLUMNS = 20

DataProbeBudgetRuntime = SchemaProbeBudgetRuntime


@dataclass(frozen=True, slots=True)
class ResearchQueryTemplate:
    """Trusted generic query; model actions can name it but cannot supply SQL."""

    probe_id: str
    namespace: str
    schema_namespace_version: str
    sql: str
    output_columns: tuple[str, ...]
    row_limit: int
    deterministic: bool

    def __post_init__(self) -> None:
        if type(self.probe_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", self.probe_id
        ):
            raise ValueError("probe_id must be a canonical identifier")
        if type(self.namespace) is not str or not self.namespace:
            raise ValueError("namespace must be non-empty text")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.schema_namespace_version):
            raise ValueError("schema_namespace_version must be an exact sha256 digest")
        if type(self.sql) is not str or not self.sql.strip():
            raise ValueError("sql must be trusted non-empty text")
        _validate_columns(self.output_columns)
        _validate_row_bound(self.row_limit, "row_limit")
        if type(self.deterministic) is not bool:
            raise TypeError("deterministic must be a boolean")


@dataclass(frozen=True, slots=True)
class DataProbeRuntime:
    """Trusted DSN, scope, deadline, and templates for internal data reads."""

    loader: ScopedSchemaLoader
    dsn: str
    scope: SchemaScope
    namespace: SchemaNamespace
    table_namespace: str
    deadline: DeadlineBudget
    query_templates: tuple[ResearchQueryTemplate, ...] = ()
    get_plugin: Callable[[str], object] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
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
        if type(self.query_templates) is not tuple or not all(
            isinstance(template, ResearchQueryTemplate)
            for template in self.query_templates
        ):
            raise TypeError("query_templates must contain ResearchQueryTemplate values")
        probe_ids = [template.probe_id for template in self.query_templates]
        if len(probe_ids) != len(set(probe_ids)):
            raise ValueError("query template probe IDs must be unique")
        expected_version = _schema_namespace_version(self.namespace)
        if any(
            template.namespace != self.table_namespace
            or template.schema_namespace_version != expected_version
            for template in self.query_templates
        ):
            raise ValueError("query templates must match the trusted schema namespace")
        if self.get_plugin is not None and not callable(self.get_plugin):
            raise TypeError("get_plugin must be callable or null")
        if not callable(self.monotonic_ns) or not callable(self.utc_now):
            raise TypeError("data probe clocks must be callable")
        if self.loaded_schema is not None:
            _validate_loaded_schema_snapshot(
                self.loaded_schema,
                namespace=self.namespace,
                scope=self.scope,
            )


@dataclass(frozen=True, slots=True)
class _PreparedQuery:
    sql: str
    parameters: tuple[str | int | float | bool | None, ...]
    row_limit: int
    output_columns: tuple[str, ...]
    can_truncate: bool
    profile_sample_limit: int | None = None


class _DataProbeFailure(RuntimeError):
    def __init__(
        self,
        failure_code: str,
        summary: str,
    ) -> None:
        self.failure_code = failure_code
        self.summary = summary
        super().__init__(summary)


def profile_column(
    target: ColumnRef,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    """Profile one deterministic bounded sample from an exact column."""

    def prepare(schema: Mapping[str, object], plugin: object) -> _PreparedQuery:
        table_name, column_name, table_body = _resolve_column(target, schema)
        available = get_table_columns(table_body)
        primary_keys = sorted(
            (
                column
                for column, metadata in available.items()
                if isinstance(metadata, Mapping) and is_pk(dict(metadata))
            ),
            key=lambda value: (value.casefold(), value),
        )
        if not primary_keys:
            raise _DataProbeFailure(
                "deterministic_order_unavailable",
                "profile_column requires a declared primary key for stable sampling",
            )
        sample_limit = budget.config.per_action.sample_rows
        _validate_row_bound(sample_limit, "profile sample limit")
        table_sql = _quote_path(plugin, table_name)
        column_sql = _quote_part(plugin, column_name)
        order_sql = ", ".join(_quote_part(plugin, column) for column in primary_keys)
        return _PreparedQuery(
            sql=f"SELECT {column_sql} FROM {table_sql} ORDER BY {order_sql} LIMIT {sample_limit}",
            parameters=(),
            row_limit=sample_limit,
            output_columns=(column_name,),
            can_truncate=True,
            profile_sample_limit=sample_limit,
        )

    return _run_data_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.PROFILE_COLUMN,
        action_parameters=(),
        prepare=prepare,
    )


def sample_rows(
    target: TableRef,
    columns: tuple[str, ...],
    limit: int,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    """Sample explicit columns in primary-key order, never with SELECT star."""
    action_parameters = tuple(
        (f"column_{index:03d}", column) for index, column in enumerate(columns)
    ) + (("limit", limit),)

    def prepare(schema: Mapping[str, object], plugin: object) -> _PreparedQuery:
        try:
            _validate_columns(columns)
        except (TypeError, ValueError) as exc:
            raise _DataProbeFailure(
                "columns_out_of_bounds",
                "sample columns exceed their closed bound",
            ) from exc
        try:
            _validate_row_bound(limit, "limit")
        except ValueError as exc:
            raise _DataProbeFailure(
                "row_limit_exceeded",
                "sample row limit exceeds its closed bound",
            ) from exc
        table_name, table_body = _resolve_table(target, schema)
        available = get_table_columns(table_body)
        missing = [column for column in columns if column not in available]
        if missing:
            raise _DataProbeFailure(
                "column_not_found",
                "sample columns are not present in the exact target table",
            )
        primary_keys = sorted(
            (
                column
                for column, metadata in available.items()
                if isinstance(metadata, Mapping) and is_pk(dict(metadata))
            ),
            key=lambda value: (value.casefold(), value),
        )
        if not primary_keys:
            raise _DataProbeFailure(
                "deterministic_order_unavailable",
                "sample_rows requires a declared primary key for stable ordering",
            )
        selected_sql = ", ".join(_quote_part(plugin, column) for column in columns)
        order_sql = ", ".join(_quote_part(plugin, column) for column in primary_keys)
        return _PreparedQuery(
            sql=(
                f"SELECT {selected_sql} FROM {_quote_path(plugin, table_name)} "
                f"ORDER BY {order_sql} LIMIT {limit}"
            ),
            parameters=(),
            row_limit=limit,
            output_columns=columns,
            can_truncate=True,
        )

    return _run_data_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.SAMPLE_ROWS,
        action_parameters=action_parameters,
        prepare=prepare,
    )


def search_value(
    target: ColumnRef,
    value: str | int | float | bool | None,
    top_k: int,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    """Search one exact value using driver binding, never SQL interpolation."""
    _validate_json_scalar(value, "value")
    action_parameters = (("top_k", top_k), ("value", value))

    def prepare(schema: Mapping[str, object], plugin: object) -> _PreparedQuery:
        try:
            _validate_top_k(top_k)
        except ValueError as exc:
            raise _DataProbeFailure(
                "top_k_out_of_bounds",
                "search top_k exceeds its closed bound",
            ) from exc
        table_name, column_name, _ = _resolve_column(target, schema)
        column_sql = _quote_part(plugin, column_name)
        predicate = f"{column_sql} IS NULL" if value is None else f"{column_sql} = ?"
        parameters = () if value is None else (value,)
        return _PreparedQuery(
            sql=(
                f"SELECT DISTINCT {column_sql} FROM {_quote_path(plugin, table_name)} "
                f"WHERE {predicate} ORDER BY {column_sql} LIMIT {top_k}"
            ),
            parameters=parameters,
            row_limit=top_k,
            output_columns=(column_name,),
            can_truncate=False,
        )

    return _run_data_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.SEARCH_VALUE,
        action_parameters=action_parameters,
        prepare=prepare,
    )


def get_distinct_values_probe(
    target: ColumnRef,
    top_k: int,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    """Return typed distinct values without changing the legacy public helper."""
    action_parameters = (("top_k", top_k),)

    def prepare(schema: Mapping[str, object], plugin: object) -> _PreparedQuery:
        try:
            _validate_top_k(top_k)
        except ValueError as exc:
            raise _DataProbeFailure(
                "top_k_out_of_bounds",
                "distinct top_k exceeds its closed bound",
            ) from exc
        table_name, column_name, _ = _resolve_column(target, schema)
        column_sql = _quote_part(plugin, column_name)
        return _PreparedQuery(
            sql=(
                f"SELECT DISTINCT {column_sql} FROM {_quote_path(plugin, table_name)} "
                f"WHERE {column_sql} IS NOT NULL ORDER BY {column_sql} LIMIT {top_k}"
            ),
            parameters=(),
            row_limit=top_k,
            output_columns=(column_name,),
            can_truncate=True,
        )

    return _run_data_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.DISTINCT_VALUES,
        action_parameters=action_parameters,
        prepare=prepare,
    )


def execute_research_probe(
    target: QueryProbeRef,
    parameters: tuple[str | int | float | bool | None, ...],
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    """Execute one trusted generic template through QueryPurpose.RESEARCH."""
    if type(parameters) is not tuple:
        raise TypeError("parameters must be an immutable tuple")
    for index, value in enumerate(parameters):
        _validate_json_scalar(value, f"parameter_{index:03d}")
    action_parameters = tuple(
        (f"parameter_{index:03d}", value) for index, value in enumerate(parameters)
    )

    def prepare(_schema: Mapping[str, object], _plugin: object) -> _PreparedQuery:
        matches = [
            template
            for template in runtime.query_templates
            if template.probe_id == target.probe_id
        ]
        if len(matches) != 1 or target.namespace != runtime.table_namespace:
            raise _DataProbeFailure(
                "query_template_not_found",
                "trusted query template is unavailable in this namespace",
            )
        template = matches[0]
        if not template.deterministic:
            raise _DataProbeFailure(
                "deterministic_order_unavailable",
                "trusted query template does not guarantee deterministic output",
            )
        return _PreparedQuery(
            sql=template.sql,
            parameters=parameters,
            row_limit=template.row_limit,
            output_columns=template.output_columns,
            can_truncate=True,
        )

    return _run_data_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.EXECUTE_PROBE,
        action_parameters=action_parameters,
        prepare=prepare,
    )


def execute_raw_research_query(
    query: RawResearchQuery,
    *,
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> ProbeResult:
    """Admit model-authored SQL against the live scope, then use the one probe seam."""

    if not isinstance(budget, SchemaProbeBudgetRuntime):
        raise TypeError("budget must be DataProbeBudgetRuntime")
    action_parameters = research_query_action_parameters(query)
    target = budget.action.target

    def prepare(schema: Mapping[str, object], plugin: object) -> _PreparedQuery:
        try:
            admitted = admit_research_query(
                query,
                schema=schema,
                dialect=dialect_for_plugin(plugin),
                namespace=runtime.table_namespace,
                schema_namespace_version=_schema_namespace_version(runtime.namespace),
                maximum_row_limit=budget.maximum_cost.rows,
            )
        except ResearchQueryAdmissionError as exc:
            raise _DataProbeFailure(exc.failure_code, exc.summary) from exc
        if admitted.target != target:
            raise _DataProbeFailure(
                "action_mismatch",
                "raw research SQL does not match its canonical research action",
            )
        return _PreparedQuery(
            sql=admitted.normalized_sql,
            parameters=query.parameters,
            row_limit=admitted.row_limit,
            output_columns=admitted.output_columns,
            can_truncate=True,
        )

    return _run_data_probe(
        runtime,
        budget,
        target=target,
        kind=ResearchActionKind.EXECUTE_PROBE,
        action_parameters=action_parameters,
        prepare=prepare,
    )


def _run_data_probe(
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
    *,
    target: TargetRef,
    kind: ResearchActionKind,
    action_parameters: tuple[tuple[str, str | int | float | bool | None], ...],
    prepare: Callable[[Mapping[str, object], object], _PreparedQuery],
) -> ProbeResult:
    if not isinstance(runtime, DataProbeRuntime):
        raise TypeError("runtime must be DataProbeRuntime")
    if not isinstance(budget, SchemaProbeBudgetRuntime):
        raise TypeError("budget must be DataProbeBudgetRuntime")

    def execute(reservation: BudgetReservation) -> ProbeResult:
        started_ns = _clock_ns(runtime.monotonic_ns)
        try:
            _validate_call(runtime, budget, target, kind, action_parameters)
            runtime.deadline.require_remaining("data probe schema load")
            schema, plugin = _load_current_schema(runtime, budget)
            query = prepare(schema, plugin)
            if query.row_limit > reservation.maximum_cost.rows:
                raise _DataProbeFailure(
                    "row_limit_exceeded",
                    "data probe row limit exceeds its reserved bound",
                )
            runtime.deadline.require_remaining("data probe execution")
            result = QueryExecutor(get_plugin=lambda _dsn: plugin).execute(
                QueryExecutionRequest(
                    sql_query=query.sql,
                    purpose=QueryPurpose.RESEARCH,
                    row_limit=query.row_limit,
                    dsn=runtime.dsn,
                    deadline=runtime.deadline,
                    parameters=query.parameters,
                )
            )
            if not result.success:
                capability = result.outcome.get("capability_error")
                failure_code = (
                    "data_probe_capability_unavailable"
                    if isinstance(capability, Mapping)
                    else "research_query_failed"
                )
                raise _DataProbeFailure(
                    failure_code,
                    "research query did not produce an observation",
                )
            columns = tuple(result.columns)
            if columns != query.output_columns:
                raise _DataProbeFailure(
                    "result_shape_mismatch",
                    "research query columns do not match its trusted template",
                )
            rows = [list(row) for row in result.data]
            payload = {
                "columns": list(columns),
                "probe_kind": kind.value,
                "schema_namespace_version": reservation.schema_namespace_version,
                "target": target.model_dump(mode="json", by_alias=True),
            }
            if query.profile_sample_limit is None:
                payload["rows"] = rows
            else:
                payload["sampled_profile"] = _sampled_profile(
                    rows,
                    query.profile_sample_limit,
                )
            if kind is ResearchActionKind.SEARCH_VALUE:
                payload["requested_value"] = action_parameters[1][1]
            payload_bytes = canonical_json_bytes(payload)
            cost = _cost(
                started_ns,
                runtime.monotonic_ns,
                rows=len(rows),
                bytes_=len(payload_bytes),
            )
            if (
                cost.wall_clock_ms > reservation.maximum_cost.wall_clock_ms
                or cost.db_probe_ms > reservation.maximum_cost.db_probe_ms
                or cost.rows > reservation.maximum_cost.rows
                or cost.bytes > reservation.maximum_cost.bytes
            ):
                raise ProbeExecutionFailure(
                    status=ProbeStatus.FAILED,
                    actual_cost=cost,
                    failure_code="data_result_limit_exceeded",
                    summary="data probe result exceeded its reserved bound",
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
                summary=f"{kind.value} data observation",
                cost=cost,
                row_count=len(rows),
                truncated=query.can_truncate and len(rows) == query.row_limit,
                payload=payload,
            )
        except ProbeExecutionFailure:
            raise
        except WorkflowDeadlineExceeded as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.TIMED_OUT,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code="data_deadline_exceeded",
                summary="data probe deadline expired",
            ) from exc
        except _DataProbeFailure as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.FAILED,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code=exc.failure_code,
                summary=exc.summary,
            ) from exc
        except TimeoutError as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.TIMED_OUT,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code="data_probe_timed_out",
                summary="data probe timed out",
            ) from exc
        except Exception as exc:
            raise ProbeExecutionFailure(
                status=ProbeStatus.FAILED,
                actual_cost=_cost(started_ns, runtime.monotonic_ns),
                failure_code="data_probe_failed",
                summary="data probe failed",
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
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
    target: TargetRef,
    kind: ResearchActionKind,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...],
) -> None:
    if runtime.namespace.scope != runtime.scope:
        raise _DataProbeFailure(
            "data_scope_mismatch",
            "trusted data scope does not match its namespace",
        )
    target_namespace = (
        target.table.namespace if isinstance(target, ColumnRef) else target.namespace
    )
    if target_namespace != runtime.table_namespace:
        raise _DataProbeFailure(
            "target_namespace_mismatch",
            "data probe target is outside the trusted namespace",
        )
    if (
        budget.action.kind is not kind
        or budget.action.target != target
        or budget.action.parameters != parameters
    ):
        raise _DataProbeFailure(
            "action_mismatch",
            "data probe call does not match its canonical research action",
        )
    expected_version = _schema_namespace_version(runtime.namespace)
    if budget.state.schema_namespace_version != expected_version:
        raise _DataProbeFailure(
            "schema_namespace_mismatch",
            "research state does not match the trusted schema namespace",
        )


def _load_current_schema(
    runtime: DataProbeRuntime,
    budget: DataProbeBudgetRuntime,
) -> tuple[dict[str, object], object]:
    get_plugin = runtime.get_plugin
    if get_plugin is None:
        from db_plugins import get_plugin as default_get_plugin

        get_plugin = default_get_plugin
    plugin = get_plugin(runtime.dsn)
    capability_getter = getattr(plugin, "get_capabilities", None)
    if not callable(capability_getter):
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin does not declare capabilities",
        )
    capabilities = capability_getter(runtime.dsn)
    if not isinstance(capabilities, DatabaseCapabilities):
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin returned invalid capabilities",
        )
    try:
        validate_required_capabilities(
            capabilities,
            RequiredDatabaseCapabilities(
                read_only=False,
                statement_timeout=False,
                cancellation=False,
                introspection=True,
            ),
        )
    except DatabaseCapabilityError as exc:
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin cannot load the trusted schema",
        ) from exc
    try:
        loaded = runtime.loaded_schema
        if loaded is None:
            loaded = runtime.loader.load_scoped_schema({}, runtime.dsn, runtime.scope)
        _validate_loaded_schema_snapshot(
            loaded,
            namespace=runtime.namespace,
            scope=runtime.scope,
        )
    except (TypeError, ValueError) as exc:
        raise _DataProbeFailure(
            "schema_introspection_invalid",
            "scoped schema loader returned an invalid result",
        ) from exc
    schema = dict(loaded.schema)
    if (
        _schema_namespace_version(loaded.namespace)
        != budget.state.schema_namespace_version
    ):
        raise _DataProbeFailure(
            "schema_stale",
            "live schema does not match the admitted schema namespace",
        )
    return schema, plugin


def _validate_loaded_schema_snapshot(
    loaded: object,
    *,
    namespace: SchemaNamespace,
    scope: SchemaScope,
) -> LoadedSchema:
    if not isinstance(loaded, LoadedSchema):
        raise TypeError("loaded_schema must be LoadedSchema")
    if loaded.namespace != namespace or loaded.namespace.scope != scope:
        raise ValueError("loaded schema namespace does not match probe runtime")
    if canonical_schema_fingerprint(loaded.schema) != namespace.schema_fingerprint:
        raise ValueError("loaded schema fingerprint does not match probe runtime")
    return loaded


def _resolve_table(
    target: TableRef,
    schema: Mapping[str, object],
) -> tuple[str, Mapping[str, object]]:
    wanted = (
        f"{target.schema_name}.{target.table}"
        if target.schema_name is not None
        else target.table
    )
    matches = [
        table_name
        for table_name in schema
        if (
            table_name == wanted
            if target.schema_name is not None
            else table_name.rsplit(".", 1)[-1] == wanted
        )
    ]
    if not matches:
        raise _DataProbeFailure("table_not_found", "exact target table was not found")
    if len(matches) != 1:
        raise _DataProbeFailure(
            "table_ambiguous",
            "unqualified target table is ambiguous",
        )
    table_body = schema[matches[0]]
    if not isinstance(table_body, Mapping):
        raise _DataProbeFailure("schema_invalid", "table metadata is invalid")
    return matches[0], table_body


def _resolve_column(
    target: ColumnRef,
    schema: Mapping[str, object],
) -> tuple[str, str, Mapping[str, object]]:
    table_name, table_body = _resolve_table(target.table, schema)
    columns = get_table_columns(table_body)
    if target.column not in columns:
        raise _DataProbeFailure(
            "column_not_found",
            "exact target column was not found",
        )
    return table_name, target.column, table_body


def _quote_path(plugin: object, identifier: str) -> str:
    quote = getattr(plugin, "quote_identifier", None)
    if not callable(quote):
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin cannot quote identifiers",
        )
    quoted = quote(identifier)
    if type(quoted) is not str or not quoted:
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin returned an invalid quoted identifier",
        )
    return quoted


def _quote_part(plugin: object, identifier: str) -> str:
    quote = getattr(plugin, "quote_identifier_part", None)
    if not callable(quote):
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin cannot quote one identifier part",
        )
    quoted = quote(identifier)
    if type(quoted) is not str or not quoted:
        raise _DataProbeFailure(
            "data_capability_unavailable",
            "database plugin returned an invalid quoted identifier part",
        )
    return quoted


def _validate_columns(columns: tuple[str, ...]) -> None:
    if type(columns) is not tuple:
        raise TypeError("columns must be an immutable tuple")
    if not 1 <= len(columns) <= MAX_DATA_PROBE_COLUMNS:
        raise ValueError("columns exceed the closed data probe bound")
    if not all(type(column) is str and column for column in columns):
        raise TypeError("columns must contain non-empty exact strings")
    if len(columns) != len(set(columns)):
        raise ValueError("columns must be unique")


def _validate_row_bound(value: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_DATA_PROBE_TOP_K:
        raise ValueError(f"{name} exceeds the closed data probe bound")


def _validate_top_k(top_k: int) -> None:
    _validate_row_bound(top_k, "top_k")


def _validate_json_scalar(value: object, name: str) -> None:
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise TypeError(f"{name} must be an exact finite JSON scalar")


def _sampled_profile(rows: list[list[object]], sample_limit: int) -> dict[str, object]:
    values = [row[0] for row in rows]
    counts: dict[bytes, tuple[object, int]] = {}
    for value in values:
        key = canonical_json_bytes(value)
        previous = counts.get(key)
        counts[key] = (value, 1 if previous is None else previous[1] + 1)
    top_values = [
        {"count": count, "value": value}
        for key, (value, count) in sorted(
            counts.items(),
            key=lambda item: (-item[1][1], item[0]),
        )[:10]
    ]
    non_null = [value for value in values if value is not None]
    exact_types = {type(value) for value in non_null}
    if not non_null:
        minimum = maximum = None
        ordering = "empty"
    elif len(exact_types) == 1:
        minimum = min(non_null)
        maximum = max(non_null)
        ordering = "single_type"
    else:
        minimum = maximum = None
        ordering = "mixed_types"
    null_count = len(values) - len(non_null)
    return {
        "sample_count": len(values),
        "sample_limit": sample_limit,
        "sampled_cardinality": len(counts),
        "sampled_max": maximum,
        "sampled_min": minimum,
        "sampled_null_count": null_count,
        "sampled_null_ratio": null_count / len(values) if values else 0.0,
        "sampled_ordering": ordering,
        "sampled_top_values": top_values,
    }


def _schema_namespace_version(namespace: SchemaNamespace) -> str:
    return f"sha256:{namespace.version_key}"


def _clock_ns(clock: Callable[[], int]) -> int:
    value = clock()
    if type(value) is not int or value < 0:
        raise ValueError(
            "data probe monotonic clock must return a non-negative integer"
        )
    return value


def _cost(
    started_ns: int,
    clock: Callable[[], int],
    *,
    rows: int = 0,
    bytes_: int = 0,
) -> EvidenceCost:
    elapsed_ns = max(0, _clock_ns(clock) - started_ns)
    elapsed_ms = (elapsed_ns + 999_999) // 1_000_000 if elapsed_ns else 0
    return EvidenceCost(
        wall_clock_ms=elapsed_ms,
        model_calls=0,
        model_tokens=0,
        db_probe_ms=elapsed_ms,
        rows=rows,
        bytes=bytes_,
    )


def _utc(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("data probe UTC clock must return an aware datetime")
    return value.astimezone(UTC)
