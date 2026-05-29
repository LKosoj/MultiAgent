"""Dependency-light serialization helpers for AG-UI service payloads."""

from __future__ import annotations

import base64
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def _serialize(
    value: Any,
    _memo: dict[int, Any] | None = None,
    _active: set[int] | None = None,
) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if _memo is None:
        _memo = {}
    if _active is None:
        _active = set()
    guardable = (
        is_dataclass(value)
        or isinstance(value, (dict, list, tuple))
        or hasattr(value, "model_dump")
        or hasattr(value, "dict")
        or hasattr(value, "to_dict")
    )
    if guardable:
        obj_id = id(value)
        if obj_id in _active:
            return "[Circular]"
        if obj_id in _memo:
            return _memo[obj_id]
        _active.add(obj_id)
    try:
        if is_dataclass(value):
            redacted: dict[str, Any] = {}
            _memo[id(value)] = redacted
            for field in fields(value):
                redacted[field.name] = _serialize(
                    getattr(value, field.name),
                    _memo,
                    _active,
                )
            return redacted
        if isinstance(value, dict):
            redacted: dict[Any, Any] = {}
            _memo[id(value)] = redacted
            for key, val in value.items():
                redacted[key] = _serialize(val, _memo, _active)
            return redacted
        if isinstance(value, (list, tuple)):
            redacted: list[Any] = []
            _memo[id(value)] = redacted
            redacted.extend(_serialize(item, _memo, _active) for item in value)
            return redacted
        if hasattr(value, "model_dump"):
            try:
                redacted = _serialize(value.model_dump(), _memo, _active)
                _memo[id(value)] = redacted
                return redacted
            except Exception:
                return str(value)
        if hasattr(value, "dict"):
            try:
                redacted = _serialize(value.dict(), _memo, _active)
                _memo[id(value)] = redacted
                return redacted
            except Exception:
                return str(value)
        if hasattr(value, "to_dict"):
            try:
                redacted = _serialize(value.to_dict(), _memo, _active)
                _memo[id(value)] = redacted
                return redacted
            except Exception:
                return str(value)
    finally:
        if guardable:
            _active.discard(id(value))
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if hasattr(value, "model_id"):
        try:
            return _serialize(getattr(value, "model_id"), _memo, _active)
        except Exception:
            return str(value)
    if hasattr(value, "value"):
        try:
            return value.value
        except Exception:
            return str(value)
    return str(value)
