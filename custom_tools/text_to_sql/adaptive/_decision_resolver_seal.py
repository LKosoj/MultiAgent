"""Transient trusted-runtime seal for one resolved research decision."""

from __future__ import annotations

from dataclasses import dataclass
from types import FunctionType

from workflow.deadline import DeadlineBudget

from ..schema_namespace import SchemaNamespace, SchemaScope
from .models import ColumnRef, TableRef
from .schema_probes import MAX_CONTAINMENT_SAMPLE_SIZE, SchemaEvidenceDocument
from .serialization import canonical_digest
from .tool_registry import AdaptiveResearchToolRegistry


class ExecutionSealError(ValueError):
    """Trusted execution context is invalid or no longer matches its seal."""


@dataclass(frozen=True, slots=True)
class CallableIdentity:
    kind: str
    owner_identity: int | None
    function_identity: int


@dataclass(frozen=True, slots=True)
class DeadlineSeal:
    identity: int
    deadline_monotonic: float
    deadline_at_ms: int
    monotonic: CallableIdentity
    wall_time: CallableIdentity


@dataclass(frozen=True, slots=True)
class DocumentSeal:
    identity: int
    document_id: str
    namespace: str
    schema_namespace_version: str
    source_version: str
    valid_until: str | None
    title_digest: str
    content_digest: str
    target_digest: str


@dataclass(frozen=True, slots=True)
class RuntimeSeal:
    identity: int
    table_namespace: str
    dsn_digest: str
    scope_identity: int
    scope_digest: str
    scope_key: str
    namespace_identity: int
    namespace_version: str
    namespace_fingerprint: str
    loader_identity: int
    loader_method: CallableIdentity
    get_plugin: CallableIdentity
    deadline: DeadlineSeal
    documents_identity: int | None
    documents: tuple[DocumentSeal, ...]
    query_templates_identity: int | None
    query_templates_digest: str
    containment_probe: CallableIdentity | None
    containment_sample_size: int | None
    monotonic_ns: CallableIdentity
    utc_now: CallableIdentity


@dataclass(frozen=True, slots=True)
class TrustedExecutionSeal:
    registry_identity: int
    resolve: CallableIdentity
    adapter_verifier: CallableIdentity
    adapter_binder: CallableIdentity
    context_identity: int
    budget_factory: CallableIdentity
    schema_runtime: RuntimeSeal
    data_runtime: RuntimeSeal


def callable_identity(value: object, label: str) -> CallableIdentity:
    if not callable(value):
        raise ExecutionSealError(f"{label} must be callable")
    owner = getattr(value, "__self__", None)
    function = getattr(value, "__func__", None)
    if owner is not None and function is not None:
        return CallableIdentity("bound_method", id(owner), id(function))
    if isinstance(value, FunctionType):
        return CallableIdentity("function", None, id(value))
    implementation = getattr(type(value), "__call__", None)
    if implementation is None:
        raise ExecutionSealError(f"{label} callable implementation is unavailable")
    return CallableIdentity("callable_object", id(value), id(implementation))


def _text_digest(value: object, label: str) -> str:
    if type(value) is not str:
        raise ExecutionSealError(f"{label} must be exact text")
    return canonical_digest({"text": value})


def _target_digest(value: TableRef | ColumnRef | None) -> str:
    if value is None:
        return canonical_digest(None)
    return canonical_digest(
        value.model_dump(mode="json", by_alias=True, warnings="error")
    )


