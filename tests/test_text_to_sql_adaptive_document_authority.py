"""Server document authority is strict, scoped, and empty by default."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_tools.text_to_sql.adaptive.freshness import (
    DocumentSourceAvailability,
    DocumentSourceState,
    FreshnessContext,
)
from custom_tools.text_to_sql.adaptive.schema_probes import SchemaEvidenceDocument
from custom_tools.text_to_sql.adaptive.models import ResearchState
from custom_tools.text_to_sql.adaptive.replay_inputs import ResearchTerminalReplayInput
from custom_tools.text_to_sql.schema_namespace import (
    SchemaNamespace,
    SchemaScope,
    canonical_schema_fingerprint,
)
from workflow._text_to_sql_document_authority import (
    CanonicalSchemaDocumentRegistry,
    DocumentAuthorityError,
    empty_schema_document_registry,
    live_terminal_document_freshness_context,
    validated_document_snapshot,
    validated_document_source_states,
)
from workflow.adaptive_state_store import (
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from test_text_to_sql_solver_runner import _runtime


def _identity():
    scope = SchemaScope(1, "tenant", "scope", "connection", True)
    namespace = SchemaNamespace(
        scope,
        canonical_schema_fingerprint({"orders": {"columns": {}}}),
    )
    return scope, namespace


def _document(namespace, document_id="document-1"):
    return SchemaEvidenceDocument(
        document_id=document_id,
        namespace="main",
        schema_namespace_version=f"sha256:{namespace.version_key}",
        source_version="v1",
        valid_until=datetime(2030, 1, 1, tzinfo=UTC),
        title="Orders definition",
        content="Orders document",
        target=None,
    )


def test_registry_returns_canonical_server_snapshot_and_live_states() -> None:
    scope, namespace = _identity()
    first = _document(namespace, "document-a")
    second = _document(namespace, "document-b")
    registry = CanonicalSchemaDocumentRegistry(
        scope,
        namespace,
        (second, first),
        lambda ids: tuple(
            DocumentSourceState(
                document_id=document_id,
                availability=DocumentSourceAvailability.AVAILABLE,
                source_version="v2",
            )
            for document_id in reversed(ids)
        ),
    )

    assert validated_document_snapshot(registry, scope, namespace) == (first, second)
    assert validated_document_source_states(
        registry, scope, namespace, ("document-a", "document-b")
    ) == (
        DocumentSourceState(
            document_id="document-a",
            availability=DocumentSourceAvailability.AVAILABLE,
            source_version="v2",
        ),
        DocumentSourceState(
            document_id="document-b",
            availability=DocumentSourceAvailability.AVAILABLE,
            source_version="v2",
        ),
    )


def test_registry_rejects_duplicate_or_wrong_namespace_documents() -> None:
    scope, namespace = _identity()
    document = _document(namespace)
    with pytest.raises(DocumentAuthorityError, match="IDs"):
        CanonicalSchemaDocumentRegistry(scope, namespace, (document, document))
    with pytest.raises(DocumentAuthorityError, match="namespace"):
        CanonicalSchemaDocumentRegistry(
            scope,
            namespace,
            (document.model_copy(update={"schema_namespace_version": "sha256:" + "0" * 64}),),
        )


def test_registry_rejects_missing_or_wrong_live_source_ids() -> None:
    scope, namespace = _identity()
    registry = CanonicalSchemaDocumentRegistry(
        scope,
        namespace,
        (_document(namespace),),
        lambda _ids: (),
    )

    with pytest.raises(DocumentAuthorityError, match="differ"):
        validated_document_source_states(registry, scope, namespace, ("document-1",))


def test_empty_registry_is_the_explicit_no_document_authority() -> None:
    scope, namespace = _identity()
    registry = empty_schema_document_registry(scope, namespace)

    assert validated_document_snapshot(registry, scope, namespace) == ()
    assert validated_document_source_states(registry, scope, namespace, ()) == ()


def _terminal_runtime(tmp_path, *, live_state, live_version="v2"):
    _, research_state, _, loaded_schema = _runtime()
    document = _document(loaded_schema.namespace)
    registry = CanonicalSchemaDocumentRegistry(
        loaded_schema.namespace.scope,
        loaded_schema.namespace,
        (document,),
        lambda ids: tuple(
            DocumentSourceState(
                document_id=document_id,
                availability=live_state,
                source_version=(
                    live_version
                    if live_state is DocumentSourceAvailability.AVAILABLE
                    else None
                ),
            )
            for document_id in ids
        ),
    )
    return research_state, document, SimpleNamespace(
        loaded_schema=loaded_schema,
        document_registry=registry,
        checkpoint_store=AdaptiveStateStore(tmp_path / "terminal.db"),
    )


def _record_terminal_freshness(
    runtime,
    state,
    document=None,
    *,
    terminal_availability=DocumentSourceAvailability.AVAILABLE,
    terminal_version="v1",
    action=None,
) -> None:
    store = runtime.checkpoint_store
    for revision in range(state.revision):
        key = AdaptiveCheckpointKey(
            state.run_id,
            state.run_incarnation,
            AdaptiveLoopKind.RESEARCH,
            revision,
        )
        store.record_planned(
            key,
            expected_revision=None if revision == 0 else revision - 1,
            action={"kind": "historical_planned", "revision": revision},
        )
        store.record_observed(
            key,
            expected_revision=revision,
            action={"kind": "historical_observed", "revision": revision},
        )
    key = AdaptiveCheckpointKey(
        state.run_id,
        state.run_incarnation,
        AdaptiveLoopKind.RESEARCH,
        state.revision,
    )
    store.record_replayable_terminal(
        key,
        expected_revision=state.revision - 1,
        action=(
            {
                "affected_source_ids": [],
                "citation_evidence_ids": [],
                "contract_version": 2,
                "kind": "research_terminal",
                "rejection_signatures": [],
                "reason": "COMPLETE",
                "ambiguity": None,
            }
            if action is None
            else action
        ),
        replay_input=ResearchTerminalReplayInput(
            freshness_context=FreshnessContext(
                evaluated_at=datetime(2026, 8, 2, tzinfo=UTC),
                run_id=state.run_id,
                run_incarnation=state.run_incarnation,
                schema_namespace_version=state.schema_namespace_version,
                document_sources=(
                    ()
                    if document is None
                    else (
                        DocumentSourceState(
                            document_id=document.document_id,
                            availability=terminal_availability,
                            source_version=(
                                terminal_version
                                if terminal_availability
                                is DocumentSourceAvailability.AVAILABLE
                                else None
                            ),
                        ),
                    )
                ),
            )
        ),
    )


@pytest.mark.parametrize(
    "live_state",
    (
        DocumentSourceAvailability.AVAILABLE,
        DocumentSourceAvailability.REMOVED,
        DocumentSourceAvailability.UNAVAILABLE,
    ),
)
def test_terminal_document_authority_rejects_changed_or_unavailable_live_source(
    tmp_path, live_state
) -> None:
    state, document, runtime = _terminal_runtime(tmp_path, live_state=live_state)
    _record_terminal_freshness(runtime, state, document)

    with pytest.raises(DocumentAuthorityError):
        live_terminal_document_freshness_context(runtime, state)


def test_terminal_document_authority_requires_replay_input(tmp_path) -> None:
    state, _, runtime = _terminal_runtime(
        tmp_path, live_state=DocumentSourceAvailability.AVAILABLE
    )

    with pytest.raises(DocumentAuthorityError, match="terminal replay input"):
        live_terminal_document_freshness_context(runtime, state)


def test_terminal_document_authority_accepts_unchanged_live_source(tmp_path) -> None:
    state, document, runtime = _terminal_runtime(
        tmp_path,
        live_state=DocumentSourceAvailability.AVAILABLE,
        live_version="v1",
    )
    _record_terminal_freshness(runtime, state, document)

    context = live_terminal_document_freshness_context(runtime, state)

    assert context.document_sources[0].source_version == "v1"


def test_terminal_document_authority_rejects_namespace_mismatch(tmp_path) -> None:
    state, document, runtime = _terminal_runtime(
        tmp_path, live_state=DocumentSourceAvailability.AVAILABLE, live_version="v1"
    )
    _record_terminal_freshness(runtime, state, document)
    scope, namespace = _identity()
    runtime.document_registry = CanonicalSchemaDocumentRegistry(
        scope,
        namespace,
        (_document(namespace),),
    )

    with pytest.raises(DocumentAuthorityError, match="identity"):
        live_terminal_document_freshness_context(runtime, state)


def test_terminal_document_authority_rejects_non_available_terminal_source(tmp_path) -> None:
    state, document, runtime = _terminal_runtime(
        tmp_path, live_state=DocumentSourceAvailability.AVAILABLE, live_version="v1"
    )
    _record_terminal_freshness(
        runtime,
        state,
        document,
        terminal_availability=DocumentSourceAvailability.REMOVED,
    )

    with pytest.raises(DocumentAuthorityError, match="terminal replay document source"):
        live_terminal_document_freshness_context(runtime, state)


def test_terminal_document_authority_accepts_ordinary_no_document_path(tmp_path) -> None:
    _, state, _, loaded_schema = _runtime()
    runtime = SimpleNamespace(
        loaded_schema=loaded_schema,
        document_registry=empty_schema_document_registry(
            loaded_schema.namespace.scope,
            loaded_schema.namespace,
        ),
        checkpoint_store=AdaptiveStateStore(tmp_path / "no-document.db"),
    )
    _record_terminal_freshness(runtime, state)

    assert live_terminal_document_freshness_context(runtime, state).document_sources == ()


def test_terminal_document_authority_rejects_loaded_namespace_different_from_state(
    tmp_path,
) -> None:
    state, document, runtime = _terminal_runtime(
        tmp_path,
        live_state=DocumentSourceAvailability.AVAILABLE,
        live_version="v1",
    )
    expected = state.schema_namespace_version
    replaced = "sha256:" + "f" * 64

    def rewrite(value):
        if type(value) is dict:
            return {key: rewrite(item) for key, item in value.items()}
        if type(value) is list:
            return [rewrite(item) for item in value]
        if type(value) is tuple:
            return tuple(rewrite(item) for item in value)
        return value.replace(expected, replaced) if type(value) is str else value

    state = ResearchState.model_validate(rewrite(state.model_dump(mode="python")))
    _record_terminal_freshness(runtime, state, document)

    with pytest.raises(DocumentAuthorityError, match="schema authority"):
        live_terminal_document_freshness_context(runtime, state)


def test_terminal_document_authority_rejects_malformed_terminal_envelope(tmp_path) -> None:
    state, document, runtime = _terminal_runtime(
        tmp_path,
        live_state=DocumentSourceAvailability.AVAILABLE,
        live_version="v1",
    )
    _record_terminal_freshness(runtime, state, document, action={"reason": "COMPLETE"})

    with pytest.raises(DocumentAuthorityError, match="terminal"):
        live_terminal_document_freshness_context(runtime, state)
