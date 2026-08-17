"""Execution-seal and trusted-runtime integrity tests."""

from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.data_probes import ResearchQueryTemplate
from custom_tools.text_to_sql.adaptive.decision_resolver import (
    DecisionAlreadyExecutedError,
    DecisionExecutionError,
    DecisionResolverError,
    execute_resolved_research_decision,
    resolve_research_decision,
)
from custom_tools.text_to_sql.adaptive.models import TableRef
from custom_tools.text_to_sql.adaptive.probes import ProbeStatus
from custom_tools.text_to_sql.adaptive.tool_registry import (
    AdaptiveResearchToolContext,
    AdaptiveResearchToolRegistry,
)
from custom_tools.text_to_sql.schema_namespace import SchemaNamespace, SchemaScope
from tests.text_to_sql_decision_resolver_helpers import (
    freshness as _freshness,
    make_registry as _registry,
    make_state as _state,
    normalized_failure as _normalized_failure,
    resolve as _resolve,
    resolved_stop as _resolved_stop,
    schema as _schema,
    schema_version as _schema_version,
    tool_decision as _tool_decision,
)
from workflow.deadline import DeadlineBudget


def test_stop_runtime_tamper_fails_before_consuming_one_shot_gate() -> None:
    resolved, registry = _resolved_stop("unsupported")
    registry.context.schema_runtime.table_namespace = "other"

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    registry.context.schema_runtime.table_namespace = "main"
    assert execute_resolved_research_decision(resolved, registry) is None
    with pytest.raises(DecisionAlreadyExecutedError):
        execute_resolved_research_decision(resolved, registry)
    assert registry.resolve_calls == 0


def _equivalent_scope(value: SchemaScope) -> SchemaScope:
    return SchemaScope.from_mapping(value.to_mapping())


def _equivalent_namespace(value: SchemaNamespace) -> SchemaNamespace:
    return SchemaNamespace(
        scope=value.scope,
        schema_fingerprint=value.schema_fingerprint,
    )


def _replace_context_runtime(context, field_name: str) -> None:
    runtime = getattr(context, field_name)
    replacement = SimpleNamespace(**vars(runtime))
    object.__setattr__(context, field_name, replacement)


