"""CTECollector — собирает projected/alias columns из CTE и subquery row-sources.

Выделено из `validators/schema_aware.py` (EPIC 8.9). Берёт ScopeResolver для
делегирования alias-mapping (нужен в `_star_projection_columns`).
"""
from typing import Dict, Set

try:
    import sqlglot  # noqa: F401
    from sqlglot import expressions as exp
    SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    SQLGLOT_AVAILABLE = False
    exp = None  # type: ignore

from ._schema_scope import ScopeResolver


class CTECollector:
    """Собирает колонки CTE/subquery row-sources."""

    def __init__(self, scope: ScopeResolver):
        self._scope = scope

    def collect_cte_columns(
        self,
        scope,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> Dict[str, Set[str]]:
        cte_columns: Dict[str, Set[str]] = {}
        # Если scope — корневой `exp.With`, у самой ноды нет args["with"]:
        # список CTE лежит прямо в `with_expr.expressions`. Для Select/прочих
        # стейтментов CTE хранится в args["with"]. Поддерживаем оба случая,
        # иначе root-WITH давал бы пустой CTE-словарь → ложные UNKNOWN_TABLE.
        if exp is not None and isinstance(scope, exp.With):
            with_expr = scope
        else:
            with_expr = scope.args.get("with") if hasattr(scope, "args") else None
        if not with_expr:
            return cte_columns
        # Поддерживаем как Select-тело CTE, так и set-операции (Union/Intersect/Except),
        # что характерно для рекурсивных CTE. Для не-Select тел регистрируем CTE
        # с пустым набором проектируемых колонок (или с alias-колонками, если заданы),
        # чтобы такая таблица не помечалась как UNKNOWN_TABLE.
        select_like = (exp.Select, getattr(exp, "Union", ()))
        for cte in getattr(with_expr, "expressions", []) or []:
            alias = getattr(cte, "alias", None)
            select_expr = getattr(cte, "this", None)
            if not alias:
                continue
            if isinstance(select_expr, exp.Select):
                cte_columns[str(alias)] = self.row_source_columns(cte, select_expr, db_schema)
            elif isinstance(select_expr, select_like):
                alias_cols = self.alias_column_names(cte)
                cte_columns[str(alias)] = alias_cols if alias_cols else {"*"}
            else:
                cte_columns[str(alias)] = {"*"}
        return cte_columns

    def collect_current_row_sources(
        self,
        scope,
        available_ctes: Dict[str, Set[str]],
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ):
        row_sources: Dict[str, Set[str]] = {}
        row_source_names: Set[str] = set()

        for table_expr in scope.find_all(exp.Table):
            if table_expr.find_ancestor(exp.Select) is not scope:
                continue
            real_name = self._scope.get_real_table_name(table_expr)
            if real_name not in available_ctes:
                continue
            alias = getattr(table_expr, 'alias', None)
            visible_name = str(alias) if alias else real_name
            row_sources[visible_name] = available_ctes[real_name]
            row_source_names.add(real_name)

        for subquery in scope.find_all(exp.Subquery):
            if subquery.find_ancestor(exp.Select) is not scope:
                continue
            alias = getattr(subquery, "alias", None)
            select_expr = getattr(subquery, "this", None)
            if alias and isinstance(select_expr, exp.Select):
                row_sources[str(alias)] = self.row_source_columns(subquery, select_expr, db_schema)
                row_source_names.add(str(alias))

        return row_sources, row_source_names

    def row_source_columns(
        self,
        source_expr,
        select_expr,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> Set[str]:
        alias_columns = self.alias_column_names(source_expr)
        return alias_columns if alias_columns else self.projected_columns(select_expr, db_schema)

    def alias_column_names(self, source_expr) -> Set[str]:
        alias_expr = source_expr.args.get("alias") if hasattr(source_expr, "args") else None
        columns = alias_expr.args.get("columns") if alias_expr is not None and hasattr(alias_expr, "args") else None
        if not columns:
            return set()
        return {self._scope.clean_identifier(column) for column in columns}

    def projected_columns(
        self,
        select_expr,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> Set[str]:
        columns: Set[str] = set()
        for projection in getattr(select_expr, "expressions", []) or []:
            alias = getattr(projection, "alias", None)
            if alias:
                columns.add(str(alias))
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                table_name = str(projection.table) if projection.table else None
                columns.update(self.star_projection_columns(select_expr, db_schema, table_name))
            elif isinstance(projection, exp.Column):
                columns.add(projection.name)
            elif isinstance(projection, exp.Star):
                columns.update(self.star_projection_columns(select_expr, db_schema))
        return columns

    def star_projection_columns(
        self,
        select_expr,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        table_alias: str | None = None,
    ) -> Set[str]:
        alias_mapping = self._scope.build_alias_mapping(select_expr, db_schema)
        if table_alias:
            table_name = alias_mapping.get(table_alias)
            referenced_tables = (
                self._scope.referenced_schema_tables({table_alias: table_name}, db_schema)
                if table_name
                else []
            )
        else:
            referenced_tables = self._scope.referenced_schema_tables(alias_mapping, db_schema)
        columns: Set[str] = set()
        for table_name in referenced_tables:
            table_schema = db_schema.get(table_name, {})
            columns.update(_get_table_columns(table_schema).keys())
        return columns


def _get_table_columns(table_schema):
    """Локальный helper — извлекает словарь колонок из node-словаря схемы."""
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
