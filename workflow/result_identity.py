"""Canonical identity contract for durable workflow results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


_WORKFLOW_RESULT_EVENT_KEY_V1_PREFIX = "workflow-result:"
_WORKFLOW_RESULT_EVENT_KEY_V2_PREFIX = "workflow-result:v2:"


def require_canonical_nonblank(value: Any, *, field_name: str) -> str:
    """Return an exact nonblank string without normalizing caller input."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ValueError(
            f"{field_name} must be a canonical non-empty string without "
            "surrounding whitespace"
        )
    return value


def workflow_result_event_key(run_id: str, run_incarnation: str) -> str:
    canonical_run_id = require_canonical_nonblank(
        run_id,
        field_name="run_id",
    )
    canonical_incarnation = require_canonical_nonblank(
        run_incarnation,
        field_name="run_incarnation",
    )
    return (
        f"{_WORKFLOW_RESULT_EVENT_KEY_V2_PREFIX}"
        f"{len(canonical_run_id)}:{canonical_run_id}:"
        f"{len(canonical_incarnation)}:{canonical_incarnation}"
    )


@dataclass(frozen=True)
class WorkflowResultIdentity:
    event_key: str
    run_id: str
    run_incarnation: str


def _parse_length(
    event_key: str,
    *,
    offset: int,
) -> tuple[int, int]:
    separator = event_key.find(":", offset)
    if separator < 0:
        raise ValueError("workflow result event_key is not canonical v2")
    encoded_length = event_key[offset:separator]
    if (
        not encoded_length
        or not encoded_length.isascii()
        or not encoded_length.isdecimal()
    ):
        raise ValueError("workflow result event_key has an invalid v2 length")
    component_length = int(encoded_length)
    if str(component_length) != encoded_length:
        raise ValueError("workflow result event_key has a noncanonical v2 length")
    return component_length, separator + 1


def parse_workflow_result_event_key(event_key: Any) -> WorkflowResultIdentity:
    canonical_event_key = require_canonical_nonblank(
        event_key,
        field_name="event_key",
    )
    if not canonical_event_key.startswith(_WORKFLOW_RESULT_EVENT_KEY_V2_PREFIX):
        raise ValueError("workflow result event_key must use canonical v2 identity")

    offset = len(_WORKFLOW_RESULT_EVENT_KEY_V2_PREFIX)
    run_id_length, run_id_start = _parse_length(
        canonical_event_key,
        offset=offset,
    )
    run_id_end = run_id_start + run_id_length
    if run_id_end >= len(canonical_event_key) or canonical_event_key[run_id_end] != ":":
        raise ValueError("workflow result event_key has an invalid v2 run_id")
    run_id = require_canonical_nonblank(
        canonical_event_key[run_id_start:run_id_end],
        field_name="run_id",
    )

    incarnation_length, incarnation_start = _parse_length(
        canonical_event_key,
        offset=run_id_end + 1,
    )
    incarnation_end = incarnation_start + incarnation_length
    if incarnation_end != len(canonical_event_key):
        raise ValueError("workflow result event_key has an invalid v2 incarnation")
    run_incarnation = require_canonical_nonblank(
        canonical_event_key[incarnation_start:incarnation_end],
        field_name="run_incarnation",
    )
    if workflow_result_event_key(run_id, run_incarnation) != canonical_event_key:
        raise ValueError("workflow result event_key is not canonical v2")
    return WorkflowResultIdentity(
        event_key=canonical_event_key,
        run_id=run_id,
        run_incarnation=run_incarnation,
    )


def validate_workflow_result_identity(
    *,
    event_key: Any,
    run_id: Any,
    run_incarnation: Any,
) -> WorkflowResultIdentity:
    canonical_run_id = require_canonical_nonblank(
        run_id,
        field_name="run_id",
    )
    canonical_incarnation = require_canonical_nonblank(
        run_incarnation,
        field_name="run_incarnation",
    )
    canonical_event_key = require_canonical_nonblank(
        event_key,
        field_name="event_key",
    )
    parsed = parse_workflow_result_event_key(canonical_event_key)
    if (
        parsed.run_id != canonical_run_id
        or parsed.run_incarnation != canonical_incarnation
    ):
        raise ValueError("event_key does not match workflow result identity")
    return WorkflowResultIdentity(
        event_key=canonical_event_key,
        run_id=canonical_run_id,
        run_incarnation=canonical_incarnation,
    )


def workflow_result_identity_from_payload(
    payload: Mapping[str, Any],
    *,
    expected_run_id: str | None = None,
    expected_run_incarnation: str | None = None,
) -> WorkflowResultIdentity:
    if not isinstance(payload, Mapping):
        raise ValueError("workflow result payload must be an object")
    identity = validate_workflow_result_identity(
        event_key=payload.get("event_key"),
        run_id=payload.get("run_id"),
        run_incarnation=payload.get("run_incarnation"),
    )
    if expected_run_id is not None:
        canonical_expected_run_id = require_canonical_nonblank(
            expected_run_id,
            field_name="expected_run_id",
        )
        if identity.run_id != canonical_expected_run_id:
            raise ValueError(
                "workflow result event_key run_id does not match expected identity"
            )
    if expected_run_incarnation is not None:
        canonical_expected_incarnation = require_canonical_nonblank(
            expected_run_incarnation,
            field_name="expected_run_incarnation",
        )
        if identity.run_incarnation != canonical_expected_incarnation:
            raise ValueError(
                "workflow result event_key run_incarnation does not match "
                "expected identity"
            )
    return identity


def validate_legacy_workflow_result_identity(
    *,
    event_key: Any,
    run_id: Any,
    run_incarnation: Any,
) -> WorkflowResultIdentity:
    """Validate one already-stored v1 identity for legacy reads only."""
    canonical_run_id = require_canonical_nonblank(run_id, field_name="run_id")
    canonical_incarnation = require_canonical_nonblank(
        run_incarnation,
        field_name="run_incarnation",
    )
    canonical_event_key = require_canonical_nonblank(
        event_key,
        field_name="event_key",
    )
    expected = (
        f"{_WORKFLOW_RESULT_EVENT_KEY_V1_PREFIX}"
        f"{canonical_run_id}:{canonical_incarnation}"
    )
    if canonical_event_key != expected:
        raise ValueError("legacy event_key does not match workflow result identity")
    return WorkflowResultIdentity(
        event_key=canonical_event_key,
        run_id=canonical_run_id,
        run_incarnation=canonical_incarnation,
    )


def workflow_result_identity_from_legacy_payload(
    payload: Mapping[str, Any],
) -> WorkflowResultIdentity:
    """Validate the identity of an already-stored v1 payload for reads only."""
    if not isinstance(payload, Mapping):
        raise ValueError("legacy workflow result payload must be an object")
    return validate_legacy_workflow_result_identity(
        event_key=payload.get("event_key"),
        run_id=payload.get("run_id"),
        run_incarnation=payload.get("run_incarnation"),
    )


def upgrade_legacy_workflow_result_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a v2 write payload from one validated stored v1 payload."""
    legacy = workflow_result_identity_from_legacy_payload(payload)
    upgraded = dict(payload)
    upgraded["event_key"] = workflow_result_event_key(
        legacy.run_id,
        legacy.run_incarnation,
    )
    return upgraded
