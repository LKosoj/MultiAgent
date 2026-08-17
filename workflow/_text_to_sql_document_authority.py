"""Server-owned authority for schema evidence documents.

The adaptive worker receives a frozen snapshot.  Later handoff and execution
checks query this registry again, so a past research result never authorizes a
document that has since changed, disappeared, or become unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.models import (
    DocumentRef,
    EvidenceSourceKind,
    ResearchState,
)
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
from custom_tools.text_to_sql.schema_namespace import SchemaNamespace, SchemaScope


class DocumentAuthorityError(ValueError):
    """A server document snapshot is missing or contradicts its schema."""


class SchemaDocumentRegistry(Protocol):
    """Server-owned document source; never populated from a request or evidence."""

    def snapshot(
        self,
        scope: SchemaScope,
        namespace: SchemaNamespace,
    ) -> tuple[SchemaEvidenceDocument, ...]: ...

    def source_states(
        self,
        scope: SchemaScope,
        namespace: SchemaNamespace,
        expected_ids: tuple[str, ...],
    ) -> tuple[DocumentSourceState, ...]: ...


@dataclass(frozen=True, slots=True)
class CanonicalSchemaDocumentRegistry:
    """Immutable canonical snapshot plus a live server-owned state reader."""

    scope: SchemaScope
    namespace: SchemaNamespace
    documents: tuple[SchemaEvidenceDocument, ...] = ()
    live_source_states: Callable[[tuple[str, ...]], tuple[DocumentSourceState, ...]] | None = None

    def __post_init__(self) -> None:
        if type(self.scope) is not SchemaScope or type(self.namespace) is not SchemaNamespace:
            raise TypeError("document registry requires exact scope and namespace")
        if self.namespace.scope != self.scope:
            raise DocumentAuthorityError("document registry namespace scope differs")
        documents = _canonical_documents(self.documents, self.namespace)
        object.__setattr__(self, "documents", documents)
        if self.live_source_states is not None and not callable(self.live_source_states):
            raise TypeError("live document source state reader must be callable")

    def snapshot(
        self,
        scope: SchemaScope,
        namespace: SchemaNamespace,
    ) -> tuple[SchemaEvidenceDocument, ...]:
        _require_identity(scope, namespace, self.scope, self.namespace)
        return self.documents

    def source_states(
        self,
        scope: SchemaScope,
        namespace: SchemaNamespace,
        expected_ids: tuple[str, ...],
    ) -> tuple[DocumentSourceState, ...]:
        _require_identity(scope, namespace, self.scope, self.namespace)
        expected = _canonical_ids(expected_ids)
        if self.live_source_states is None:
            return tuple(
                DocumentSourceState(
                    document_id=document.document_id,
                    availability=DocumentSourceAvailability.AVAILABLE,
                    source_version=document.source_version,
                )
                for document in self.documents
                if document.document_id in expected
            )
        return _canonical_source_states(self.live_source_states(expected), expected)


def empty_schema_document_registry(
    scope: SchemaScope,
    namespace: SchemaNamespace,
) -> CanonicalSchemaDocumentRegistry:
    """Return the explicit empty trusted set used by ordinary no-document runs."""

    return CanonicalSchemaDocumentRegistry(scope=scope, namespace=namespace)


def validated_document_snapshot(
    registry: object,
    scope: SchemaScope,
    namespace: SchemaNamespace,
) -> tuple[SchemaEvidenceDocument, ...]:
    """Read one exact server snapshot and reject every untrusted shape."""

    if not isinstance(scope, SchemaScope) or not isinstance(namespace, SchemaNamespace):
        raise TypeError("document snapshot requires SchemaScope and SchemaNamespace")
    snapshot = getattr(registry, "snapshot", None)
    if not callable(snapshot):
        raise DocumentAuthorityError("document registry is unavailable")
    return _canonical_documents(snapshot(scope, namespace), namespace)


def validated_document_source_states(
    registry: object,
    scope: SchemaScope,
    namespace: SchemaNamespace,
    expected_ids: tuple[str, ...],
) -> tuple[DocumentSourceState, ...]:
    """Read exact live states for an already authenticated document ID set."""

    expected = _canonical_ids(expected_ids)
    source_states = getattr(registry, "source_states", None)
    if not callable(source_states):
        raise DocumentAuthorityError("document registry is unavailable")
    return _canonical_source_states(source_states(scope, namespace, expected), expected)


def live_document_freshness_context(
    runtime: object,
    state: ResearchState,
) -> FreshnessContext:
    """Build freshness only from the current server registry and durable state."""

    if type(state) is not ResearchState:
        raise TypeError("document freshness requires exact ResearchState")
    loaded = getattr(runtime, "loaded_schema", None)
    registry = getattr(runtime, "document_registry", None)
    namespace = getattr(loaded, "namespace", None)
    scope = getattr(namespace, "scope", None)
    if type(namespace) is not SchemaNamespace or type(scope) is not SchemaScope:
        raise DocumentAuthorityError("live schema authority is unavailable")
    expected_ids = tuple(
        sorted(
            {
                evidence.target.document_id
                for evidence in state.evidence
                if evidence.source_kind is EvidenceSourceKind.DOCUMENT
                and type(evidence.target) is DocumentRef
            }
        )
    )
    source_states = (
        ()
        if not expected_ids and registry is None
        else validated_document_source_states(
            registry,
            scope,
            namespace,
            expected_ids,
        )
    )
    return FreshnessContext(
        evaluated_at=datetime.now(UTC),
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=source_states,
    )


def live_terminal_document_freshness_context(
    runtime: object,
    state: ResearchState,
    *,
    loaded_schema: object | None = None,
    document_registry: object | None = None,
) -> FreshnessContext:
    """Revalidate terminal document IDs against the live server source."""

    from .adaptive_state_store import AdaptiveLoopKind

    checkpoint_store = getattr(runtime, "checkpoint_store", None)
    if not callable(getattr(checkpoint_store, "load_terminal_replay_input", None)):
        raise DocumentAuthorityError("terminal replay input store is unavailable")
    events = checkpoint_store.load_run_events(
        state.run_id,
        state.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
    )
    terminals = tuple(
        event for event in events if event.phase.value == "terminal"
    )
    if len(terminals) != 1 or terminals[0].key.revision != state.revision:
        raise DocumentAuthorityError("terminal replay input is unavailable")
    replay_input = checkpoint_store.load_terminal_replay_input(terminals[0].key)
    _validate_terminal_replay_authority(state, terminals[0].action, replay_input)
    return _revalidated_document_freshness_context(
        runtime,
        state,
        replay_input.freshness_context,
        loaded_schema=loaded_schema,
        document_registry=document_registry,
    )


def live_solver_document_freshness_context(
    runtime: object,
    state: ResearchState,
) -> FreshnessContext:
    """Use a terminal or completed re-entry replay input for solver authority."""

    from custom_tools.text_to_sql.adaptive.models import ResearchReentryStatus
    from custom_tools.text_to_sql.adaptive.replay_inputs import (
        ResearchTerminalReplayInput,
        SolverReentryCompletedReplayInput,
    )
    from custom_tools.text_to_sql.adaptive.serialization import canonical_digest
    from .adaptive_state_store import AdaptiveActionPhase, AdaptiveLoopKind
    from .adaptive_solver_checkpoint import AdaptiveSolverCheckpointError

    checkpoint_store = getattr(runtime, "checkpoint_store", None)
    if not callable(getattr(checkpoint_store, "load_run_events", None)):
        raise DocumentAuthorityError("terminal replay input store is unavailable")
    events = checkpoint_store.load_run_events(
        state.run_id,
        state.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
    )
    terminals = tuple(
        event for event in events if event.phase is AdaptiveActionPhase.TERMINAL
    )
    if len(terminals) != 1:
        raise DocumentAuthorityError("research terminal replay input is unavailable")
    terminal = terminals[0]
    replay_input = checkpoint_store.load_terminal_replay_input(terminal.key)
    if type(replay_input) is not ResearchTerminalReplayInput:
        raise DocumentAuthorityError("terminal replay input is unavailable")
    if state.revision == terminal.key.revision:
        _validate_terminal_replay_authority(state, terminal.action, replay_input)
    if state.revision == terminal.key.revision:
        reference = replay_input.freshness_context
    elif state.revision > terminal.key.revision:
        research_store = getattr(runtime, "research_state_store", None)
        if not callable(getattr(research_store, "load_research_state", None)):
            raise DocumentAuthorityError("terminal research state is unavailable")
        terminal_state = research_store.load_research_state(
            state.run_id,
            state.run_incarnation,
            revision=terminal.key.revision,
        )
        if type(terminal_state) is not ResearchState:
            raise DocumentAuthorityError("terminal research state is unavailable")
        _validate_terminal_replay_authority(
            terminal_state,
            terminal.action,
            replay_input,
        )
        solver_store = getattr(runtime, "solver_checkpoint_store", None)
        if not callable(getattr(solver_store, "load_replay_chain", None)):
            raise DocumentAuthorityError("solver re-entry replay store is unavailable")
        try:
            chain = solver_store.load_replay_chain(state.run_id, state.run_incarnation)
        except AdaptiveSolverCheckpointError as exc:
            raise DocumentAuthorityError(
                "solver re-entry replay input is invalid"
            ) from exc
        if chain is None or not chain.snapshots:
            raise DocumentAuthorityError("solver re-entry replay input is unavailable")
        solver_state = chain.snapshots[-1].state
        expected_records = tuple(
            record
            for record in solver_state.research_reentries
            if record.status is ResearchReentryStatus.COMPLETED
            and record.research_result_revision == state.revision
        )
        if len(expected_records) != 1:
            raise DocumentAuthorityError("completed re-entry does not match research state")
        expected = expected_records[0]
        completed_inputs = []
        for action in chain.actions:
            try:
                replay = solver_store.load_transition_replay_input(
                    state.run_id,
                    state.run_incarnation,
                    action.action_revision,
                )
            except AdaptiveSolverCheckpointError as exc:
                raise DocumentAuthorityError(
                    "completed re-entry replay input is invalid"
                ) from exc
            if (
                type(replay) is SolverReentryCompletedReplayInput
                and replay.research_reentry_id == expected.research_reentry_id
                and replay.research_state_revision == state.revision
                and replay.research_state_digest == canonical_digest(state)
            ):
                completed_inputs.append(replay)
        if len(completed_inputs) != 1:
            raise DocumentAuthorityError("completed re-entry replay input is unavailable")
        reference = completed_inputs[0].freshness_context
    else:
        raise DocumentAuthorityError("research state predates terminal replay input")
    return _revalidated_document_freshness_context(runtime, state, reference)


def _revalidated_document_freshness_context(
    runtime: object,
    state: ResearchState,
    reference: FreshnessContext,
    *,
    loaded_schema: object | None = None,
    document_registry: object | None = None,
) -> FreshnessContext:
    if type(state) is not ResearchState or type(reference) is not FreshnessContext:
        raise TypeError("document replay freshness requires exact contracts")
    terminal = reference
    if (
        terminal.run_id != state.run_id
        or terminal.run_incarnation != state.run_incarnation
        or terminal.schema_namespace_version != state.schema_namespace_version
    ):
        raise DocumentAuthorityError("terminal replay freshness identity differs")
    loaded = (
        loaded_schema if loaded_schema is not None else getattr(runtime, "loaded_schema", None)
    )
    namespace = getattr(loaded, "namespace", None)
    scope = getattr(namespace, "scope", None)
    if type(namespace) is not SchemaNamespace or type(scope) is not SchemaScope:
        raise DocumentAuthorityError("live schema authority is unavailable")
    if f"sha256:{namespace.version_key}" != state.schema_namespace_version:
        raise DocumentAuthorityError("live schema authority differs from research state")
    expected_ids = tuple(sorted(item.document_id for item in terminal.document_sources))
    registry = (
        document_registry
        if document_registry is not None
        else getattr(runtime, "document_registry", None)
    )
    source_states = (
        ()
        if not expected_ids and registry is None
        else validated_document_source_states(
            registry,
            scope,
            namespace,
            expected_ids,
        )
    )
    terminal_by_id = {item.document_id: item for item in terminal.document_sources}
    if any(
        item.availability is not DocumentSourceAvailability.AVAILABLE
        or item.source_version is None
        for item in terminal_by_id.values()
    ):
        raise DocumentAuthorityError("terminal replay document source is unavailable")
    for source_state in source_states:
        terminal_state = terminal_by_id[source_state.document_id]
        if source_state.availability is not DocumentSourceAvailability.AVAILABLE:
            raise DocumentAuthorityError("terminal document source is unavailable")
        if source_state.source_version != terminal_state.source_version:
            raise DocumentAuthorityError("terminal document source version differs")
    return FreshnessContext(
        evaluated_at=datetime.now(UTC),
        run_id=state.run_id,
        run_incarnation=state.run_incarnation,
        schema_namespace_version=state.schema_namespace_version,
        document_sources=source_states,
    )


def _validate_terminal_replay_authority(
    state: ResearchState,
    action: object,
    replay_input: object,
) -> None:
    from custom_tools.text_to_sql.adaptive._research_terminal_authority import (
        _terminal_envelope,
        _terminal_replay_is_authorized,
    )
    from custom_tools.text_to_sql.adaptive.models import ResearchStopReason
    from custom_tools.text_to_sql.adaptive.replay_inputs import ResearchTerminalReplayInput

    if type(replay_input) is not ResearchTerminalReplayInput:
        raise DocumentAuthorityError("terminal replay input is unavailable")
    try:
        terminal = _terminal_envelope(action, state)
        reason = ResearchStopReason(terminal["reason"])
        authorized = _terminal_replay_is_authorized(
            state,
            replay_input.freshness_context,
            reason,
            terminal,
        )
    except (TypeError, ValueError) as exc:
        raise DocumentAuthorityError("terminal replay authority is invalid") from exc
    if not authorized:
        raise DocumentAuthorityError("terminal replay authority is not authorized")


def _require_identity(
    scope: SchemaScope,
    namespace: SchemaNamespace,
    expected_scope: SchemaScope,
    expected_namespace: SchemaNamespace,
) -> None:
    if type(scope) is not SchemaScope or type(namespace) is not SchemaNamespace:
        raise TypeError("document registry lookup requires exact schema identity")
    if scope != expected_scope or namespace != expected_namespace:
        raise DocumentAuthorityError("document registry lookup identity differs")


def _canonical_documents(
    documents: object,
    namespace: SchemaNamespace,
) -> tuple[SchemaEvidenceDocument, ...]:
    if type(documents) is not tuple or any(
        type(document) is not SchemaEvidenceDocument for document in documents
    ):
        raise DocumentAuthorityError("document snapshot has an invalid type")
    expected_version = f"sha256:{namespace.version_key}"
    if any(document.schema_namespace_version != expected_version for document in documents):
        raise DocumentAuthorityError("document snapshot schema namespace differs")
    by_id = {document.document_id: document for document in documents}
    if len(by_id) != len(documents):
        raise DocumentAuthorityError("document snapshot IDs must be unique")
    return tuple(by_id[key] for key in sorted(by_id))


def _canonical_ids(values: object) -> tuple[str, ...]:
    if type(values) is not tuple or any(type(value) is not str or not value for value in values):
        raise DocumentAuthorityError("document IDs must be an exact text tuple")
    if len(values) != len(set(values)):
        raise DocumentAuthorityError("document IDs must be unique")
    return tuple(sorted(values))


def _canonical_source_states(
    states: object,
    expected_ids: tuple[str, ...],
) -> tuple[DocumentSourceState, ...]:
    if type(states) is not tuple or any(type(state) is not DocumentSourceState for state in states):
        raise DocumentAuthorityError("document source states have an invalid type")
    by_id = {state.document_id: state for state in states}
    if len(by_id) != len(states) or tuple(sorted(by_id)) != expected_ids:
        raise DocumentAuthorityError("document source states differ from expected IDs")
    return tuple(by_id[key] for key in expected_ids)


__all__ = (
    "CanonicalSchemaDocumentRegistry",
    "DocumentAuthorityError",
    "SchemaDocumentRegistry",
    "empty_schema_document_registry",
    "live_document_freshness_context",
    "live_solver_document_freshness_context",
    "live_terminal_document_freshness_context",
    "validated_document_snapshot",
    "validated_document_source_states",
)
