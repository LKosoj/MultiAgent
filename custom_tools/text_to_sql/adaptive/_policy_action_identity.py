"""Canonical adaptive-research action identity."""

from __future__ import annotations


from pydantic import ValidationError


from .models import (
    ResearchAction,
    ResearchActionKind,
    TargetRef,
)
from .serialization import canonical_digest

from ._policy_common import (
    ActionIdentityError,
    POLICY_VERSION,
    _revalidate,
)


def canonical_action_digest(
    *,
    kind: ResearchActionKind,
    hypothesis_id: str | None,
    target: TargetRef | None,
    parameters: tuple[tuple[str, str | int | float | bool | None], ...],
    expected_revision: int,
) -> str:
    """Hash semantic action fields, excluding action ID and CAS revision."""

    try:
        checked = ResearchAction(
            action_id="canonical-action",
            kind=kind,
            hypothesis_id=hypothesis_id,
            target=target,
            parameters=parameters,
            action_digest="sha256:" + "0" * 64,
            expected_revision=expected_revision,
        )
        identity = {
                "identity_version": POLICY_VERSION,
                "kind": checked.kind.value,
                "hypothesis_id": checked.hypothesis_id,
                "target": checked.target,
                "parameters": tuple(
                    sorted(checked.parameters, key=lambda item: item[0])
                ),
            }
        if checked.kind is ResearchActionKind.SEMANTIC_COMMIT:
            identity["expected_revision"] = checked.expected_revision
        return canonical_digest(identity)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ActionIdentityError(
            "action fields cannot form canonical identity"
        ) from exc


def canonical_digest_for_action(action: ResearchAction) -> str:
    """Revalidate an action and compute its canonical semantic digest."""

    checked = _revalidate(action, ResearchAction, "research action")
    return canonical_action_digest(
        kind=checked.kind,
        hypothesis_id=checked.hypothesis_id,
        target=checked.target,
        parameters=checked.parameters,
        expected_revision=checked.expected_revision,
    )
