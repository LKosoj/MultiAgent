from __future__ import annotations

from typing import Dict
from urllib.parse import urlparse

from .base import (
    DatabaseCapabilities,
    PluginHealth,
    RequiredDatabaseCapabilities,
    validate_required_capabilities,
)
from .duckdb import DuckDBPlugin
from .impala import ImpalaPlugin
from .mysql import MySQLPlugin
from .postgres import PostgresPlugin
from .sapiq import SAPIQPlugin
from .sqlite import SQLitePlugin


_PLUGINS: Dict[str, object] = {
    "sqlite": SQLitePlugin(),
    "postgres": PostgresPlugin(),
    "duckdb": DuckDBPlugin(),
    "mysql": MySQLPlugin(),
    "sapiq": SAPIQPlugin(),
    "impala": ImpalaPlugin(),
}
_ALIASES = {
    "postgresql": "postgres",
    "psql": "postgres",
    "pg": "postgres",
}


def canonical_dialect(target: str) -> str:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("database dialect or DSN must be non-empty text")
    value = target.strip()
    if "://" in value or value.startswith("file:"):
        scheme = (urlparse(value).scheme or "sqlite").lower()
    elif value.lower() in _PLUGINS or value.lower() in _ALIASES:
        scheme = value.lower()
    else:
        scheme = "sqlite"
    return _ALIASES.get(scheme, scheme)


def get_plugin(dsn: str):
    """Return the canonical plugin for a DSN while preserving aliases."""
    dialect = canonical_dialect(dsn)
    plugin = _PLUGINS.get(dialect)
    if plugin is None:
        raise ValueError(
            f"Нет плагина для схемы: {dialect}. Добавьте реализацию."
        )
    return plugin


def get_capabilities(target: str) -> DatabaseCapabilities:
    dialect = canonical_dialect(target)
    plugin = _PLUGINS.get(dialect)
    if plugin is None:
        raise ValueError(f"unsupported database dialect: {dialect}")
    dsn = target if "://" in target or target.startswith("file:") else None
    return plugin.get_capabilities(dsn)


def get_support_matrix() -> tuple[DatabaseCapabilities, ...]:
    return tuple(
        _PLUGINS[dialect].get_capabilities()
        for dialect in sorted(_PLUGINS)
    )


def serialize_support_matrix() -> list[dict[str, object]]:
    return [capabilities.to_mapping() for capabilities in get_support_matrix()]


def probe_plugin(target: str, conn=None) -> PluginHealth:
    dialect = canonical_dialect(target)
    plugin = _PLUGINS.get(dialect)
    if plugin is None:
        raise ValueError(f"unsupported database dialect: {dialect}")
    dsn = target if "://" in target or target.startswith("file:") else None
    return plugin.probe_capabilities(conn, dsn)


def require_capabilities(
    target: str | DatabaseCapabilities,
    required: RequiredDatabaseCapabilities,
) -> DatabaseCapabilities:
    capabilities = (
        target if isinstance(target, DatabaseCapabilities) else get_capabilities(target)
    )
    validate_required_capabilities(capabilities, required)
    return capabilities


__all__ = [
    "canonical_dialect",
    "get_capabilities",
    "get_plugin",
    "get_support_matrix",
    "probe_plugin",
    "require_capabilities",
    "serialize_support_matrix",
]
