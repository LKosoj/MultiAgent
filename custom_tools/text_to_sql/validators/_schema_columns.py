"""ColumnResolver — валидация column references против scope/CTE/schema.

Выделено из `validators/schema_aware.py` (EPIC 8.9). Принимает ScopeResolver
для делегирования резолва таблиц.
"""
from typing import Any, Dict, List, Set

try:
    import sqlglot  # noqa: F401
    from sqlglot import expressions as exp
    SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    SQLGLOT_AVAILABLE = False
    exp = None  # type: ignore

from ._schema_scope import ScopeResolver


class ColumnResolver:
    """Резолвер column references против таблиц и row-sources."""

    def __init__(self, scope: ScopeResolver):
        self._scope = scope

    def should_validate_column(self, column_node) -> bool:
        """Проверяет, нужно ли валидировать данную колонку."""
        column_name = column_node.name
        # Пропускаем только wildcard; имена вроде sum/count могут быть реальными колонками.
        if column_name in ["*"]:
            return False
        return True

    def is_select_alias_reference(self, column_node, select_aliases: Set[str]) -> bool:
        if column_node.table or column_node.name not in select_aliases:
            return False
        # SELECT-алиасы видны в ORDER BY, GROUP BY, HAVING, QUALIFY.
        # В WHERE алиасы НЕ видны — туда не идём (через ancestor-walk до exp.Select
        # без захода в эти узлы это даст False).
        alias_visible_ancestors = (exp.Order, exp.Ordered, exp.Group, exp.Having)
        qualify_cls = getattr(exp, "Qualify", None)
        if qualify_cls is not None:
            alias_visible_ancestors = alias_visible_ancestors + (qualify_cls,)
        current = getattr(column_node, "parent", None)
        while current is not None and not isinstance(current, exp.Select):
            if isinstance(current, alias_visible_ancestors):
                return True
            current = getattr(current, "parent", None)
        return False

    def row_source_has_column(self, column_name: str, columns: Set[str]) -> bool:
        return "*" in columns or column_name.lower() in {column.lower() for column in columns}

    def find_row_source_column_matches(
        self,
        column_name: str,
        row_sources: Dict[str, Set[str]],
    ) -> List[str]:
        return [
            alias
            for alias, columns in row_sources.items()
            if self.row_source_has_column(column_name, columns)
        ]

    def column_exists_in_schema(
        self,
        column_name: str,
        table_name: str,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> bool:
        """Проверяет существование колонки в схеме."""
        if table_name:
            actual_table = self._scope.resolve_table_name(table_name, db_schema)
            if actual_table:
                table_schema = db_schema[actual_table]
                table_columns = self.get_table_columns(table_schema)
                column_names_lower = {c.lower(): c for c in table_columns.keys()}
                return column_name.lower() in column_names_lower or column_name in table_columns
        else:
            for table_schema in db_schema.values():
                table_columns = self.get_table_columns(table_schema)
                column_names_lower = {c.lower(): c for c in table_columns.keys()}
                if column_name.lower() in column_names_lower or column_name in table_columns:
                    return True
        return False

    def find_column_matches(
        self,
        column_name: str,
        candidate_tables: List[str],
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> List[str]:
        matches: List[str] = []
        tables = candidate_tables or list(db_schema.keys())
        for table_name in tables:
            actual_table = self._scope.resolve_table_name(table_name, db_schema)
            if not actual_table:
                continue
            table_columns = self.get_table_columns(db_schema[actual_table])
            column_names_lower = {c.lower(): c for c in table_columns.keys()}
            if column_name.lower() in column_names_lower or column_name in table_columns:
                matches.append(actual_table)
        return matches

    def get_table_columns(self, table_schema: Dict[str, Any]) -> Dict[str, Any]:
        columns = table_schema.get("columns") if isinstance(table_schema, dict) else None
        if isinstance(columns, dict):
            return columns
        if isinstance(table_schema, dict):
            return {
                key: value
                for key, value in table_schema.items()
                if isinstance(value, dict) and key not in {"description", "metadata"}
            }
        return {}
