"""Collision-aware repository for durable workflow-result sources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ._result_common import _canonical_payload
from .result_identity import (
    WorkflowResultIdentity,
    require_canonical_nonblank,
    workflow_result_event_key,
    workflow_result_identity_from_payload,
    workflow_result_identity_from_legacy_payload,
)


class WorkflowResultCollisionError(RuntimeError):
    """Raised when durable workflow-result sources cannot be reconciled."""


@dataclass(frozen=True)
class ReconciledWorkflowResult:
    payload: dict[str, Any]
    created_at_ms: Optional[int]


def _read_identity(
    payload: dict[str, Any],
    *,
    expected_run_id: Optional[str] = None,
    expected_run_incarnation: Optional[str] = None,
) -> WorkflowResultIdentity:
    try:
        return workflow_result_identity_from_payload(
            payload,
            expected_run_id=expected_run_id,
            expected_run_incarnation=expected_run_incarnation,
        )
    except ValueError as v2_error:
        try:
            identity = workflow_result_identity_from_legacy_payload(payload)
        except ValueError:
            raise v2_error
        if expected_run_id is not None and identity.run_id != expected_run_id:
            raise ValueError(
                "legacy workflow result run_id does not match expected identity"
            )
        if (
            expected_run_incarnation is not None
            and identity.run_incarnation != expected_run_incarnation
        ):
            raise ValueError(
                "legacy workflow result run_incarnation does not match "
                "expected identity"
            )
        return identity


def _canonical_payload_for_comparison(
    payload: dict[str, Any],
    identity: WorkflowResultIdentity,
) -> str:
    normalized = dict(payload)
    normalized["event_key"] = workflow_result_event_key(
        identity.run_id,
        identity.run_incarnation,
    )
    return _canonical_payload(normalized)


def _stored_event_key(event: Any) -> Optional[str]:
    event_key = getattr(event, "event_key", None)
    if isinstance(event_key, str):
        return event_key
    payload = getattr(event, "payload", None)
    payload_key = payload.get("event_key") if isinstance(payload, dict) else None
    return payload_key if isinstance(payload_key, str) else None


def _stored_event_incarnation(event: Any) -> Optional[str]:
    incarnation = getattr(event, "run_incarnation", None)
    if isinstance(incarnation, str):
        return incarnation
    payload = getattr(event, "payload", None)
    payload_incarnation = (
        payload.get("run_incarnation") if isinstance(payload, dict) else None
    )
    return payload_incarnation if isinstance(payload_incarnation, str) else None


def _is_workflow_result_candidate(event: Any, run_id: str) -> bool:
    if getattr(event, "event_type", None) == "WORKFLOW_RESULT":
        return True
    event_key = _stored_event_key(event)
    event_incarnation = _stored_event_incarnation(event)
    if not isinstance(event_incarnation, str):
        return False
    try:
        return event_key == workflow_result_event_key(run_id, event_incarnation)
    except ValueError:
        return False


def _primary_event_has_identity(event: Any, payload: dict[str, Any]) -> bool:
    return any(
        value is not None
        for value in (
            payload.get("event_key"),
            payload.get("run_incarnation"),
            getattr(event, "event_key", None),
            getattr(event, "run_incarnation", None),
        )
    )


def _validated_primary_payload(
    event: Any,
    *,
    run_id: str,
    run_incarnation: Optional[str],
) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        raise ValueError("primary event payload must be an object")
    if getattr(event, "event_type", None) != "WORKFLOW_RESULT":
        raise ValueError("primary event_type must be WORKFLOW_RESULT")
    if getattr(event, "run_id", None) != run_id:
        raise ValueError("primary event run_id does not match requested run")

    event_key = getattr(event, "event_key", None)
    event_incarnation = getattr(event, "run_incarnation", None)
    payload_has_identity = (
        payload.get("event_key") is not None
        or payload.get("run_incarnation") is not None
    )
    envelope_has_identity = event_key is not None or event_incarnation is not None
    if not payload_has_identity and not envelope_has_identity:
        if payload.get("run_id") not in {None, run_id}:
            raise ValueError("primary payload run_id does not match requested run")
        if run_incarnation is not None:
            raise ValueError("primary event is missing workflow result identity")
        return payload

    identity = _read_identity(
        payload,
        expected_run_id=run_id,
        expected_run_incarnation=run_incarnation,
    )
    canonical_v2_key = workflow_result_event_key(
        identity.run_id,
        identity.run_incarnation,
    )
    if identity.event_key != canonical_v2_key:
        if (event_key is None) != (event_incarnation is None):
            raise ValueError("legacy primary event has partial envelope identity")
        if event_key is not None and (
            event_key != identity.event_key
            or event_incarnation != identity.run_incarnation
        ):
            raise ValueError(
                "legacy primary event envelope does not match payload identity"
            )
        return payload
    if (
        event_key != identity.event_key
        or event_incarnation != identity.run_incarnation
    ):
        raise ValueError("primary event envelope does not match payload identity")
    return payload


def _validate_strict_primary_candidates(
    candidates: list[Any],
    *,
    run_id: str,
    run_incarnation: Optional[str],
) -> list[WorkflowResultIdentity]:
    """Validate the complete candidate set before strict-mode selection."""
    identified: list[tuple[WorkflowResultIdentity, str, bool]] = []
    unkeyed_count = 0
    for event in candidates:
        try:
            payload = _validated_primary_payload(
                event,
                run_id=run_id,
                run_incarnation=run_incarnation,
            )
            if not _primary_event_has_identity(event, payload):
                unkeyed_count += 1
                continue
            identity = _read_identity(
                payload,
                expected_run_id=run_id,
                expected_run_incarnation=run_incarnation,
            )
            canonical_payload = _canonical_payload_for_comparison(
                payload,
                identity,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise WorkflowResultCollisionError(
                f"WORKFLOW_RESULT primary candidate identity or envelope "
                f"mismatch for {run_id}: {exc}"
            ) from exc
        is_v2 = identity.event_key == workflow_result_event_key(
            identity.run_id,
            identity.run_incarnation,
        )
        identified.append((identity, canonical_payload, is_v2))

    if unkeyed_count and identified:
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT primary candidate identity collision for {run_id}"
        )
    if not identified:
        return []

    first_identity, first_payload, _ = identified[0]
    if any(
        identity.run_id != first_identity.run_id
        or identity.run_incarnation != first_identity.run_incarnation
        or canonical_payload != first_payload
        for identity, canonical_payload, _is_v2 in identified[1:]
    ):
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT primary candidate identity collision for {run_id}"
        )

    v2_identities = [
        identity for identity, _payload, is_v2 in identified if is_v2
    ]
    return v2_identities


def _strict_outbox_v2_identity(
    payload: Optional[dict[str, Any]],
    *,
    run_id: str,
    run_incarnation: Optional[str],
) -> Optional[WorkflowResultIdentity]:
    if payload is None:
        return None
    try:
        identity = _read_identity(
            payload,
            expected_run_id=run_id,
            expected_run_incarnation=run_incarnation,
        )
        canonical_key = workflow_result_event_key(
            identity.run_id,
            identity.run_incarnation,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT outbox identity mismatch for {run_id}: {exc}"
        ) from exc
    return identity if identity.event_key == canonical_key else None


def _validate_strict_v2_reservation(
    identities: list[WorkflowResultIdentity],
    *,
    run_id: str,
    reservation_lookup_supported: bool,
    reservation: Any,
) -> None:
    if not identities:
        return
    if not reservation_lookup_supported:
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT reservation lookup is unavailable for {run_id}"
        )
    try:
        reservation_run_id = require_canonical_nonblank(
            getattr(reservation, "run_id", None),
            field_name="reservation run_id",
        )
        reservation_incarnation = require_canonical_nonblank(
            getattr(reservation, "run_incarnation", None),
            field_name="reservation run_incarnation",
        )
    except ValueError as exc:
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT reservation is missing or invalid for {run_id}: {exc}"
        ) from exc
    if reservation_run_id != run_id:
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT run_id does not match reservation for {run_id}"
        )
    if any(
        identity.run_id != reservation_run_id
        or identity.run_incarnation != reservation_incarnation
        for identity in identities
    ):
        raise WorkflowResultCollisionError(
            f"WORKFLOW_RESULT incarnation does not match reservation for {run_id}"
        )


def _select_primary_event(
    candidates: list[Any],
    outbox_payload: Optional[dict[str, Any]],
    *,
    run_id: str,
    run_incarnation: Optional[str],
) -> Any:
    if not candidates:
        return None
    preferred_key = (
        outbox_payload.get("event_key")
        if isinstance(outbox_payload, dict)
        else None
    )
    if not isinstance(preferred_key, str) and run_incarnation is not None:
        preferred_key = workflow_result_event_key(run_id, run_incarnation)
    if isinstance(preferred_key, str):
        keyed = [
            event for event in candidates if _stored_event_key(event) == preferred_key
        ]
        if keyed:
            return keyed[-1]
    if run_incarnation is not None:
        matching = [
            event
            for event in candidates
            if _stored_event_incarnation(event) == run_incarnation
        ]
        if matching:
            return matching[-1]
    return candidates[-1]


def _sources_match(
    primary_event: Any,
    outbox_payload: dict[str, Any],
    *,
    run_id: str,
    run_incarnation: Optional[str],
) -> bool:
    try:
        primary_payload = _validated_primary_payload(
            primary_event,
            run_id=run_id,
            run_incarnation=run_incarnation,
        )
        primary_identity = _read_identity(primary_payload)
        outbox_identity = _read_identity(
            outbox_payload,
            expected_run_id=run_id,
            expected_run_incarnation=run_incarnation,
        )
    except ValueError:
        return False
    if (
        primary_identity.run_id != outbox_identity.run_id
        or primary_identity.run_incarnation != outbox_identity.run_incarnation
    ):
        return False
    try:
        return _canonical_payload_for_comparison(
            primary_payload,
            primary_identity,
        ) == _canonical_payload_for_comparison(
            outbox_payload,
            outbox_identity,
        )
    except (TypeError, ValueError):
        return False


def _open_primary_store(
    db_path: Path,
    factory: Optional[Callable[[str], Any]],
) -> Any:
    if factory is None:
        from backend.fastapi_app.agui.store import EventStore

        factory = EventStore
    return factory(str(db_path))


def _open_outbox(
    db_path: Path,
    factory: Optional[Callable[[str], Any]],
) -> Any:
    if factory is None:
        from .result_outbox import WorkflowResultOutbox

        factory = WorkflowResultOutbox
    return factory(str(db_path))


def load_reconciled_workflow_result(
    run_id: str,
    *,
    primary_store: Any = None,
    primary_path: Path | str | None = None,
    outbox_path: Path | str | None = None,
    strict: bool = False,
    run_incarnation: Optional[str] = None,
    event_store_factory: Optional[Callable[[str], Any]] = None,
    outbox_factory: Optional[Callable[[str], Any]] = None,
) -> Optional[ReconciledWorkflowResult]:
    """Read both durable sources and reject collisions before returning data."""
    require_canonical_nonblank(
        run_id,
        field_name="run_id",
    )
    if run_incarnation is not None:
        require_canonical_nonblank(
            run_incarnation,
            field_name="run_incarnation",
        )
    primary_error: Optional[Exception] = None
    primary_candidates: list[Any] = []
    primary_reservation: Any = None
    reservation_lookup_supported = False
    owned_primary = None
    try:
        active_primary = primary_store
        if active_primary is None and primary_path is not None:
            resolved_primary_path = Path(primary_path)
            if resolved_primary_path.exists():
                owned_primary = _open_primary_store(
                    resolved_primary_path,
                    event_store_factory,
                )
                active_primary = owned_primary
        if active_primary is not None:
            events: Iterable[Any] = active_primary.list_after(run_id, 0)
            primary_candidates = [
                event
                for event in events
                if _is_workflow_result_candidate(event, run_id)
            ]
            reservation_lookup = getattr(
                active_primary,
                "get_workflow_run_invocation",
                None,
            )
            if strict and callable(reservation_lookup):
                reservation_lookup_supported = True
                primary_reservation = reservation_lookup(run_id)
    except Exception as exc:
        primary_error = exc
    finally:
        if owned_primary is not None:
            try:
                owned_primary.close()
            except Exception as exc:
                primary_error = primary_error or exc

    outbox_error: Optional[Exception] = None
    outbox_payload: Optional[dict[str, Any]] = None
    if outbox_path is not None:
        try:
            resolved_outbox_path = Path(outbox_path)
            if resolved_outbox_path.exists():
                outbox = _open_outbox(resolved_outbox_path, outbox_factory)
                try:
                    outbox_payload = outbox.latest_payload(run_id)
                finally:
                    outbox.close()
        except Exception as exc:
            outbox_error = exc

    if strict:
        if primary_error is not None:
            raise primary_error
        if outbox_error is not None:
            raise outbox_error
        v2_identities = _validate_strict_primary_candidates(
            primary_candidates,
            run_id=run_id,
            run_incarnation=run_incarnation,
        )
        outbox_identity = _strict_outbox_v2_identity(
            outbox_payload,
            run_id=run_id,
            run_incarnation=run_incarnation,
        )
        if outbox_identity is not None:
            v2_identities.append(outbox_identity)
        _validate_strict_v2_reservation(
            v2_identities,
            run_id=run_id,
            reservation_lookup_supported=reservation_lookup_supported,
            reservation=primary_reservation,
        )

    latest_event = _select_primary_event(
        primary_candidates,
        outbox_payload,
        run_id=run_id,
        run_incarnation=run_incarnation,
    )
    if latest_event is not None and outbox_payload is not None:
        if not _sources_match(
            latest_event,
            outbox_payload,
            run_id=run_id,
            run_incarnation=run_incarnation,
        ):
            raise WorkflowResultCollisionError(
                f"WORKFLOW_RESULT collision: primary and outbox mismatch for {run_id}"
            )
        return ReconciledWorkflowResult(
            payload=latest_event.payload,
            created_at_ms=getattr(latest_event, "created_at_ms", None),
        )
    if outbox_payload is not None:
        try:
            _read_identity(
                outbox_payload,
                expected_run_id=run_id,
                expected_run_incarnation=run_incarnation,
            )
        except ValueError as exc:
            raise WorkflowResultCollisionError(
                f"WORKFLOW_RESULT outbox identity mismatch for {run_id}: {exc}"
            ) from exc
        return ReconciledWorkflowResult(
            payload=outbox_payload,
            created_at_ms=None,
        )
    if latest_event is not None:
        try:
            latest_payload = _validated_primary_payload(
                latest_event,
                run_id=run_id,
                run_incarnation=run_incarnation,
            )
        except ValueError as exc:
            raise WorkflowResultCollisionError(
                f"WORKFLOW_RESULT primary envelope mismatch for {run_id}: {exc}"
            ) from exc
        return ReconciledWorkflowResult(
            payload=latest_payload,
            created_at_ms=getattr(latest_event, "created_at_ms", None),
        )
    if strict:
        if primary_error is not None:
            raise primary_error
        if outbox_error is not None:
            raise outbox_error
    return None
