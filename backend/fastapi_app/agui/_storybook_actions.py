"""Диспетчер действий `storybook.*`: роли и вызов возможностей studio."""

from __future__ import annotations

from typing import Any, Dict

from .auth import Principal
from .redaction import _redact_payload
from .serialization import _serialize

STORYBOOK_PREFIX = "storybook."


def is_storybook_action(action: str) -> bool:
    return isinstance(action, str) and action.startswith(STORYBOOK_PREFIX)


def resolve(action: str):
    from custom_tools.storybook.studio.capabilities import find

    return find(action)


def require_storybook_role(action: str, principal: Principal) -> None:
    # Все storybook.* только для админа, как workflows.storybook_*;
    # access (read/write/run) храним для подтверждений и интерфейса.
    if resolve(action) is None:
        raise PermissionError(f"service action '{action}' is not classified")
    if not principal.has_role("admin"):
        raise PermissionError(f"service action '{action}' requires role 'admin'")


def dispatch(action: str, payload: Dict[str, Any], principal: Principal) -> Dict[str, Any]:
    from custom_tools.storybook.studio.capabilities import invoke

    cap = resolve(action)
    if cap is None:
        raise PermissionError(f"service action '{action}' is not classified")
    # ошибки studio возвращаются как status=error, а не бросаются
    return _redact_payload(_serialize(invoke(cap.id, payload)))
