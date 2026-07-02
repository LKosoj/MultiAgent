"""Minimal AG-UI request authentication and run ownership helpers."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Iterator

from fastapi import HTTPException, Request, status


_VALID_AUTH_MODES = frozenset({"disabled", "optional", "required"})
_SCOPED_SESSION_MARKER = "__u_"
_CURRENT_PRINCIPAL: contextvars.ContextVar["Principal | None"] = contextvars.ContextVar(
    "agui_principal",
    default=None,
)


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str = "default"
    roles: frozenset[str] = frozenset({"user"})

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def can_access(self, owner: "Principal") -> bool:
        if self.has_role("admin"):
            return True
        return self.subject == owner.subject and self.tenant_id == owner.tenant_id


def system_principal_for_disabled_mode() -> Principal:
    return Principal(
        subject="system",
        tenant_id="default",
        roles=frozenset({"admin", "memory_archivist", "user"}),
    )


def anonymous_principal() -> Principal:
    return Principal(subject="anonymous", tenant_id="anonymous", roles=frozenset({"user"}))


def scope_session_id(base_session_id: str, principal: Principal) -> str:
    if principal.subject == "system" and principal.has_role("admin"):
        return base_session_id
    material = f"{base_session_id}\0{principal.tenant_id}\0{principal.subject}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{base_session_id}{_SCOPED_SESSION_MARKER}{digest}"


def normalize_session_id_for_principal(session_id: str, principal: Principal) -> str:
    base_session_id, separator, _suffix = session_id.rpartition(_SCOPED_SESSION_MARKER)
    if not separator:
        return scope_session_id(session_id, principal)
    if principal.has_role("admin"):
        return session_id
    expected = scope_session_id(base_session_id, principal)
    if not hmac.compare_digest(session_id, expected):
        raise PermissionError("session_id scope does not match current principal")
    return session_id


def current_principal() -> Principal:
    principal = _CURRENT_PRINCIPAL.get()
    if principal is not None:
        return principal
    if auth_mode() == "disabled":
        return system_principal_for_disabled_mode()
    return anonymous_principal()


def current_principal_or_none() -> Principal | None:
    return _CURRENT_PRINCIPAL.get()


@contextlib.contextmanager
def principal_context(principal: Principal) -> Iterator[None]:
    token = _CURRENT_PRINCIPAL.set(principal)
    try:
        yield
    finally:
        _CURRENT_PRINCIPAL.reset(token)


def auth_mode() -> str:
    raw = os.getenv("AG_UI_AUTH_MODE")
    if raw is None:
        raw = "required"
    mode = raw.strip().lower()
    if mode not in _VALID_AUTH_MODES:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"invalid AG_UI_AUTH_MODE: {raw}",
        )
    return mode


def authenticate_request(request: Request) -> Principal:
    mode = auth_mode()
    if mode == "disabled":
        return system_principal_for_disabled_mode()

    token = _extract_token(request)
    if not token:
        if mode == "optional":
            return anonymous_principal()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="AG-UI authentication token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = authenticate_token(token)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid AG-UI authentication token",
        )
    return principal


def authenticate_token(candidate: str) -> Principal | None:
    for configured_token, principal in _configured_token_map().items():
        if hmac.compare_digest(candidate, configured_token):
            return principal
    return None


def principal_to_record(principal: Principal) -> dict[str, object]:
    return {
        "subject": principal.subject,
        "tenant_id": principal.tenant_id,
        "roles": sorted(principal.roles),
    }


def principal_from_record(record: dict[str, object]) -> Principal:
    roles_raw = record.get("roles") or []
    roles = frozenset(str(role) for role in roles_raw if str(role))
    return Principal(
        subject=str(record.get("subject") or ""),
        tenant_id=str(record.get("tenant_id") or "default"),
        roles=roles or frozenset({"user"}),
    )


def _extract_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    for header_name in ("x-agui-token", "x-ag-ui-token"):
        value = request.headers.get(header_name)
        if value and value.strip():
            return value.strip()
    return None


def _has_configured_tokens() -> bool:
    return any(
        os.getenv(name)
        for name in (
            "AG_UI_AUTH_TOKEN",
            "AG_UI_ADMIN_TOKEN",
            "AG_UI_USER_TOKEN",
            "AG_UI_MEMORY_ARCHIVIST_TOKEN",
            "AG_UI_AUTH_TOKEN_MAP",
        )
    )


def _configured_token_map() -> dict[str, Principal]:
    result: dict[str, Principal] = {}
    token_map_raw = os.getenv("AG_UI_AUTH_TOKEN_MAP")
    if token_map_raw:
        try:
            parsed = json.loads(token_map_raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="invalid AG_UI_AUTH_TOKEN_MAP JSON",
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="AG_UI_AUTH_TOKEN_MAP must be a JSON object",
            )
        for token, spec in parsed.items():
            if not isinstance(token, str) or not token:
                continue
            if not isinstance(spec, dict):
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="AG_UI_AUTH_TOKEN_MAP values must be objects",
                )
            result[token] = _principal_from_spec(spec, default_subject=token)

    _add_env_token(
        result,
        "AG_UI_AUTH_TOKEN",
        "agui-user",
        {"user"},
    )
    _add_env_token(result, "AG_UI_ADMIN_TOKEN", "agui-admin", {"admin", "memory_archivist", "user"})
    _add_env_token(result, "AG_UI_USER_TOKEN", "agui-user", {"user"})
    _add_env_token(
        result,
        "AG_UI_MEMORY_ARCHIVIST_TOKEN",
        "agui-memory-archivist",
        {"memory_archivist", "user"},
    )
    return result


def _principal_from_spec(spec: dict[str, object], default_subject: str) -> Principal:
    roles_raw = spec.get("roles") or ["user"]
    if isinstance(roles_raw, str):
        roles = frozenset(role.strip() for role in roles_raw.split(",") if role.strip())
    elif isinstance(roles_raw, list):
        roles = frozenset(str(role) for role in roles_raw if str(role))
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="principal roles must be a string or list",
        )
    return Principal(
        subject=str(spec.get("subject") or default_subject),
        tenant_id=str(spec.get("tenant_id") or "default"),
        roles=roles or frozenset({"user"}),
    )


def _add_env_token(
    token_map: dict[str, Principal],
    env_name: str,
    default_subject: str,
    roles: set[str],
) -> None:
    token = os.getenv(env_name)
    if not token:
        return
    token_map[token] = Principal(
        subject=default_subject,
        tenant_id="default",
        roles=frozenset(roles),
    )
