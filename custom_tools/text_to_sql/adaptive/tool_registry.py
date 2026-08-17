"""Lazy allowlisted registry for adaptive research probes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal, Protocol, TypeAlias, cast

from pydantic import Field, ValidationError, field_validator

from .controller import JsonValue, NormalizedToolResult, ToolAdapter, ToolInvocation
from .models import (
    ColumnRef,
    DocumentRef,
    ResearchActionKind,
    StrictModel,
    TableRef,
    TargetRef,
)
from .research_tool_contracts import (
    ExecuteResearchProbeArguments,
    GetDistinctValuesArguments,
    InspectColumnArguments,
    InspectRelationshipsArguments,
    InspectTableArguments,
    MAX_RESEARCH_PARAMETERS,
    MAX_RESEARCH_PARAMETER_STRING_CHARS,
    MAX_SEARCH_VALUE_STRING_CHARS,
    ProfileColumnArguments,
    ReadSchemaEvidenceArguments,
    SampleRowsArguments,
    SearchSchemaCatalogArguments,
    SearchValueArguments,
    _ColumnRequest,
    _ColumnTopKRequest,
    _DocumentRequest,
    _Parameters,
    _RawResearchRequest,
    _RelationshipsRequest,
    _RequestModel,
    _SampleRowsRequest,
    _SearchValueRequest,
    _TableRequest,
    _TableTopKRequest,
)


ADAPTIVE_RESEARCH_TOOL_NAMES = (
    "search_schema_catalog",
    "inspect_table",
    "inspect_column",
    "inspect_relationships",
    "profile_column",
    "sample_rows",
    "search_value",
    "get_distinct_values",
    "execute_research_probe",
    "read_schema_evidence",
)
ADAPTIVE_RESEARCH_DEFINITIONS_DIR = Path(__file__).with_name(
    "research_tool_definitions"
)
_BudgetFactory: TypeAlias = Callable[
    [ResearchActionKind, TargetRef, _Parameters], object
]
_OwnedAdapterMethods: TypeAlias = tuple[
    Callable[[ToolInvocation], NormalizedToolResult],
    Callable[[ToolInvocation], NormalizedToolResult | None],
]


@dataclass(frozen=True, slots=True)
class AdaptiveResearchToolContext:
    """Trusted runtimes and the one existing per-call budget seam."""

    schema_runtime: object
    data_runtime: object
    budget_factory: _BudgetFactory

    def __post_init__(self) -> None:
        if not callable(self.budget_factory):
            raise TypeError("budget_factory must be callable")


@dataclass(frozen=True, slots=True)
class ResolvedResearchToolClaim:
    """One request revalidated against the existing registry claim logic."""

    kind: ResearchActionKind
    target: TargetRef
    parameters: _Parameters
    arguments: dict[str, JsonValue]


_REQUEST_MODELS: Mapping[str, type[_RequestModel]] = {
    "search_schema_catalog": _TableTopKRequest,
    "inspect_table": _TableRequest,
    "inspect_column": _ColumnRequest,
    "inspect_relationships": _RelationshipsRequest,
    "profile_column": _ColumnRequest,
    "sample_rows": _SampleRowsRequest,
    "search_value": _SearchValueRequest,
    "get_distinct_values": _ColumnTopKRequest,
    "execute_research_probe": _RawResearchRequest,
    "read_schema_evidence": _DocumentRequest,
}

_ARGUMENT_CONTRACTS: Mapping[str, tuple[tuple[str, str, bool], ...]] = {
    "search_schema_catalog": (("query", "string", True), ("top_k", "integer", True)),
    "inspect_table": (("table", "string", True),),
    "inspect_column": (("table", "string", True), ("column", "string", True)),
    "inspect_relationships": (
        ("table", "string", True),
        ("top_k", "integer", True),
        ("depth", "integer", True),
    ),
    "profile_column": (("table", "string", True), ("column", "string", True)),
    "sample_rows": (
        ("table", "string", True),
        ("columns", "string_array", True),
        ("limit", "integer", True),
    ),
    "search_value": (
        ("table", "string", True),
        ("column", "string", True),
        ("value", "json_scalar", True),
        ("top_k", "integer", True),
    ),
    "get_distinct_values": (
        ("table", "string", True),
        ("column", "string", True),
        ("top_k", "integer", True),
    ),
    "execute_research_probe": (
        ("sql", "string", True),
        ("parameters", "json_scalar_array", False),
    ),
    "read_schema_evidence": (("document_id", "string", True),),
}


class _ArgumentDefinition(StrictModel):
    name: str
    type: Literal[
        "integer",
        "string",
        "string_array",
        "json_scalar",
        "json_scalar_array",
    ]
    required: bool


class _ToolDefinition(StrictModel):
    version: Literal[1]
    name: str
    description: Annotated[str, Field(min_length=1, max_length=1_000)]
    implementation: str
    arguments: tuple[_ArgumentDefinition, ...]
    additional_properties: Literal[False]

    @field_validator("arguments", mode="before")
    @classmethod
    def decode_yaml_arguments(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        return value


class _ProbeCallable(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class _AdapterSpec:
    request_model: type[_RequestModel]
    execute: _ProbeCallable


class _AdaptiveProbeAdapter:
    __slots__ = ("_name", "_context", "_spec")

    def __init__(
        self,
        *,
        name: str,
        context: AdaptiveResearchToolContext,
        spec: _AdapterSpec,
    ) -> None:
        self._name = name
        self._context = context
        self._spec = spec

    def execute(self, invocation: ToolInvocation) -> NormalizedToolResult:
        request, target, runtime, budget = self._prepare(invocation)
        result = _execute_probe(
            self._name,
            self._spec.execute,
            request,
            target=target,
            runtime=runtime,
            budget=budget,
        )
        return _normalize_probe_result(result)

    def recover(self, invocation: ToolInvocation) -> NormalizedToolResult | None:
        _, _, _, budget = self._prepare(invocation)
        from .policy import recover_probe_with_budget

        result = recover_probe_with_budget(
            budget.state,
            budget.action,
            budget.maximum_cost,
            expected_invocation_id=invocation.invocation_id,
            config=budget.config,
            ledger=budget.ledger,
        )
        if result is None:
            return None
        if result.invocation_id != invocation.invocation_id:
            raise ValueError("recovered result does not match the exact invocation")
        return _normalize_probe_result(result)

    def _prepare(
        self,
        invocation: ToolInvocation,
    ) -> tuple[_RequestModel, TargetRef, object, object]:
        if not isinstance(invocation, ToolInvocation):
            raise TypeError("invocation must be ToolInvocation")
        if invocation.tool_call.tool_name != self._name:
            raise ValueError("tool invocation name does not match its resolved adapter")
        request = self._spec.request_model.model_validate(
            invocation.tool_call.arguments,
            strict=True,
        )
        kind, target, parameters = _claim_for_request(
            self._name,
            request,
            self._context,
        )
        budget = self._context.budget_factory(kind, target, parameters)
        _validate_budget_for_invocation(
            budget,
            invocation,
            kind,
            target,
            parameters,
        )
        runtime = (
            self._context.schema_runtime
            if self._name in _SCHEMA_TOOL_NAMES
            else self._context.data_runtime
        )
        return request, target, runtime, budget


_ADAPTER_EXECUTE_IMPLEMENTATION = _AdaptiveProbeAdapter.execute
_ADAPTER_RECOVER_IMPLEMENTATION = _AdaptiveProbeAdapter.recover


class AdaptiveResearchToolRegistry:
    """Resolve only the local ten-tool allowlist, loading it on first use."""

    def __init__(self, context: AdaptiveResearchToolContext) -> None:
        if not isinstance(context, AdaptiveResearchToolContext):
            raise TypeError("context must be AdaptiveResearchToolContext")
        self._context = context
        self._definitions: dict[str, _ToolDefinition] | None = None
        self._adapters: dict[str, ToolAdapter] = {}
        self._issued_adapters: dict[str, _AdaptiveProbeAdapter] = {}
        self._lock = Lock()

    @property
    def names(self) -> tuple[str, ...]:
        return ADAPTIVE_RESEARCH_TOOL_NAMES

    @property
    def context(self) -> AdaptiveResearchToolContext:
        return self._context

    @property
    def cached_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(name for name in self.names if name in self._adapters)

    def description(self, tool_name: str) -> str | None:
        if tool_name not in ADAPTIVE_RESEARCH_TOOL_NAMES:
            return None
        definitions = self._definitions_or_load()
        return definitions[tool_name].description

    def resolve(self, tool_name: str) -> ToolAdapter | None:
        if tool_name not in ADAPTIVE_RESEARCH_TOOL_NAMES:
            return None
        with self._lock:
            cached = self._adapters.get(tool_name)
            if cached is not None:
                return cached
            definitions = self._definitions_or_load_locked()
            definition = definitions[tool_name]
            if definition.implementation != tool_name:
                raise ValueError("adaptive tool implementation is not allowlisted")
            adapter = _AdaptiveProbeAdapter(
                name=tool_name,
                context=self._context,
                spec=_build_spec(tool_name),
            )
            self._adapters[tool_name] = adapter
            self._issued_adapters[tool_name] = adapter
            return adapter

    def owns_resolved_adapter(self, tool_name: str, adapter: object) -> bool:
        """Return whether ``adapter`` is this registry's exact cached adapter."""

        if tool_name not in ADAPTIVE_RESEARCH_TOOL_NAMES:
            return False
        expected_spec = _build_spec(tool_name)
        with self._lock:
            return (
                self._owned_adapter_methods_locked(
                    tool_name,
                    adapter,
                    expected_spec,
                )
                is not None
            )

    def bind_owned_adapter_methods(
        self,
        tool_name: str,
        adapter: object,
    ) -> _OwnedAdapterMethods | None:
        """Return the already-validated original methods for one owned adapter."""

        if tool_name not in ADAPTIVE_RESEARCH_TOOL_NAMES:
            return None
        expected_spec = _build_spec(tool_name)
        with self._lock:
            return self._owned_adapter_methods_locked(
                tool_name,
                adapter,
                expected_spec,
            )

    def _owned_adapter_methods_locked(
        self,
        tool_name: str,
        adapter: object,
        expected_spec: _AdapterSpec,
    ) -> _OwnedAdapterMethods | None:
        if type(adapter) is not _AdaptiveProbeAdapter:
            return None
        adapter_type = type(adapter)
        execute = _ADAPTER_EXECUTE_IMPLEMENTATION.__get__(adapter, adapter_type)
        recover = _ADAPTER_RECOVER_IMPLEMENTATION.__get__(adapter, adapter_type)
        if (
            self._adapters.get(tool_name) is not adapter
            or self._issued_adapters.get(tool_name) is not adapter
            or adapter._name != tool_name
            or adapter._context is not self._context
            or type(adapter._spec) is not _AdapterSpec
            or adapter._spec.request_model is not expected_spec.request_model
            or adapter._spec.execute is not expected_spec.execute
            or adapter_type.__dict__.get("execute")
            is not _ADAPTER_EXECUTE_IMPLEMENTATION
            or adapter_type.__dict__.get("recover")
            is not _ADAPTER_RECOVER_IMPLEMENTATION
            or execute.__self__ is not adapter
            or execute.__func__ is not _ADAPTER_EXECUTE_IMPLEMENTATION
            or recover.__self__ is not adapter
            or recover.__func__ is not _ADAPTER_RECOVER_IMPLEMENTATION
        ):
            return None
        return execute, recover

    def _definitions_or_load(self) -> dict[str, _ToolDefinition]:
        with self._lock:
            return self._definitions_or_load_locked()

    def _definitions_or_load_locked(self) -> dict[str, _ToolDefinition]:
        if self._definitions is None:
            self._definitions = _load_definitions()
        return self._definitions


