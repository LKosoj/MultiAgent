"""Durable callback tests for the adaptive checkpoint journal."""

from __future__ import annotations

import pytest

from workflow.adaptive_state_store import (
    AdaptiveActionPhase,
    AdaptiveCheckpointConflictError,
    AdaptiveCheckpointKey,
    AdaptiveLoopKind,
    AdaptiveStateStore,
)
from workflow.state_manager import SQLiteWorkflowStore


def _key() -> AdaptiveCheckpointKey:
    return AdaptiveCheckpointKey("run-1", "inc-1", AdaptiveLoopKind.RESEARCH, 0)


def test_transition_callback_is_durable_idempotent_and_rejects_conflicts(
    tmp_path,
) -> None:
    path = tmp_path / "state.db"
    store = AdaptiveStateStore(path)
    key = _key()

    first = store.on_state_transition(
        key,
        AdaptiveActionPhase.PLANNED,
        expected_revision=None,
        action={"tool": "inspect"},
    )
    assert (
        store.on_state_transition(
            key,
            AdaptiveActionPhase.PLANNED,
            expected_revision=None,
            action={"tool": "inspect"},
        )
        == first
    )
    with pytest.raises(AdaptiveCheckpointConflictError):
        store.on_state_transition(
            key,
            AdaptiveActionPhase.PLANNED,
            expected_revision=None,
            action={"tool": "other"},
        )
    store.close()

    resumed = AdaptiveStateStore(path)
    assert resumed.get_snapshot(key).planned == first
    resumed.close()


def test_workflow_store_adapter_forwards_transition_callback(tmp_path) -> None:
    workflow_store = SQLiteWorkflowStore(str(tmp_path / "workflow.db"))
    event = workflow_store.on_state_transition(
        _key(),
        AdaptiveActionPhase.PLANNED,
        expected_revision=None,
        action={"tool": "inspect"},
    )

    assert (
        workflow_store.get_adaptive_state_store().get_snapshot(_key()).planned == event
    )