@pytest.mark.parametrize(
    ("label", "raw", "mutate"),
    [
        (
            "schema_table_namespace",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime, "table_namespace", "other"
            ),
        ),
        (
            "namespace_fingerprint_and_version",
            False,
            lambda tools: object.__setattr__(
                tools.context.schema_runtime.namespace,
                "schema_fingerprint",
                "f" * 64,
            ),
        ),
        (
            "namespace_replacement",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime,
                "namespace",
                _equivalent_namespace(tools.context.schema_runtime.namespace),
            ),
        ),
        (
            "scope_replacement",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime,
                "scope",
                _equivalent_scope(tools.context.schema_runtime.scope),
            ),
        ),
        (
            "schema_dsn",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime, "dsn", "sqlite://changed"
            ),
        ),
        (
            "documents",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime,
                "documents",
                (
                    tools.context.schema_runtime.documents[0].model_copy(
                        update={"content": "changed"}
                    ),
                ),
            ),
        ),
        (
            "schema_loader",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime,
                "loader",
                SimpleNamespace(load_scoped_schema=lambda *_args: None),
            ),
        ),
        (
            "schema_get_plugin",
            False,
            lambda tools: setattr(
                tools.context.schema_runtime,
                "get_plugin",
                lambda _dsn: SimpleNamespace(dialect="sqlite"),
            ),
        ),
        (
            "schema_deadline_value",
            False,
            lambda tools: object.__setattr__(
                tools.context.schema_runtime.deadline,
                "deadline_at_ms",
                tools.context.schema_runtime.deadline.deadline_at_ms + 1,
            ),
        ),
        (
            "budget_factory",
            False,
            lambda tools: object.__setattr__(
                tools.context, "budget_factory", lambda *_args: None
            ),
        ),
        (
            "context_replacement",
            False,
            lambda tools: object.__setattr__(
                tools,
                "_context",
                AdaptiveResearchToolContext(
                    schema_runtime=tools.context.schema_runtime,
                    data_runtime=tools.context.data_runtime,
                    budget_factory=tools.context.budget_factory,
                ),
            ),
        ),
        (
            "schema_runtime_replacement",
            False,
            lambda tools: _replace_context_runtime(tools.context, "schema_runtime"),
        ),
        (
            "raw_data_table_namespace",
            True,
            lambda tools: setattr(
                tools.context.data_runtime, "table_namespace", "other"
            ),
        ),
        (
            "raw_data_dsn",
            True,
            lambda tools: setattr(
                tools.context.data_runtime, "dsn", "sqlite://changed"
            ),
        ),
        (
            "raw_data_get_plugin",
            True,
            lambda tools: setattr(
                tools.context.data_runtime,
                "get_plugin",
                lambda _dsn: SimpleNamespace(dialect="sqlite"),
            ),
        ),
        (
            "raw_data_loader",
            True,
            lambda tools: setattr(
                tools.context.data_runtime,
                "loader",
                SimpleNamespace(load_scoped_schema=lambda *_args: None),
            ),
        ),
        (
            "raw_data_deadline_replacement",
            True,
            lambda tools: setattr(
                tools.context.data_runtime,
                "deadline",
                DeadlineBudget(
                    deadline_monotonic=130.0,
                    deadline_at_ms=1_700_000_030_000,
                    monotonic=lambda: 100.0,
                    wall_time=lambda: 1_700_000_000.0,
                ),
            ),
        ),
        (
            "data_runtime_replacement",
            True,
            lambda tools: _replace_context_runtime(tools.context, "data_runtime"),
        ),
    ],
)
def test_trusted_runtime_tamper_fails_before_registry_resolution(
    label: str,
    raw: bool,
    mutate,
) -> None:
    del label
    loaded, namespace = _schema()
    registry = _registry(namespace)
    decision = (
        _tool_decision(
            "execute_research_probe",
            {
                "sql": "SELECT id FROM public.orders ORDER BY id LIMIT 2",
                "parameters": (),
            },
        )
        if raw
        else _tool_decision("inspect_table", {"table": "public.orders"})
    )
    resolved = _resolve(
        decision,
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    mutate(registry)

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0
    assert registry.adapter.recover_calls == 0


def test_transient_execution_seal_does_not_store_dsn_or_document_content() -> None:
    resolved = _resolve(_tool_decision("inspect_table", {"table": "public.orders"}))

    seal = repr(resolved._trusted_execution_seal)
    assert "sqlite://trusted" not in seal
    assert "Orders schema" not in seal


def test_benign_runtime_clock_progression_does_not_break_execution_seal() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    now = [100.0]
    deadline = DeadlineBudget(
        deadline_monotonic=130.0,
        deadline_at_ms=1_700_000_030_000,
        monotonic=lambda: now[0],
        wall_time=lambda: 1_700_000_000.0 + now[0] - 100.0,
    )
    registry.context.schema_runtime.deadline = deadline
    registry.context.data_runtime.deadline = deadline
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    registry.adapter.result = _normalized_failure(resolved)

    now[0] = 110.0
    result = execute_resolved_research_decision(resolved, registry)

    assert result.status is ProbeStatus.FAILED
    assert registry.resolve_calls == 1


def test_invocation_deadline_binds_absolute_values_but_not_clock_progression() -> None:
    loaded, namespace = _schema()
    state = _state(namespace)
    registry = _registry(namespace)
    now = [100.0]
    deadline = DeadlineBudget(
        deadline_monotonic=130.0,
        deadline_at_ms=1_700_000_030_000,
        monotonic=lambda: now[0],
        wall_time=lambda: 1_700_000_000.0 + now[0] - 100.0,
    )
    resolved = resolve_research_decision(
        state,
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=registry,
        deadline=deadline,
    )
    registry.adapter.result = _normalized_failure(resolved)

    now[0] = 110.0
    result = execute_resolved_research_decision(resolved, registry)

    assert result.status is ProbeStatus.FAILED
    assert registry.resolve_calls == 1


def test_invocation_deadline_tamper_fails_before_registry_resolution() -> None:
    loaded, namespace = _schema()
    state = _state(namespace)
    registry = _registry(namespace)
    deadline = DeadlineBudget(
        deadline_monotonic=130.0,
        deadline_at_ms=1_700_000_030_000,
        monotonic=lambda: 100.0,
        wall_time=lambda: 1_700_000_000.0,
    )
    resolved = resolve_research_decision(
        state,
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded_schema=loaded,
        freshness_context=_freshness(state),
        registry=registry,
        deadline=deadline,
    )
    object.__setattr__(deadline, "deadline_at_ms", deadline.deadline_at_ms + 1)

    with pytest.raises(DecisionExecutionError, match="identity was changed"):
        execute_resolved_research_decision(resolved, registry)
    assert registry.resolve_calls == 0


def test_instance_resolve_replacement_is_rejected_before_gate_or_dispatch() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    registry.resolve = lambda _tool_name: registry.adapter

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_class_resolve_replacement_is_rejected_before_dispatch(monkeypatch) -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    original = type(registry).resolve

    def wrapper(self, tool_name: str):
        return original(self, tool_name)

    monkeypatch.setattr(type(registry), "resolve", wrapper)

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_bound_resolve_wrapper_is_rejected_before_dispatch() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )

    def wrapper(self, tool_name: str):
        return type(self).resolve(self, tool_name)

    registry.resolve = MethodType(wrapper, registry)

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_mutation_inside_resolve_is_rejected_before_adapter_execution() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    original_resolve = registry.resolve

    class ResolveCallback:
        mutate = False

        def __call__(self, tool_name: str):
            adapter = original_resolve(tool_name)
            if self.mutate:
                registry.context.schema_runtime.table_namespace = "other"
            return adapter

    callback = ResolveCallback()
    registry.resolve = callback
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    callback.mutate = True

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 1
    assert registry.adapter.execute_calls == 0
    assert registry.adapter.recover_calls == 0


