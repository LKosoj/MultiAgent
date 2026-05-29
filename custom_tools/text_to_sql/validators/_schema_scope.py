"""ScopeResolver — структурный резолвер таблиц/алиасов/scope для schema-aware валидатора.

Выделено из `validators/schema_aware.py` (EPIC 8.9). Чисто структурный AST/schema
lookup без логики issue-emission. Все методы — pure functions, состояние не хранится.
"""
from typing import Dict, List, NamedTuple, Optional

# Опциональный импорт sqlglot. Резолвер сам по себе не парсит SQL, но
# использует exp.* типы при обходе AST, переданного caller'ом.
try:
    import sqlglot  # noqa: F401
    from sqlglot import expressions as exp
    SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover - sqlglot is required at runtime
    SQLGLOT_AVAILABLE = False
    exp = None  # type: ignore


class _ResolveResult(NamedTuple):
    """Результат резолва имени таблицы в схему БД.

    kind:
        'found'     — найдено точное соответствие, name заполнено.
        'unknown'   — таблица не найдена ни в каком виде.
        'ambiguous' — короткое имя совпало с несколькими таблицами;
                      candidates содержит все варианты.
    """

    kind: str  # 'found' | 'unknown' | 'ambiguous'
    name: Optional[str]
    candidates: List[str]


class ScopeResolver:
    """Резолвер таблиц/алиасов/row-source visibility внутри SELECT scope.

    Pure helper: не хранит состояние между вызовами; всю информацию принимает
    через параметры.
    """

    # ----- идентификаторы / нормализация имён -----

    def clean_identifier(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return text
        # MSSQL/T-SQL: [name]
        if len(text) >= 2 and text.startswith("[") and text.endswith("]"):
            return text[1:-1]
        # ANSI/PostgreSQL: "name" с экранированием "" -> "
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            return text[1:-1].replace('""', '"')
        # MySQL/SQLite: `name` с экранированием `` -> `
        if len(text) >= 2 and text.startswith("`") and text.endswith("`"):
            return text[1:-1].replace("``", "`")
        return text

    def get_real_table_name(self, table_node) -> str:
        """Извлекает реальное имя таблицы из узла AST с учётом схемы."""
        if hasattr(table_node, 'db') and table_node.db:
            return f"{self.clean_identifier(table_node.db)}.{self.clean_identifier(table_node.name)}"
        elif hasattr(table_node, 'catalog') and table_node.catalog:
            return f"{self.clean_identifier(table_node.catalog)}.{self.clean_identifier(table_node.name)}"
        elif hasattr(table_node, 'name'):
            return self.clean_identifier(table_node.name)
        elif hasattr(table_node, 'this'):
            return self.clean_identifier(table_node.this)
        else:
            return self.clean_identifier(table_node)

    def normalize_table_name(self, table_node, db_schema: Dict[str, Dict[str, Dict[str, str]]]) -> str:
        """Нормализует имя таблицы из AST узла."""
        if hasattr(table_node, 'db') and table_node.db:
            return f"{table_node.db}.{table_node.name}"
        else:
            table_name = table_node.name
            if table_name in db_schema:
                return table_name
            for schema_table in db_schema.keys():
                if schema_table.split(".")[-1] == table_name:
                    return schema_table
            return table_name

    def normalize_table_name_from_identifier(self, identifier, db_schema: Dict[str, Dict[str, Dict[str, str]]]) -> str:
        """Нормализует имя таблицы из идентификатора."""
        if hasattr(identifier, 'parts') and len(identifier.parts) > 1:
            return ".".join(identifier.parts)
        else:
            table_name = str(identifier)
            if table_name in db_schema:
                return table_name
            for schema_table in db_schema.keys():
                if schema_table.split(".")[-1] == table_name:
                    return schema_table
            return table_name

    # ----- резолв таблицы в схему -----

    def resolve_table_name_detailed(
        self,
        table_name: str,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> _ResolveResult:
        """Резолвит имя таблицы и явно сообщает caller'у о ambiguous vs unknown."""
        if not table_name:
            return _ResolveResult(kind="unknown", name=None, candidates=[])
        cleaned = self.clean_identifier(table_name)
        if cleaned in db_schema:
            return _ResolveResult(kind="found", name=cleaned, candidates=[cleaned])

        table_lower = cleaned.lower()
        for schema_table in db_schema.keys():
            if schema_table.lower() == table_lower:
                return _ResolveResult(kind="found", name=schema_table, candidates=[schema_table])

        short_matches = [
            schema_table
            for schema_table in db_schema.keys()
            if schema_table.lower().rsplit('.', 1)[-1] == table_lower
        ]
        if len(short_matches) == 1:
            return _ResolveResult(kind="found", name=short_matches[0], candidates=short_matches)
        if len(short_matches) > 1:
            return _ResolveResult(kind="ambiguous", name=None, candidates=short_matches)
        return _ResolveResult(kind="unknown", name=None, candidates=[])

    def resolve_table_name(
        self,
        table_name: str,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> Optional[str]:
        """Backward-compatible shim: возвращает имя только при kind == 'found'."""
        result = self.resolve_table_name_detailed(table_name, db_schema)
        return result.name if result.kind == "found" else None

    def table_exists_in_schema(
        self,
        table_name: str,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> bool:
        """Проверяет существование таблицы в схеме."""
        return self.resolve_table_name(table_name, db_schema) is not None

    # ----- алиасы / row-sources / scope visibility -----

    def build_alias_mapping(
        self,
        stmt,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        row_source_names: Optional[set] = None,
        ambiguous_names: Optional[set] = None,
    ) -> Dict[str, str]:
        """Строит маппинг алиасов таблиц к их реальным именам.

        row_source_names и ambiguous_names — явные параметры, чтобы не хранить
        состояние валидатора на узлах AST (см. 2.14).
        """
        alias_mapping: Dict[str, str] = {}
        row_source_names = row_source_names if row_source_names is not None else set()
        ambiguous_names = ambiguous_names if ambiguous_names is not None else set()

        for table_expr in stmt.find_all(exp.Table):
            if table_expr.find_ancestor(exp.Select) is not stmt:
                continue
            real_name = self.get_real_table_name(table_expr)
            if real_name in row_source_names:
                continue
            if real_name in ambiguous_names:
                continue
            alias = getattr(table_expr, 'alias', None)

            if alias and real_name:
                alias_mapping[str(alias)] = real_name

            if real_name:
                alias_mapping[real_name] = real_name
                alias_mapping[real_name.rsplit('.', 1)[-1]] = real_name

        return alias_mapping

    def referenced_schema_tables(
        self,
        alias_to_table: Dict[str, str],
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
    ) -> List[str]:
        referenced: List[str] = []
        seen: set = set()
        for table_name in alias_to_table.values():
            resolved = self.resolve_table_name(table_name, db_schema)
            if resolved and resolved not in seen:
                referenced.append(resolved)
                seen.add(resolved)
        return referenced

    def child_scope_can_see_outer_aliases(self, child_scope) -> bool:
        current = getattr(child_scope, "parent", None)
        while current is not None and not isinstance(current, exp.Select):
            if isinstance(current, exp.CTE):
                return False
            if isinstance(current, exp.Subquery):
                parent = getattr(current, "parent", None)
                if isinstance(parent, (exp.From, exp.Join)):
                    return False
            current = getattr(current, "parent", None)
        return True
