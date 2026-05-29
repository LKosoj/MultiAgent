from __future__ import annotations

from typing import Dict
from urllib.parse import urlparse

from .sqlite import SQLitePlugin
from .postgres import PostgresPlugin
from .duckdb import DuckDBPlugin
from .mysql import MySQLPlugin
from .sapiq import SAPIQPlugin
from .impala import ImpalaPlugin


_PLUGINS: Dict[str, object] = {
    "sqlite": SQLitePlugin(),
    "postgres": PostgresPlugin(),
    "postgresql": PostgresPlugin(),
    "duckdb": DuckDBPlugin(),
    "mysql": MySQLPlugin(),
    "sapiq": SAPIQPlugin(),
    "impala": ImpalaPlugin(),
}


def get_plugin(dsn: str):
    """Возвращает плагин по схеме DSN.

    Примеры DSN:
    - sqlite:///abs/path/to.db
    - duckdb:///abs/path/to.duckdb
    - postgresql://user:pass@host:5432/db
    - mysql://user:pass@host:3306/db
    - sapiq://user:pass@host:2638/mydatabase
    - impala://user:pass@host:21050/db?auth_mechanism=GSSAPI
    """
    parsed = urlparse(dsn)
    scheme = (parsed.scheme or "sqlite").lower()
    if scheme in {"postgresql", "psql", "pg"}:
        scheme = "postgres"
    plugin = _PLUGINS.get(scheme)
    if not plugin:
        raise ValueError(f"Нет плагина для схемы: {scheme}. Добавьте реализацию.")
    return plugin