_SCHEMA_TOOL_NAMES = frozenset(
    {
        "search_schema_catalog",
        "inspect_table",
        "inspect_column",
        "inspect_relationships",
        "read_schema_evidence",
    }
)


def _load_definitions() -> dict[str, _ToolDefinition]:
    import yaml

    expected = set(ADAPTIVE_RESEARCH_TOOL_NAMES)
    paths = sorted(ADAPTIVE_RESEARCH_DEFINITIONS_DIR.glob("*.yaml"))
    if {path.stem for path in paths} != expected:
        raise ValueError("adaptive research YAML files do not match the allowlist")
    definitions: dict[str, _ToolDefinition] = {}
    for path in paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            definition = _ToolDefinition.model_validate(document, strict=True)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise ValueError(
                f"invalid adaptive research definition: {path.name}"
            ) from exc
        if definition.name != path.stem or definition.name not in expected:
            raise ValueError("adaptive research definition name is not allowlisted")
        if (
            definition.implementation != definition.name
            or "." in definition.implementation
        ):
            raise ValueError(
                "adaptive research definition implementation is not allowlisted"
            )
        actual_arguments = tuple(
            (argument.name, argument.type, argument.required)
            for argument in definition.arguments
        )
        if actual_arguments != _ARGUMENT_CONTRACTS[definition.name]:
            raise ValueError(
                "adaptive research definition arguments do not match its schema"
            )
        if definition.name in definitions:
            raise ValueError("adaptive research definition names must be unique")
        definitions[definition.name] = definition
    return definitions