def test_spoofed_registry_cache_adapter_is_never_executed() -> None:
    loaded, namespace = _schema()
    context = _registry(namespace).context
    registry = AdaptiveResearchToolRegistry(context)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )

    class MaliciousAdapter:
        execute_calls = 0

        def execute(self, _invocation):
            self.execute_calls += 1
            raise AssertionError("untrusted adapter executed")

    malicious = MaliciousAdapter()
    registry._adapters["inspect_table"] = malicious

    with pytest.raises(DecisionExecutionError, match="not registry-owned"):
        execute_resolved_research_decision(resolved, registry)

    assert malicious.execute_calls == 0


def test_spoofed_cached_adapter_callback_is_never_executed() -> None:
    loaded, namespace = _schema()
    context = _registry(namespace).context
    registry = AdaptiveResearchToolRegistry(context)
    adapter = registry.resolve("inspect_table")
    assert adapter is not None
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    calls = 0

    def malicious_execute(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("spoofed adapter callback executed")

    object.__setattr__(adapter._spec, "execute", malicious_execute)

    with pytest.raises(DecisionExecutionError, match="not registry-owned"):
        execute_resolved_research_decision(resolved, registry)

    assert calls == 0


@pytest.mark.parametrize("method_name", ["execute", "recover"])
def test_owned_adapter_instance_cannot_shadow_dispatch_methods(
    method_name: str,
) -> None:
    _, namespace = _schema()
    registry = AdaptiveResearchToolRegistry(_registry(namespace).context)
    adapter = registry.resolve("inspect_table")
    assert adapter is not None
    calls = 0

    def spoof(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    with pytest.raises(AttributeError):
        setattr(adapter, method_name, spoof)
    with pytest.raises(AttributeError):
        object.__setattr__(adapter, method_name, spoof)

    assert not hasattr(adapter, "__dict__")
    assert calls == 0


@pytest.mark.parametrize(
    ("method_name", "recover"),
    [("execute", False), ("recover", True)],
)
def test_class_level_adapter_spoof_is_rejected_before_callback(
    monkeypatch,
    method_name: str,
    recover: bool,
) -> None:
    loaded, namespace = _schema()
    registry = AdaptiveResearchToolRegistry(_registry(namespace).context)
    adapter = registry.resolve("inspect_table")
    assert adapter is not None
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    calls = 0

    def spoof(_self, _invocation):
        nonlocal calls
        calls += 1
        raise AssertionError("class-level adapter spoof executed")

    monkeypatch.setattr(type(adapter), method_name, spoof)

    assert not registry.owns_resolved_adapter("inspect_table", adapter)
    with pytest.raises(DecisionExecutionError, match="not registry-owned"):
        execute_resolved_research_decision(resolved, registry, recover=recover)

    assert calls == 0


@pytest.mark.parametrize("method_name", ["execute", "recover"])
def test_class_level_adapter_descriptor_is_rejected_without_lookup(
    monkeypatch,
    method_name: str,
) -> None:
    loaded, namespace = _schema()
    registry = AdaptiveResearchToolRegistry(_registry(namespace).context)
    adapter = registry.resolve("inspect_table")
    assert adapter is not None
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    calls = 0

    class SpoofDescriptor:
        def __get__(self, _instance, _owner):
            nonlocal calls
            calls += 1
            raise AssertionError("spoof descriptor was read")

    monkeypatch.setattr(type(adapter), method_name, SpoofDescriptor())

    assert not registry.owns_resolved_adapter("inspect_table", adapter)
    with pytest.raises(DecisionExecutionError, match="not registry-owned"):
        execute_resolved_research_decision(resolved, registry)

    assert calls == 0


@pytest.mark.parametrize("method_index", [0, 1])
def test_bound_owned_method_does_not_relookup_class_method(
    monkeypatch,
    method_index: int,
) -> None:
    _, namespace = _schema()
    registry = AdaptiveResearchToolRegistry(_registry(namespace).context)
    adapter = registry.resolve("inspect_table")
    assert adapter is not None
    methods = registry.bind_owned_adapter_methods("inspect_table", adapter)
    assert methods is not None
    calls = 0

    def spoof(_self, _invocation):
        nonlocal calls
        calls += 1
        raise AssertionError("late class-level adapter spoof executed")

    method_name = "execute" if method_index == 0 else "recover"
    monkeypatch.setattr(type(adapter), method_name, spoof)

    with pytest.raises(TypeError, match="invocation must be ToolInvocation"):
        methods[method_index](object())

    assert calls == 0


def test_malicious_adapter_subclass_in_both_caches_is_rejected() -> None:
    loaded, namespace = _schema()
    registry = AdaptiveResearchToolRegistry(_registry(namespace).context)
    issued = registry.resolve("inspect_table")
    assert issued is not None
    calls = 0

    class MaliciousAdapter(type(issued)):
        def execute(self, _invocation):
            nonlocal calls
            calls += 1
            raise AssertionError("adapter subclass executed")

    malicious = MaliciousAdapter(
        name=issued._name,
        context=issued._context,
        spec=issued._spec,
    )
    registry._adapters["inspect_table"] = malicious
    registry._issued_adapters["inspect_table"] = malicious
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )

    assert not registry.owns_resolved_adapter("inspect_table", malicious)
    with pytest.raises(DecisionExecutionError, match="not registry-owned"):
        execute_resolved_research_decision(resolved, registry)

    assert calls == 0


def test_legitimate_cached_adapter_binds_both_original_methods() -> None:
    _, namespace = _schema()
    registry = AdaptiveResearchToolRegistry(_registry(namespace).context)
    adapter = registry.resolve("inspect_table")
    assert adapter is not None
    assert registry.resolve("inspect_table") is adapter

    methods = registry.bind_owned_adapter_methods("inspect_table", adapter)

    assert registry.owns_resolved_adapter("inspect_table", adapter)
    assert methods is not None
    assert methods[0].__self__ is adapter
    assert methods[0].__func__ is type(adapter).execute
    assert methods[1].__self__ is adapter
    assert methods[1].__func__ is type(adapter).recover


@pytest.mark.parametrize(
    ("method_name", "recover"),
    [("execute", False), ("recover", True)],
)
def test_dispatch_uses_method_bound_before_class_spoof(
    monkeypatch,
    method_name: str,
    recover: bool,
) -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    original_binder = registry.bind_owned_adapter_methods
    calls = 0

    def spoof(_self, _invocation):
        nonlocal calls
        calls += 1
        raise AssertionError("late adapter spoof executed")

    class BinderCallback:
        def __call__(self, tool_name: str, adapter: object):
            methods = original_binder(tool_name, adapter)
            monkeypatch.setattr(type(registry.adapter), method_name, spoof)
            return methods

    registry.bind_owned_adapter_methods = BinderCallback()
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    registry.adapter.result = _normalized_failure(resolved)

    result = execute_resolved_research_decision(resolved, registry, recover=recover)

    assert result.status is ProbeStatus.FAILED
    assert registry.resolve_calls == 1
    assert registry.adapter.execute_calls == (0 if recover else 1)
    assert registry.adapter.recover_calls == (1 if recover else 0)
    assert calls == 0


def test_strict_document_revalidation_rejects_forgery_during_resolution() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    document = registry.context.schema_runtime.documents[0]
    object.__setattr__(document, "content", ["not", "text"])

    with pytest.raises(DecisionResolverError, match="runtime is invalid"):
        _resolve(
            _tool_decision("inspect_table", {"table": "public.orders"}),
            loaded=loaded,
            namespace=namespace,
            registry=registry,
        )

    assert registry.resolve_calls == 0


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("title", ["not", "text"]),
        ("content", {"not": "text"}),
        ("target", {"namespace": "main", "schema": "public", "table": "orders"}),
        ("source_version", ["doc-v2"]),
        ("valid_until", {"timestamp": "2026-07-31T12:00:00Z"}),
        ("schema_namespace_version", ["sha256:forged"]),
    ],
)
def test_forged_document_fields_are_rejected_before_registry_resolution(
    field_name: str,
    forged_value: object,
) -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    document = registry.context.schema_runtime.documents[0]
    object.__setattr__(document, field_name, forged_value)

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_invalid_document_model_copy_is_rejected_before_registry_resolution() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    document = registry.context.schema_runtime.documents[0]
    registry.context.schema_runtime.documents = (
        document.model_copy(update={"content": ["not", "text"]}),
    )

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_forged_nested_document_target_is_rejected_before_dispatch() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    document = registry.context.schema_runtime.documents[0]
    target = TableRef(namespace="main", schema="public", table="orders")
    object.__setattr__(document, "target", target)
    resolved = _resolve(
        _tool_decision("inspect_table", {"table": "public.orders"}),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    object.__setattr__(target, "schema_name", ["public"])

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0


def test_forged_nested_query_template_is_rejected_before_dispatch() -> None:
    loaded, namespace = _schema()
    registry = _registry(namespace)
    template = ResearchQueryTemplate(
        probe_id="probe-1",
        namespace="main",
        schema_namespace_version=_schema_version(namespace),
        sql="SELECT id FROM public.orders ORDER BY id LIMIT 2",
        output_columns=("id",),
        row_limit=2,
        deterministic=True,
    )
    registry.context.data_runtime.query_templates = (template,)
    resolved = _resolve(
        _tool_decision(
            "execute_research_probe",
            {
                "sql": "SELECT id FROM public.orders ORDER BY id LIMIT 2",
                "parameters": (),
            },
        ),
        loaded=loaded,
        namespace=namespace,
        registry=registry,
    )
    object.__setattr__(template, "output_columns", ["id"])

    with pytest.raises(DecisionExecutionError, match="runtime identity was changed"):
        execute_resolved_research_decision(resolved, registry)

    assert registry.resolve_calls == 0
    assert registry.adapter.execute_calls == 0