def _deadline_seal(value: object, label: str) -> DeadlineSeal:
    if not isinstance(value, DeadlineBudget):
        raise ExecutionSealError(f"{label} must be DeadlineBudget")
    try:
        checked = DeadlineBudget(
            deadline_monotonic=value.deadline_monotonic,
            deadline_at_ms=value.deadline_at_ms,
            monotonic=value.monotonic,
            wall_time=value.wall_time,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionSealError(f"{label} is invalid") from exc
    return DeadlineSeal(
        identity=id(value),
        deadline_monotonic=float(checked.deadline_monotonic),
        deadline_at_ms=checked.deadline_at_ms,
        monotonic=callable_identity(checked.monotonic, f"{label}.monotonic"),
        wall_time=callable_identity(checked.wall_time, f"{label}.wall_time"),
    )


def deadline_identity_payload(value: object) -> object:
    if value is None:
        return None
    seal = _deadline_seal(value, "invocation.deadline")
    return {
        "identity": seal.identity,
        "deadline_monotonic": seal.deadline_monotonic,
        "deadline_at_ms": seal.deadline_at_ms,
        "monotonic": {
            "kind": seal.monotonic.kind,
            "owner_identity": seal.monotonic.owner_identity,
            "function_identity": seal.monotonic.function_identity,
        },
        "wall_time": {
            "kind": seal.wall_time.kind,
            "owner_identity": seal.wall_time.owner_identity,
            "function_identity": seal.wall_time.function_identity,
        },
    }


def _document_seal(document: object) -> DocumentSeal:
    if not isinstance(document, SchemaEvidenceDocument):
        raise ExecutionSealError("runtime document must be SchemaEvidenceDocument")
    for field_name in (
        "document_id",
        "namespace",
        "schema_namespace_version",
        "source_version",
        "title",
        "content",
    ):
        if type(getattr(document, field_name, None)) is not str:
            raise ExecutionSealError(
                f"runtime document {field_name} must be exact text"
            )
    target = document.target
    if target is not None and not isinstance(target, (TableRef, ColumnRef)):
        raise ExecutionSealError("runtime document target has the wrong type")
    try:
        checked = SchemaEvidenceDocument.model_validate(
            document.model_dump(
                mode="python",
                round_trip=True,
                warnings="error",
            ),
            strict=True,
        )
    except Exception as exc:
        raise ExecutionSealError(
            "runtime document violates its strict contract"
        ) from exc
    valid_until = (
        None if checked.valid_until is None else checked.valid_until.isoformat()
    )
    return DocumentSeal(
        identity=id(document),
        document_id=checked.document_id,
        namespace=checked.namespace,
        schema_namespace_version=checked.schema_namespace_version,
        source_version=checked.source_version,
        valid_until=valid_until,
        title_digest=_text_digest(checked.title, "document.title"),
        content_digest=_text_digest(checked.content, "document.content"),
        target_digest=_target_digest(checked.target),
    )


def _scope_values(value: object) -> tuple[int, str, str]:
    if not isinstance(value, SchemaScope):
        raise ExecutionSealError("runtime scope must be SchemaScope")
    try:
        mapping = value.to_mapping()
        checked = SchemaScope.from_mapping(mapping)
    except (TypeError, ValueError) as exc:
        raise ExecutionSealError("runtime scope violates its strict contract") from exc
    if checked != value:
        raise ExecutionSealError("runtime scope changed during validation")
    return id(value), canonical_digest(mapping), checked.scope_key


def _namespace_values(value: object) -> tuple[int, str, str]:
    if not isinstance(value, SchemaNamespace):
        raise ExecutionSealError("runtime namespace must be SchemaNamespace")
    try:
        checked = SchemaNamespace(
            scope=value.scope,
            schema_fingerprint=value.schema_fingerprint,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionSealError(
            "runtime namespace violates its strict contract"
        ) from exc
    return id(value), checked.version_key, checked.schema_fingerprint


def _effective_get_plugin(runtime: object) -> object:
    get_plugin = getattr(runtime, "get_plugin", None)
    if get_plugin is not None:
        return get_plugin
    from db_plugins import get_plugin as default_get_plugin

    return default_get_plugin


def _query_template_seal(value: object) -> tuple[int, dict[str, object]]:
    from .data_probes import ResearchQueryTemplate

    if not isinstance(value, ResearchQueryTemplate):
        raise ExecutionSealError("runtime query template must be ResearchQueryTemplate")
    try:
        checked = ResearchQueryTemplate(
            probe_id=value.probe_id,
            namespace=value.namespace,
            schema_namespace_version=value.schema_namespace_version,
            sql=value.sql,
            output_columns=value.output_columns,
            row_limit=value.row_limit,
            deterministic=value.deterministic,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionSealError(
            "runtime query template violates its strict contract"
        ) from exc
    return id(value), {
        "identity": id(value),
        "probe_id": checked.probe_id,
        "namespace": checked.namespace,
        "schema_namespace_version": checked.schema_namespace_version,
        "sql_digest": _text_digest(checked.sql, "query_template.sql"),
        "output_columns": checked.output_columns,
        "row_limit": checked.row_limit,
        "deterministic": checked.deterministic,
    }


def _runtime_seal(runtime: object, *, schema_runtime: bool) -> RuntimeSeal:
    table_namespace = getattr(runtime, "table_namespace", None)
    dsn = getattr(runtime, "dsn", None)
    if type(table_namespace) is not str or not table_namespace:
        raise ExecutionSealError("runtime table_namespace must be exact text")
    if type(dsn) is not str or not dsn:
        raise ExecutionSealError("runtime dsn must be exact text")

    scope = getattr(runtime, "scope", None)
    scope_identity, scope_digest, scope_key = _scope_values(scope)
    namespace = getattr(runtime, "namespace", None)
    namespace_identity, namespace_version, namespace_fingerprint = _namespace_values(
        namespace
    )
    if namespace.scope is not scope:
        raise ExecutionSealError("runtime scope and namespace scope must be identical")

    loader = getattr(runtime, "loader", None)
    if loader is None:
        raise ExecutionSealError("runtime loader is required")
    loader_method = callable_identity(
        getattr(loader, "load_scoped_schema", None),
        "runtime.loader.load_scoped_schema",
    )
    get_plugin = callable_identity(
        _effective_get_plugin(runtime),
        "runtime.get_plugin",
    )
    deadline = _deadline_seal(getattr(runtime, "deadline", None), "runtime.deadline")
    monotonic_ns = callable_identity(
        getattr(runtime, "monotonic_ns", None),
        "runtime.monotonic_ns",
    )
    utc_now = callable_identity(getattr(runtime, "utc_now", None), "runtime.utc_now")

    documents_value = getattr(runtime, "documents", ())
    if type(documents_value) is not tuple:
        raise ExecutionSealError("runtime documents must be an immutable tuple")
    documents = tuple(_document_seal(document) for document in documents_value)
    document_ids = [document.document_id for document in documents]
    if len(document_ids) != len(set(document_ids)):
        raise ExecutionSealError("runtime document IDs must be unique")
    if not schema_runtime and documents:
        raise ExecutionSealError("data runtime cannot carry schema documents")

    templates_value = getattr(runtime, "query_templates", ())
    if type(templates_value) is not tuple:
        raise ExecutionSealError("runtime query_templates must be an immutable tuple")
    template_items = tuple(_query_template_seal(item) for item in templates_value)
    if schema_runtime and template_items:
        raise ExecutionSealError("schema runtime cannot carry query templates")
    template_ids = [item[1]["probe_id"] for item in template_items]
    if len(template_ids) != len(set(template_ids)):
        raise ExecutionSealError("runtime query template IDs must be unique")

    containment_probe_value = getattr(runtime, "containment_probe", None)
    containment_probe = (
        None
        if containment_probe_value is None
        else callable_identity(containment_probe_value, "runtime.containment_probe")
    )
    containment_sample_size = getattr(runtime, "containment_sample_size", None)
    if containment_sample_size is not None and (
        type(containment_sample_size) is not int
        or not 1 <= containment_sample_size <= MAX_CONTAINMENT_SAMPLE_SIZE
    ):
        raise ExecutionSealError("runtime containment_sample_size is invalid")
    if containment_probe is not None and containment_sample_size is None:
        raise ExecutionSealError(
            "runtime containment probe requires containment_sample_size"
        )

    return RuntimeSeal(
        identity=id(runtime),
        table_namespace=table_namespace,
        dsn_digest=_text_digest(dsn, "runtime.dsn"),
        scope_identity=scope_identity,
        scope_digest=scope_digest,
        scope_key=scope_key,
        namespace_identity=namespace_identity,
        namespace_version=namespace_version,
        namespace_fingerprint=namespace_fingerprint,
        loader_identity=id(loader),
        loader_method=loader_method,
        get_plugin=get_plugin,
        deadline=deadline,
        documents_identity=id(documents_value) if schema_runtime else None,
        documents=documents,
        query_templates_identity=id(templates_value) if not schema_runtime else None,
        query_templates_digest=canonical_digest([item[1] for item in template_items]),
        containment_probe=containment_probe,
        containment_sample_size=containment_sample_size,
        monotonic_ns=monotonic_ns,
        utc_now=utc_now,
    )


def capture_trusted_execution_seal(
    registry: AdaptiveResearchToolRegistry,
) -> TrustedExecutionSeal:
    if not isinstance(registry, AdaptiveResearchToolRegistry):
        raise ExecutionSealError("registry must be AdaptiveResearchToolRegistry")
    context = registry.context
    return TrustedExecutionSeal(
        registry_identity=id(registry),
        resolve=callable_identity(registry.resolve, "registry.resolve"),
        adapter_verifier=callable_identity(
            registry.owns_resolved_adapter,
            "registry.owns_resolved_adapter",
        ),
        adapter_binder=callable_identity(
            registry.bind_owned_adapter_methods,
            "registry.bind_owned_adapter_methods",
        ),
        context_identity=id(context),
        budget_factory=callable_identity(
            context.budget_factory,
            "context.budget_factory",
        ),
        schema_runtime=_runtime_seal(context.schema_runtime, schema_runtime=True),
        data_runtime=_runtime_seal(context.data_runtime, schema_runtime=False),
    )


def trusted_execution_digest(value: TrustedExecutionSeal) -> str:
    if not isinstance(value, TrustedExecutionSeal):
        raise ExecutionSealError("trusted execution seal has the wrong type")
    return canonical_digest({"trusted_execution_seal": repr(value)})


def validate_trusted_execution_seal(
    registry: AdaptiveResearchToolRegistry,
    expected: TrustedExecutionSeal,
    expected_digest: str,
) -> None:
    if trusted_execution_digest(expected) != expected_digest:
        raise ExecutionSealError("trusted execution seal digest was changed")
    if capture_trusted_execution_seal(registry) != expected:
        raise ExecutionSealError("trusted execution context was changed")