def _build_spec(name: str) -> _AdapterSpec:
    if name == "execute_research_probe":
        from .data_probes import execute_raw_research_query

        execute = execute_raw_research_query
    else:
        from . import probes

        implementation_name = (
            "get_distinct_values_probe" if name == "get_distinct_values" else name
        )
        execute = getattr(probes, implementation_name)
    return _AdapterSpec(request_model=_REQUEST_MODELS[name], execute=execute)


def _claim_for_request(
    name: str,
    request: _RequestModel,
    context: AdaptiveResearchToolContext,
) -> tuple[ResearchActionKind, TargetRef, _Parameters]:
    runtime = (
        context.schema_runtime if name in _SCHEMA_TOOL_NAMES else context.data_runtime
    )
    namespace = _trusted_table_namespace(runtime)
    if name == "search_schema_catalog":
        checked = cast(_TableTopKRequest, request)
        target = TableRef(namespace=namespace, schema=None, table=checked.query)
        return ResearchActionKind.INSPECT_CATALOG, target, (("top_k", checked.top_k),)
    if name == "inspect_table":
        checked = cast(_TableRequest, request)
        target = _table_target(namespace, checked.table)
        return ResearchActionKind.INSPECT_TABLE, target, ()
    if name == "inspect_column":
        checked = cast(_ColumnRequest, request)
        target = _column_target(namespace, checked.table, checked.column)
        return ResearchActionKind.INSPECT_COLUMN, target, ()
    if name == "inspect_relationships":
        checked = cast(_RelationshipsRequest, request)
        target = _table_target(namespace, checked.table)
        return (
            ResearchActionKind.INSPECT_RELATIONSHIPS,
            target,
            (("depth", checked.depth), ("top_k", checked.top_k)),
        )
    if name == "profile_column":
        checked = cast(_ColumnRequest, request)
        target = _column_target(namespace, checked.table, checked.column)
        return ResearchActionKind.PROFILE_COLUMN, target, ()
    if name == "sample_rows":
        checked = cast(_SampleRowsRequest, request)
        target = _table_target(namespace, checked.table)
        parameters = tuple(
            (f"column_{index:03d}", column)
            for index, column in enumerate(checked.columns)
        ) + (("limit", checked.limit),)
        return ResearchActionKind.SAMPLE_ROWS, target, parameters
    if name == "search_value":
        checked = cast(_SearchValueRequest, request)
        target = _column_target(namespace, checked.table, checked.column)
        return (
            ResearchActionKind.SEARCH_VALUE,
            target,
            (("top_k", checked.top_k), ("value", checked.value)),
        )
    if name == "get_distinct_values":
        checked = cast(_ColumnTopKRequest, request)
        target = _column_target(namespace, checked.table, checked.column)
        return ResearchActionKind.DISTINCT_VALUES, target, (("top_k", checked.top_k),)
    if name == "read_schema_evidence":
        checked = cast(_DocumentRequest, request)
        _require_allowlisted_document(
            context.schema_runtime, checked.document_id, namespace
        )
        target = DocumentRef(document_id=checked.document_id, namespace=namespace)
        return ResearchActionKind.READ_DOCUMENT, target, ()
    if name == "execute_research_probe":
        return _raw_claim(cast(_RawResearchRequest, request), context.data_runtime)
    raise AssertionError("unreachable adaptive research tool")


