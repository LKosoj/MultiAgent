"""
Schema resolution utilities (Phase 7, T7.3 dedup).

Top-level pure functions for resolving table/column names against a loaded
database schema dict in a case-insensitive and schema-qualified manner.

These functions are the single source of truth shared by ``SchemaLinkingCore``
(``strategies.py``) and ``SchemaLinker`` (``schema_linker.py``).

Previously: реализации жили только в ``SchemaLinkingCore``, а ``SchemaLinker``
делегировал через ``self.linking_core._resolve_table_name`` и т.д.
Теперь оба класса импортируют top-level функции напрямую.

Signatures (canonical):
    _resolve_table_name(table_name, db_schema) -> Optional[str]
    _resolve_column_name(column_name, table_name, db_schema) -> Optional[str]
    _table_exists_in_schema(table_name, db_schema) -> bool
    _column_exists_in_table(column_name, table_name, db_schema) -> bool
    _get_column_meta(table_name, column_name, db_schema) -> Optional[dict]

Note: ``_get_column_meta`` сохраняет исходный порядок аргументов
``(table_name, column_name)`` — менять его сломало бы публичный контракт
(см. tests/test_schema_linker_improvements.py::test_get_column_meta и
SchemaLinker._get_column_meta).
"""
import logging
from typing import Any, Dict, Optional

from ..utils import get_table_columns

logger = logging.getLogger(__name__)


def _resolve_table_name(
    table_name: str,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> Optional[str]:
    """Возвращает реальное имя таблицы из схемы с учётом регистра и схемы.

    Алгоритм:
      1. Точное совпадение.
      2. Совпадение в lower-case по полному ключу схемы.
      3. Совпадение по короткому имени (после последней точки) — если
         единственный матч, возвращаем его. Иначе ``None``.
    """
    if not table_name:
        return None
    if table_name in db_schema:
        return table_name
    table_lower = str(table_name).lower()
    for schema_table in db_schema.keys():
        schema_table_lower = schema_table.lower()
        if schema_table_lower == table_lower:
            return schema_table
    short_matches = [
        schema_table
        for schema_table in db_schema.keys()
        if schema_table.lower().rsplit(".", 1)[-1] == table_lower
    ]
    if len(short_matches) == 1:
        return short_matches[0]
    if len(short_matches) > 1:
        # 4.23: неоднозначное короткое имя — это сигнал, что вверх по
        # стеку нужно использовать схема-квалифицированное имя. Возвращаем
        # None как и раньше (поведение публичного контракта не меняется),
        # но логируем все matches, чтобы calling-код мог разобраться.
        logger.warning(
            "_resolve_table_name: ambiguous short name '%s' matches "
            "multiple schema-qualified tables: %s. Returning None.",
            table_name,
            short_matches,
        )
    return None


def _resolve_column_name(
    column_name: str,
    table_name: str,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> Optional[str]:
    """Возвращает имя колонки в реальном регистре для таблицы.

    Требует, чтобы ``table_name`` уже было резолвлено (см.
    ``_resolve_table_name``). Если в таблице несколько колонок
    отличаются только регистром — возвращаем ``None`` (неоднозначность).
    """
    if not column_name or table_name not in db_schema:
        return None
    table_columns = get_table_columns(db_schema[table_name])
    if column_name in table_columns:
        return column_name
    column_lower = str(column_name).lower()
    matches = [name for name in table_columns.keys() if name.lower() == column_lower]
    if len(matches) == 1:
        return matches[0]
    return None


def _table_exists_in_schema(
    table_name: str,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> bool:
    """Проверяет существование таблицы в схеме (case/schema-insensitive)."""
    return _resolve_table_name(table_name, db_schema) is not None


def _column_exists_in_table(
    column_name: str,
    table_name: str,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> bool:
    """Проверяет существование колонки в таблице."""
    resolved_table_name = _resolve_table_name(table_name, db_schema)
    if not resolved_table_name:
        return False
    return _resolve_column_name(column_name, resolved_table_name, db_schema) is not None


def _get_column_meta(
    table_name: str,
    column_name: str,
    db_schema: Dict[str, Dict[str, Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    """Получает метаданные колонки.

    Порядок аргументов ``(table_name, column_name)`` сохранён ради
    обратной совместимости с ``SchemaLinker._get_column_meta`` и тестами.
    """
    resolved_table_name = _resolve_table_name(table_name, db_schema)
    if not resolved_table_name:
        return None
    resolved_column_name = _resolve_column_name(column_name, resolved_table_name, db_schema)
    if not resolved_column_name:
        return None
    table_columns = get_table_columns(db_schema[resolved_table_name])
    return table_columns.get(resolved_column_name)
