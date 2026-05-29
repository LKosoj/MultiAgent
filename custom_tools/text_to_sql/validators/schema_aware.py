"""
SQL schema-aware валидатор: проверяет соответствие SQL запроса схеме БД.

EPIC 8.9: декомпозиция god-класса на 3 хелпера:
- ScopeResolver  — резолв таблиц/алиасов/scope visibility (`_schema_scope.py`).
- CTECollector   — projected/alias columns из CTE/subquery (`_schema_cte.py`).
- ColumnResolver — резолв column references против schema/row-sources (`_schema_columns.py`).

`SQLSchemaValidator` остаётся фасадом-оркестратором: `validate_sql_against_schema`,
`_validate_schema_with_sqlglot`, `_validate_select_scope` ходят в хелперы, но владеют
issue list mutation и AST traversal.

Приватные методы (`_clean_identifier`, `_resolve_table_name_detailed`, …) сохранены как
1-line делегаты для backward-compat с тестами (`test_text_to_sql_schema_aware_v2.py`).
"""
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Опциональный импорт sqlglot
try:
    import sqlglot
    from sqlglot import expressions as exp
    SQLGLOT_AVAILABLE = True
except ImportError:
    SQLGLOT_AVAILABLE = False
    sqlglot = None
    exp = None

from ..dialects import get_sqlglot_dialect, is_sqlglot_enabled

from .safety import record_sqlglot_metric, _parse_with_timeout, _ParseTimeoutError

# Re-export `_ResolveResult` для обратной совместимости тестов:
# `from custom_tools.text_to_sql.validators.schema_aware import _ResolveResult`.
from ._schema_scope import ScopeResolver, _ResolveResult  # noqa: F401
from ._schema_cte import CTECollector
from ._schema_columns import ColumnResolver


def _redact_schema_validation_error(error: Exception) -> str:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        return str(redact_pii_in_payload(_redact_payload(str(error))))
    except Exception as e:
        logger.warning("_redact_schema_validation_error: redaction import failed: %s", e)
        return "<redacted>"