def _raw_claim(
    request: _RawResearchRequest,
    data_runtime: object,
) -> tuple[ResearchActionKind, TargetRef, _Parameters]:
    from .research_query import (
        RawResearchQuery,
        derive_research_query_identity,
        dialect_for_plugin,
    )

    dsn = getattr(data_runtime, "dsn", None)
    get_plugin = getattr(data_runtime, "get_plugin", None)
    namespace = getattr(data_runtime, "table_namespace", None)
    schema_namespace = getattr(data_runtime, "namespace", None)
    version_key = getattr(schema_namespace, "version_key", None)
    if (
        type(dsn) is not str
        or type(namespace) is not str
        or type(version_key) is not str
    ):
        raise TypeError("data_runtime lacks trusted raw research identity metadata")
    if get_plugin is None:
        from db_plugins import get_plugin as default_get_plugin

        get_plugin = default_get_plugin
    if not callable(get_plugin):
        raise TypeError("data_runtime get_plugin must be callable or null")
    schema_version = f"sha256:{version_key}"
    query = RawResearchQuery(sql=request.sql, parameters=request.parameters)
    identity = derive_research_query_identity(
        query,
        dialect=dialect_for_plugin(get_plugin(dsn)),
        namespace=namespace,
        schema_namespace_version=schema_version,
    )
    return ResearchActionKind.EXECUTE_PROBE, identity.target, identity.action_parameters


