"""Runtime state for the single Typed Text-to-SQL pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
import threading
from typing import Any, Awaitable, Callable

from .deadline import DeadlineBudget


_ADMISSION_CAPABILITY = object()


@dataclass(frozen=True, slots=True)
class TextToSqlTypedAdmission:
    run_id: str
    run_incarnation: str
    deadline: DeadlineBudget
    query: object
    dsn: object
    schema_scope: object
    _capability: object = field(repr=False, compare=False)
    context_documents: tuple[str, ...] = ()


@dataclass(slots=True)
class TextToSqlTypedRuntime:
    run_id: str
    run_incarnation: str
    deadline: DeadlineBudget
    query: object
    dsn: object
    schema_scope: object
    research_state_store: Any
    checkpoint_store: Any
    budget_ledger: Any
    solver_checkpoint_store: Any
    _capability: object = field(repr=False, compare=False)
    _admission: TextToSqlTypedAdmission = field(repr=False, compare=False)
    context_documents: tuple[str, ...] = ()
    document_registry: Any | None = field(default=None, repr=False, compare=False)
    supervisor_evidence: Any | None = field(default=None, repr=False, compare=False)
    _cancellation_checker: Callable[[], Awaitable[bool]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    loaded_schema: Any | None = field(default=None, init=False, repr=False)
    loaded_schema_digest: str | None = field(default=None, init=False)
    document_snapshot: tuple[Any, ...] = field(default=(), init=False, repr=False)
    verified_research_state: Any | None = field(default=None, init=False, repr=False)
    verified_research_outcome: Any | None = field(default=None, init=False, repr=False)
    verified_research_policy: Any | None = field(default=None, init=False, repr=False)
    verified_solver_state: Any | None = field(default=None, init=False, repr=False)
    verified_solver_candidate_id: str | None = field(default=None, init=False)
    verified_solver_terminal: Any | None = field(default=None, init=False, repr=False)
    _schema_capture_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _cancelled: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
        compare=False,
    )

    def __setattr__(self, name: str, value: object) -> None:
        if name == "_admission" and hasattr(self, "_admission"):
            raise AttributeError("Typed runtime admission is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name == "_admission":
            raise AttributeError("Typed runtime admission is immutable")
        object.__delattr__(self, name)

    def capture_loaded_schema(self, value: object) -> None:
        from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
        from custom_tools.text_to_sql.schema_loader import LoadedSchema
        from custom_tools.text_to_sql.schema_namespace import (
            SchemaScope,
            canonical_schema_fingerprint,
        )

        if not isinstance(value, LoadedSchema):
            raise TypeError("Typed schema capture requires LoadedSchema")
        if not isinstance(self.schema_scope, Mapping):
            raise ValueError("Typed schema capture requires schema scope")
        scope = SchemaScope.from_mapping(self.schema_scope)
        if value.namespace.scope != scope:
            raise ValueError("captured schema scope differs from admission")
        if canonical_schema_fingerprint(value.schema) != value.namespace.schema_fingerprint:
            raise ValueError("captured schema fingerprint is invalid")
        digest = canonical_digest(
            {
                "namespace_version": value.namespace.version_key,
                "schema": value.schema,
                "source": value.source,
            }
        )
        from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
        from ._text_to_sql_document_authority import (
            CanonicalSchemaDocumentRegistry,
            validated_document_snapshot,
        )

        registry = self.document_registry
        request_documents = tuple(
            SchemaEvidenceDocument(
                document_id=f"context-{index + 1}-{sha256(content.encode('utf-8')).hexdigest()}",
                namespace="main",
                schema_namespace_version=f"sha256:{value.namespace.version_key}",
                source_version=f"sha256:{sha256(content.encode('utf-8')).hexdigest()}",
                title=f"Context document {index + 1}",
                content=content,
                target=None,
            )
            for index, content in enumerate(self.context_documents)
        )
        if registry is None:
            registry = CanonicalSchemaDocumentRegistry(
                scope, value.namespace, request_documents
            )
        elif request_documents:
            existing = validated_document_snapshot(registry, scope, value.namespace)
            registry = CanonicalSchemaDocumentRegistry(
                scope,
                value.namespace,
                (*existing, *request_documents),
                getattr(registry, "live_source_states", None),
            )
        documents = validated_document_snapshot(registry, scope, value.namespace)
        with self._schema_capture_lock:
            if self.loaded_schema is not None:
                if self.loaded_schema_digest != digest:
                    raise RuntimeError("Typed schema was captured with different bytes")
                return
            self.loaded_schema = value
            self.loaded_schema_digest = digest
            self.document_registry = registry
            self.document_snapshot = documents

    def mark_cancelled(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()


def capture_text_to_sql_typed_admission(
    *,
    run_id: object,
    run_incarnation: object,
    deadline: object,
    query: object,
    context_documents: object = (),
    dsn: object,
    schema_scope: object,
) -> TextToSqlTypedAdmission:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Typed run_id must be non-empty text")
    if not isinstance(run_incarnation, str) or not run_incarnation:
        raise ValueError("Typed run_incarnation must be non-empty text")
    if not isinstance(deadline, DeadlineBudget):
        raise TypeError("Typed deadline must be a DeadlineBudget")
    if type(context_documents) not in {tuple, list} or any(
        type(document) is not str or not document.strip()
        for document in context_documents
    ):
        raise ValueError("Typed context documents must be non-empty text strings")
    frozen_scope = (
        MappingProxyType(dict(schema_scope))
        if isinstance(schema_scope, Mapping)
        else schema_scope
    )
    return TextToSqlTypedAdmission(
        run_id=run_id,
        run_incarnation=run_incarnation,
        deadline=deadline,
        query=query,
        context_documents=tuple(context_documents),
        dsn=dsn,
        schema_scope=frozen_scope,
        _capability=_ADMISSION_CAPABILITY,
    )


def install_text_to_sql_typed_runtime(
    context: object,
    *,
    state_manager: object,
    cancellation_checker: Callable[[], Awaitable[bool]] | None = None,
    document_registry: object | None = None,
) -> TextToSqlTypedRuntime:
    admission = getattr(context, "_text_to_sql_typed_admission", None)
    if not isinstance(admission, TextToSqlTypedAdmission):
        raise TypeError("Text-to-SQL requires a Typed admission")
    if admission._capability is not _ADMISSION_CAPABILITY:
        raise TypeError("Typed admission is not trusted")
    if getattr(context, "_deadline_budget", None) is not admission.deadline:
        raise ValueError("Typed admission deadline differs from workflow deadline")
    variables = getattr(context, "variables", None)
    if not isinstance(variables, Mapping) or variables.get("run_id") != admission.run_id:
        raise ValueError("Typed admission run differs from workflow context")
    existing = getattr(context, "_text_to_sql_typed_runtime", None)
    if isinstance(existing, TextToSqlTypedRuntime):
        if existing._admission is admission:
            return existing
        raise RuntimeError("Typed runtime belongs to a different admission")

    store_owner = getattr(state_manager, "store", None)
    if store_owner is None:
        raise TypeError("workflow state manager has no Typed stores")
    research_state_store = store_owner.get_adaptive_research_state_store()
    checkpoint_store = store_owner.get_adaptive_state_store()
    budget_ledger = store_owner.get_adaptive_budget_ledger()
    solver_checkpoint_store = store_owner.get_adaptive_solver_checkpoint_store()
    runtime = TextToSqlTypedRuntime(
        run_id=admission.run_id,
        run_incarnation=admission.run_incarnation,
        deadline=admission.deadline,
        query=admission.query,
        context_documents=admission.context_documents,
        dsn=admission.dsn,
        schema_scope=admission.schema_scope,
        research_state_store=research_state_store,
        checkpoint_store=checkpoint_store,
        budget_ledger=budget_ledger,
        solver_checkpoint_store=solver_checkpoint_store,
        document_registry=document_registry,
        supervisor_evidence=getattr(context, "_supervisor_evidence", None),
        _capability=_ADMISSION_CAPABILITY,
        _admission=admission,
        _cancellation_checker=cancellation_checker,
    )
    context._text_to_sql_typed_runtime = runtime
    return runtime


__all__ = (
    "TextToSqlTypedAdmission",
    "TextToSqlTypedRuntime",
    "capture_text_to_sql_typed_admission",
    "install_text_to_sql_typed_runtime",
)