class SQLSchemaValidator:
    """Валидатор для проверки соответствия SQL запроса схеме БД."""

    def __init__(self):
        self._scope = ScopeResolver()
        self._ctes = CTECollector(self._scope)
        self._columns = ColumnResolver(self._scope)
        # SCHEMA_AWARE_STRICT=1 — пустая схема трактуется как ошибка,
        # а не как «нечего проверять». Default 0 сохраняет старое поведение.
        self._strict_empty_schema = (
            os.getenv("SCHEMA_AWARE_STRICT", "0").strip() == "1"
        )

    def validate_sql_against_schema(
        self,
        sql_query: str,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        dsn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Проверяет, что все таблицы и колонки в SQL существуют в схеме."""
        if not db_schema:
            if self._strict_empty_schema:
                return {
                    "is_valid": False,
                    "issues": [{
                        "issue_type": "SCHEMA_NOT_AVAILABLE",
                        "description": (
                            "DB schema is empty; SCHEMA_AWARE_STRICT=1 requires a "
                            "non-empty schema for validation."
                        ),
                    }],
                }
            # backward compat: is_valid=True, но с явным сигналом для caller'а.
            logger.warning(
                "schema_aware: db_schema is empty, schema validation skipped "
                "(schema_check_skipped=True). Set SCHEMA_AWARE_STRICT=1 to treat as error."
            )
            return {
                "is_valid": True,
                "issues": [],
                "schema_check_skipped": True,
                "skip_reason": "empty_schema",
            }

        if not is_sqlglot_enabled():
            return {
                "is_valid": False,
                "issues": [{
                    "issue_type": "SQLGLOT_DISABLED_FOR_SCHEMA_VALIDATION",
                    "description": "USE_SQLGLOT=0 disables schema validation. Enable sqlglot or disable TEXT_TO_SQL_VALIDATE_SCHEMA."
                }]
            }

        if not SQLGLOT_AVAILABLE:
            return {
                "is_valid": False,
                "issues": [{
                    "issue_type": "SQLGLOT_UNAVAILABLE",
                    "description": "SQLglot is not available. Cannot perform proper schema validation."
                }]
            }

        return self._validate_schema_with_sqlglot(sql_query, db_schema, dsn=dsn)

    def _validate_schema_with_sqlglot(
        self,
        sql_query: str,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        dsn: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Валидация схемы с использованием sqlglot AST."""
        record_sqlglot_metric("parse_attempts")

        issues: List[Dict[str, Any]] = []

        try:
            dialect = get_sqlglot_dialect(dsn, strict=bool(dsn and str(dsn).strip()))
            # Pathological SQL может зависнуть sqlglot.parse на любое время.
            # Защищаем wall-time таймаутом тем же helper'ом, что и safety.py
            # (см. ``_parse_with_timeout``), чтобы schema-аware валидатор не
            # подвешивал pipeline.
            try:
                parse_timeout = float(
                    os.getenv("SQL_VALIDATE_PARSE_TIMEOUT_SEC", "5")
                )
            except ValueError:
                parse_timeout = 5.0
            try:
                statements = _parse_with_timeout(
                    sql_query,
                    None if dialect == "ansi" else dialect,
                    parse_timeout,
                )
            except _ParseTimeoutError:
                record_sqlglot_metric("parse_failures")
                return {
                    "is_valid": False,
                    "issues": [{
                        "issue_type": "SQL_PARSE_TIMEOUT",
                        "description": (
                            f"SQL parse exceeded timeout of {parse_timeout}s"
                        ),
                    }],
                }

            if not statements:
                return {"is_valid": True, "issues": []}

            for stmt in statements:
                # Работаем с копией AST: валидация не должна мутировать
                # дерево, отданное caller'у (см. 2.10).
                stmt = stmt.copy()

                # Корневой WITH — на практике sqlglot складывает WITH ... SELECT
                # в exp.Select(args['with']=...), но отдельные диалекты могут
                # вернуть exp.With в корне. В этом случае собираем CTE на уровне
                # With явно и пробрасываем во внутренние Select через kwarg
                # available_ctes — без мутации stmt.this/inner.with.
                if isinstance(stmt, exp.With):
                    root_ctes = self._ctes.collect_cte_columns(stmt, db_schema)
                    inner = getattr(stmt, "this", None)
                    if isinstance(inner, exp.Select):
                        self._validate_select_scope(
                            inner, db_schema, issues, available_ctes=root_ctes
                        )
                        continue
                    # Не Select под WITH (теоретически невозможно для допустимого
                    # text-to-sql пайплайна — пресекается safety-валидатором).
                    # Тем не менее, валидируем вложенные select'ы, прокидывая CTE.
                    for scope in stmt.find_all(exp.Select):
                        if scope.find_ancestor(exp.Select) is None:
                            self._validate_select_scope(
                                scope, db_schema, issues, available_ctes=root_ctes
                            )
                    continue

                if isinstance(stmt, exp.Select):
                    self._validate_select_scope(stmt, db_schema, issues)
                else:
                    # Не-Select корень (например, EXPLAIN/DESCRIBE-обёртки или
                    # запрещённые DML, отсечённые ранее). Если у корня есть WITH,
                    # CTE должны быть видны всем вложенным Select.
                    root_ctes: Dict[str, set[str]] = {}
                    if hasattr(stmt, "args") and stmt.args.get("with") is not None:
                        root_ctes = self._ctes.collect_cte_columns(stmt, db_schema)
                    for scope in stmt.find_all(exp.Select):
                        if scope.find_ancestor(exp.Select) is None:
                            self._validate_select_scope(
                                scope, db_schema, issues, available_ctes=root_ctes
                            )

        except Exception as e:
            safe_error = _redact_schema_validation_error(e)
            record_sqlglot_metric("parse_failures")
            logger.error("sql schema validation failed: %s", safe_error)
            return {
                "is_valid": False,
                "issues": [{
                    "issue_type": "SQL_SCHEMA_VALIDATION_ERROR",
                    "description": f"Failed to validate schema: {safe_error}"
                }]
            }

        return {
            "is_valid": len(issues) == 0,
            "issues": issues
        }

    def _validate_select_scope(
        self,
        scope,
        db_schema: Dict[str, Dict[str, Dict[str, str]]],
        issues: List[Dict[str, Any]],
        inherited_aliases: Dict[str, str] | None = None,
        inherited_row_sources: Dict[str, set[str]] | None = None,
        available_ctes: Dict[str, set[str]] | None = None,
    ) -> None:
        available_ctes = {**(available_ctes or {}), **self._ctes.collect_cte_columns(scope, db_schema)}
        current_row_sources, row_source_names = self._ctes.collect_current_row_sources(
            scope, available_ctes, db_schema
        )
        inherited_aliases = inherited_aliases or {}
        inherited_row_sources = inherited_row_sources or {}

        # Сначала классифицируем все физические таблицы текущего scope (found/unknown/ambiguous).
        ambiguous_names: set = set()
        has_from_tables = False
        for table_node in scope.find_all(exp.Table):
            if table_node.find_ancestor(exp.Select) is not scope:
                continue
            real_table_name = self._scope.get_real_table_name(table_node)
            if real_table_name in row_source_names:
                has_from_tables = True
                continue
            if not real_table_name:
                continue
            has_from_tables = True
            resolved = self._scope.resolve_table_name_detailed(real_table_name, db_schema)
            if resolved.kind == "ambiguous":
                ambiguous_names.add(real_table_name)
                issues.append({
                    "issue_type": "AMBIGUOUS_TABLE",
                    "description": (
                        f"Table '{real_table_name}' is ambiguous: matches "
                        f"{resolved.candidates}"
                    ),
                })
            elif resolved.kind == "unknown":
                issues.append({
                    "issue_type": "UNKNOWN_TABLE",
                    "description": f"Table '{real_table_name}' not found in schema",
                })

        current_aliases = self._scope.build_alias_mapping(
            scope,
            db_schema,
            row_source_names=row_source_names,
            ambiguous_names=ambiguous_names,
        )
        current_referenced_tables = self._scope.referenced_schema_tables(current_aliases, db_schema)

        select_aliases = set()
        if hasattr(scope, 'expressions'):
            for expr in scope.expressions:
                if isinstance(expr, exp.Alias) and hasattr(expr, 'alias'):
                    select_aliases.add(expr.alias)

        for column_node in scope.find_all(exp.Column):
            if column_node.find_ancestor(exp.Select) is not scope:
                continue
            column_name = column_node.name

            if self._columns.is_select_alias_reference(column_node, select_aliases):
                continue

            if not self._columns.should_validate_column(column_node):
                continue

            table_alias = str(column_node.table) if column_node.table else None
            real_table_name = None
            if table_alias:
                if table_alias in current_row_sources:
                    if not self._columns.row_source_has_column(column_name, current_row_sources[table_alias]):
                        issues.append({
                            "issue_type": "UNKNOWN_COLUMN",
                            "description": f"Column '{column_name}' not found in row source '{table_alias}'"
                        })
                    continue

                # Если квалификатор колонки указывает на ambiguous таблицу, не
                # эскалируем колоночный issue — AMBIGUOUS_TABLE уже зафиксирован.
                if table_alias in ambiguous_names:
                    continue

                real_table_name = current_aliases.get(table_alias)
                if real_table_name is None and table_alias in inherited_row_sources:
                    if not self._columns.row_source_has_column(column_name, inherited_row_sources[table_alias]):
                        issues.append({
                            "issue_type": "UNKNOWN_COLUMN",
                            "description": f"Column '{column_name}' not found in row source '{table_alias}'"
                        })
                    continue

                if real_table_name is None:
                    real_table_name = inherited_aliases.get(table_alias)

                if real_table_name is None:
                    issues.append({
                        "issue_type": "UNKNOWN_TABLE_REFERENCE",
                        "description": (
                            f"Column qualifier '{table_alias}' is not present in FROM/JOIN tables"
                        )
                    })
                    continue

            if real_table_name:
                if not self._columns.column_exists_in_schema(column_name, real_table_name, db_schema):
                    issues.append({
                        "issue_type": "UNKNOWN_COLUMN",
                        "description": f"Column '{column_name}' not found in table '{real_table_name}'"
                    })
                continue

            # Если у текущего scope есть FROM-таблицы, но все они ambiguous —
            # колонки уже не имеют достоверного источника. Пропускаем lookup,
            # AMBIGUOUS_TABLE уже зафиксирован выше.
            if has_from_tables and not current_referenced_tables and not current_row_sources and ambiguous_names:
                continue

            column_matches = (
                self._columns.find_column_matches(column_name, current_referenced_tables, db_schema)
                + self._columns.find_row_source_column_matches(column_name, current_row_sources)
            )
            if not column_matches:
                inherited_tables = self._scope.referenced_schema_tables(inherited_aliases, db_schema)
                column_matches = (
                    self._columns.find_column_matches(column_name, inherited_tables, db_schema)
                    + self._columns.find_row_source_column_matches(column_name, inherited_row_sources)
                )
            if len(column_matches) > 1:
                issues.append({
                    "issue_type": "AMBIGUOUS_COLUMN",
                    "description": (
                        f"Unqualified column '{column_name}' is ambiguous across tables: "
                        f"{', '.join(column_matches)}"
                    )
                })
            elif not column_matches:
                issues.append({
                    "issue_type": "UNKNOWN_COLUMN",
                    "description": f"Column '{column_name}' not found in any table"
                })

        for child_scope in scope.find_all(exp.Select):
            if child_scope is scope:
                continue
            if child_scope.find_ancestor(exp.Select) is scope:
                if self._scope.child_scope_can_see_outer_aliases(child_scope):
                    child_inherited_aliases = {**inherited_aliases, **current_aliases}
                    child_inherited_row_sources = {**inherited_row_sources, **current_row_sources}
                else:
                    child_inherited_aliases = {}
                    child_inherited_row_sources = {}
                self._validate_select_scope(
                    child_scope,
                    db_schema,
                    issues,
                    child_inherited_aliases,
                    child_inherited_row_sources,
                    available_ctes,
                )

    # ----- Backward-compat shims для существующих тестов -----
    # `test_text_to_sql_schema_aware_v2.py` обращается напрямую к этим именам.

    def _clean_identifier(self, value) -> str:
        return self._scope.clean_identifier(value)

    def _resolve_table_name_detailed(self, table_name, db_schema):
        return self._scope.resolve_table_name_detailed(table_name, db_schema)

    def _resolve_table_name(self, table_name, db_schema):
        return self._scope.resolve_table_name(table_name, db_schema)

    def _normalize_table_name(self, table_node, db_schema):
        return self._scope.normalize_table_name(table_node, db_schema)

    def _normalize_table_name_from_identifier(self, identifier, db_schema):
        return self._scope.normalize_table_name_from_identifier(identifier, db_schema)

    def _get_real_table_name(self, table_node):
        return self._scope.get_real_table_name(table_node)

    def _build_alias_mapping(self, stmt, db_schema, row_source_names=None, ambiguous_names=None):
        return self._scope.build_alias_mapping(stmt, db_schema, row_source_names, ambiguous_names)

    def _referenced_schema_tables(self, alias_to_table, db_schema):
        return self._scope.referenced_schema_tables(alias_to_table, db_schema)

    def _table_exists_in_schema(self, table_name, db_schema):
        return self._scope.table_exists_in_schema(table_name, db_schema)

    def _child_scope_can_see_outer_aliases(self, child_scope):
        return self._scope.child_scope_can_see_outer_aliases(child_scope)

    def _collect_cte_columns(self, scope, db_schema):
        return self._ctes.collect_cte_columns(scope, db_schema)

    def _collect_current_row_sources(self, scope, available_ctes, db_schema):
        return self._ctes.collect_current_row_sources(scope, available_ctes, db_schema)

    def _row_source_columns(self, source_expr, select_expr, db_schema):
        return self._ctes.row_source_columns(source_expr, select_expr, db_schema)

    def _alias_column_names(self, source_expr):
        return self._ctes.alias_column_names(source_expr)

    def _projected_columns(self, select_expr, db_schema):
        return self._ctes.projected_columns(select_expr, db_schema)

    def _star_projection_columns(self, select_expr, db_schema, table_alias=None):
        return self._ctes.star_projection_columns(select_expr, db_schema, table_alias)

    def _should_validate_column(self, column_node) -> bool:
        return self._columns.should_validate_column(column_node)

    def _is_select_alias_reference(self, column_node, select_aliases):
        return self._columns.is_select_alias_reference(column_node, select_aliases)

    def _row_source_has_column(self, column_name, columns):
        return self._columns.row_source_has_column(column_name, columns)

    def _find_row_source_column_matches(self, column_name, row_sources):
        return self._columns.find_row_source_column_matches(column_name, row_sources)

    def _column_exists_in_schema(self, column_name, table_name, db_schema):
        return self._columns.column_exists_in_schema(column_name, table_name, db_schema)

    def _find_column_matches(self, column_name, candidate_tables, db_schema):
        return self._columns.find_column_matches(column_name, candidate_tables, db_schema)

    def _get_table_columns(self, table_schema):
        return self._columns.get_table_columns(table_schema)