def _trusted_table_namespace(runtime: object) -> str:
    namespace = getattr(runtime, "table_namespace", None)
    if type(namespace) is not str or not namespace:
        raise TypeError("trusted runtime lacks table_namespace")
    return namespace


def _table_target(namespace: str, table: str) -> TableRef:
    qualifier, separator, table_name = table.rpartition(".")
    if separator:
        if not qualifier or not table_name:
            raise ValueError("qualified table must contain exact non-empty parts")
        return TableRef(namespace=namespace, schema=qualifier, table=table_name)
    return TableRef(namespace=namespace, schema=None, table=table)


def _column_target(namespace: str, table: str, column: str) -> ColumnRef:
    return ColumnRef(
        table=_table_target(namespace, table),
        column=column,
    )


def resolve_research_tool_claim(
    tool_name: str,
    arguments: Mapping[str, JsonValue],
    context: AdaptiveResearchToolContext,
) -> ResolvedResearchToolClaim:
    """Purely revalidate one allowlisted request and derive its trusted claim."""

    if tool_name not in ADAPTIVE_RESEARCH_TOOL_NAMES:
        raise ValueError("adaptive research tool is not allowlisted")
    if not isinstance(context, AdaptiveResearchToolContext):
        raise TypeError("context must be AdaptiveResearchToolContext")
    try:
        request = _REQUEST_MODELS[tool_name].model_validate(arguments, strict=True)
    except ValidationError as exc:
        raise ValueError("adaptive research tool arguments are invalid") from exc
    kind, target, parameters = _claim_for_request(tool_name, request, context)
    normalized = request.model_dump(mode="python", round_trip=True, warnings="error")
    return ResolvedResearchToolClaim(
        kind=kind,
        target=target,
        parameters=parameters,
        arguments=cast(dict[str, JsonValue], normalized),
    )


def _require_allowlisted_document(
    runtime: object,
    document_id: str,
    namespace: str,
) -> None:
    documents = getattr(runtime, "documents", None)
    if type(documents) is not tuple:
        raise TypeError("trusted schema runtime lacks allowlisted documents")
    matches = [
        document
        for document in documents
        if getattr(document, "document_id", None) == document_id
        and getattr(document, "namespace", None) == namespace
    ]
    if len(matches) != 1:
        raise ValueError("schema document is not uniquely allowlisted")


