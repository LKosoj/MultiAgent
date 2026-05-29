"""Lightweight workflow log redaction helpers."""

from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


def _redact_workflow_log_value(value: Any) -> Any:
    """Mask DSN/secret-shaped values before workflow diagnostic logging."""
    try:
        from custom_tools.text_to_sql.utils import (
            is_sensitive_secret_key,
            mask_dsn,
            mask_dsn_value,
        )
    except Exception:
        logger.warning("Workflow log redaction unavailable; redacting value", exc_info=True)
        return "<redacted>"

    sensitive_config_keys = {
        "connection_string",
        "database_dsn",
        "database_url",
        "db_dsn",
        "db_url",
        "dsn",
    }

    def _normalize_key(key: Any) -> str:
        text = str(key)
        text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
        text = re.sub(r"[^A-Za-z0-9]+", "_", text)
        return re.sub(r"_+", "_", text).strip("_").casefold()

    def _mask(item: Any, key: Any = None) -> Any:
        if key is not None:
            normalized = _normalize_key(key)
            if normalized in sensitive_config_keys or is_sensitive_secret_key(key):
                if isinstance(item, str):
                    return mask_dsn_value(item)
                return "<redacted>"
        if isinstance(item, dict):
            return {dict_key: _mask(dict_value, dict_key) for dict_key, dict_value in item.items()}
        if isinstance(item, list):
            return [_mask(list_item) for list_item in item]
        if isinstance(item, tuple):
            return tuple(_mask(tuple_item) for tuple_item in item)
        if isinstance(item, str):
            return mask_dsn(item)
        return item

    return _mask(value)