def _execute_probe(
    name: str,
    execute: _ProbeCallable,
    request: _RequestModel,
    *,
    target: TargetRef,
    runtime: object,
    budget: object,
) -> object:
    if name == "search_schema_catalog":
        checked = cast(_TableTopKRequest, request)
        return execute(target, checked.top_k, runtime=runtime, budget=budget)
    if name in {
        "inspect_table",
        "inspect_column",
        "profile_column",
        "read_schema_evidence",
    }:
        return execute(target, runtime=runtime, budget=budget)
    if name == "inspect_relationships":
        checked = cast(_RelationshipsRequest, request)
        return execute(
            target,
            checked.top_k,
            checked.depth,
            runtime=runtime,
            budget=budget,
        )
    if name == "sample_rows":
        checked = cast(_SampleRowsRequest, request)
        return execute(
            target,
            checked.columns,
            checked.limit,
            runtime=runtime,
            budget=budget,
        )
    if name == "search_value":
        checked = cast(_SearchValueRequest, request)
        return execute(
            target,
            checked.value,
            checked.top_k,
            runtime=runtime,
            budget=budget,
        )
    if name == "get_distinct_values":
        checked = cast(_ColumnTopKRequest, request)
        return execute(target, checked.top_k, runtime=runtime, budget=budget)
    if name == "execute_research_probe":
        from .research_query import RawResearchQuery

        checked = cast(_RawResearchRequest, request)
        return execute(
            RawResearchQuery(sql=checked.sql, parameters=checked.parameters),
            runtime=runtime,
            budget=budget,
        )
    raise AssertionError("unreachable adaptive research tool")


def _validate_budget_for_invocation(
    budget: object,
    invocation: ToolInvocation,
    kind: ResearchActionKind,
    target: TargetRef,
    parameters: _Parameters,
) -> None:
    state = getattr(budget, "state", None)
    action = getattr(budget, "action", None)
    if (
        getattr(state, "run_id", None) != invocation.run_id
        or getattr(state, "run_incarnation", None) != invocation.run_incarnation
        or getattr(state, "revision", None) != invocation.revision
        or getattr(budget, "invocation_id", None) != invocation.invocation_id
    ):
        raise ValueError("trusted budget does not match the exact tool invocation")
    if (
        getattr(action, "expected_revision", None) != invocation.revision
        or getattr(action, "kind", None) is not kind
        or getattr(action, "target", None) != target
        or getattr(action, "parameters", None) != parameters
    ):
        raise ValueError("trusted budget action does not match the derived claim")


def _normalize_probe_result(result: object) -> NormalizedToolResult:
    from .probes import ProbeResult, ProbeStatus

    if not isinstance(result, ProbeResult):
        raise TypeError("adaptive research tool must return ProbeResult")
    status: Literal["success", "error"] = (
        "success" if result.status is ProbeStatus.SUCCESS else "error"
    )
    value = _normalized_json(result.model_dump(mode="json", by_alias=True))
    return NormalizedToolResult(status, cast(JsonValue, value))


def _normalized_json(value: object) -> JsonValue:
    if value is None or type(value) in {bool, int, str}:
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise TypeError("probe result contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("probe result keys must be text")
        return {key: _normalized_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return tuple(_normalized_json(item) for item in value)
    raise TypeError("probe result must contain only JSON values")


__all__ = [
    "ADAPTIVE_RESEARCH_DEFINITIONS_DIR",
    "ADAPTIVE_RESEARCH_TOOL_NAMES",
    "MAX_RESEARCH_PARAMETERS",
    "MAX_RESEARCH_PARAMETER_STRING_CHARS",
    "MAX_SEARCH_VALUE_STRING_CHARS",
    "ExecuteResearchProbeArguments",
    "GetDistinctValuesArguments",
    "InspectColumnArguments",
    "InspectRelationshipsArguments",
    "InspectTableArguments",
    "ProfileColumnArguments",
    "ReadSchemaEvidenceArguments",
    "SampleRowsArguments",
    "SearchSchemaCatalogArguments",
    "SearchValueArguments",
    "AdaptiveResearchToolContext",
    "AdaptiveResearchToolRegistry",
    "ResolvedResearchToolClaim",
    "resolve_research_tool_claim",
]
