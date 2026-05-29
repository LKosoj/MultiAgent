"""Деревообразный билдер SQL из linked_entities (EPIC 8.1).

Выделено из `sql_generator.py`. Pure-functions: никакого LLM I/O, никакого
схема-кэша. Schema-validator передаётся через kwarg (DI) caller'ом, который
знает, какой именно валидатор использовать.
"""
import logging
from typing import Any, Dict, List, Optional, Set

from .dialects import (
    quote_identifier,
    quote_single_identifier,
    sql_string_literal,
)
from .constants import JOIN_TYPE_ALIASES

logger = logging.getLogger(__name__)


def build_sql_from_linked_entities(
    context: Optional[Dict[str, Any]],
    *,
    schema_validator,
    dsn: str | None = None,
) -> Dict[str, Any]:
    """Генерирует SQL из структурированных linked_entities.

    Возвращает либо `{"sql_query": "..."}`, либо `{"error": "...", ...}`,
    либо пустой dict `{}` если контекст не подходит для structured-генерации.

    schema_validator: объект с методом `validate_sql_against_schema(sql, schema, dsn=...)`.
    """
    if not isinstance(context, dict):
        return {}
    effective_dsn = dsn or _get_dsn_from_context(context)

    linked = _get_linked_entities(context)
    metrics_raw = linked.get("metrics")
    if metrics_raw is not None and not isinstance(metrics_raw, list):
        return {
            "error": f"metrics must be a list, got {type(metrics_raw).__name__}",
            "sql_query": "",
        }
    dimensions_raw = linked.get("dimensions")
    if dimensions_raw is not None and not isinstance(dimensions_raw, list):
        return {
            "error": f"dimensions must be a list, got {type(dimensions_raw).__name__}",
            "sql_query": "",
        }
    metrics = [m for m in (metrics_raw or []) if isinstance(m, dict)]
    dimensions = [d for d in (dimensions_raw or []) if isinstance(d, dict)]
    filters = linked.get("filters", {})
    joins = [j for j in context.get("joins", []) if isinstance(j, dict)]

    # NOTE: контракт — пустой dict означает «контекст не подходит для
    # structured-генерации» (см. test_build_sql_returns_empty_when_no_metrics_or_dimensions).
    if not (metrics or dimensions):
        return {}

    from_table = None
    for entity in metrics + dimensions:
        if entity.get("table"):
            from_table = entity["table"]
            break
    if not from_table:
        return {}

    select_parts: List[str] = []
    group_by_parts: List[str] = []

    for dim in dimensions:
        table = dim.get("table")
        column = dim.get("column")
        if not table or not column:
            continue
        expr = quote_identifier(f"{table}.{column}", dsn=effective_dsn)
        # Alias — одиночный идентификатор: точка внутри alias не должна
        # split'иться, иначе `"foo"."bar"` в позиции AS даёт syntax error.
        alias = quote_single_identifier(str(dim.get("name") or column), dsn=effective_dsn)
        select_parts.append(f"{expr} AS {alias}")
        group_by_parts.append(expr)

    for metric in metrics:
        table = metric.get("table")
        column = metric.get("column")
        if not table or not column:
            continue
        expr = quote_identifier(f"{table}.{column}", dsn=effective_dsn)
        name = str(metric.get("name") or column)
        try:
            func = metric_aggregate(metric)
        except ValueError as exc:
            # W2-T5: metric_aggregate теперь fail-fast при отсутствии
            # aggregation. Конвертируем raise в структурную error-ошибку,
            # чтобы `build_sql_from_linked_entities` сохранял dict-контракт.
            return {
                "error": str(exc),
                "metric": metric,
            }
        if not func:
            return {
                "error": "Structured SQL builder received unsupported aggregation",
                "metric": metric,
            }
        # Alias — одиночный идентификатор (точка внутри alias не split'ится).
        alias = quote_single_identifier(name, dsn=effective_dsn)
        column_str = str(column).strip()
        if func == "COUNT" and (column_str == "*" or column_str.endswith(".*")):
            # NOTE: COUNT("table".*) парсится sqlglot и валиден в Postgres/
            # SQLite/MySQL/DuckDB/Snowflake (проверено через sqlglot.parse_one
            # для всех пяти диалектов). Звёздочку квотировать не нужно —
            # квотируем только идентификатор таблицы. Если в будущем какой-то
            # диалект не поддержит этот синтаксис — нужно обрабатывать через
            # диалект-специфичную функцию.
            # Для qualified-имени вида ``schema.table.*`` обрезаем суффикс
            # ``.*`` и передаём оставшуюся часть в quote_identifier — он сам
            # корректно разобьёт её через sqlglot (см. dialects.quote_identifier).
            # Наивный split('.')[0] терял бы `schema`-часть.
            aggregate_arg = (
                column_str
                if column_str == "*"
                else f"{quote_identifier(column_str[:-2], dsn=effective_dsn)}.*"
            )
        else:
            aggregate_arg = expr
        select_parts.append(f"{func}({aggregate_arg}) AS {alias}")

    if not select_parts:
        return {}

    sql_parts = [
        f"SELECT {', '.join(select_parts)}",
        f"FROM {quote_identifier(from_table, dsn=effective_dsn)}",
    ]

    joined_tables: Set[str] = {str(from_table)}
    pending_joins = list(joins)
    # max(..., 1) гарантирует выполнение цикла хотя бы 1 раз даже при пустых
    # joins (тело while с pending_joins=[] всё равно не отработает, но
    # формальный bound должен быть >=1 для читаемости инварианта).
    # bound = len(pending_joins) * 2 предотвращает бесконечный цикл при
    # disconnected components: каждая успешная итерация снимает >=1 JOIN,
    # иначе срабатывает not-made-progress ветка ниже.
    max_iterations = max(len(pending_joins) * 2, 1)
    iterations = 0
    while pending_joins and iterations < max_iterations:
        iterations += 1
        remaining_joins: List[Dict[str, Any]] = []
        made_progress = False
        for join in pending_joins:
            from_join_table = join.get("from_table")
            from_col = join.get("from_column")
            to_table = join.get("to_table")
            to_col = join.get("to_column")
            # Для CROSS/NATURAL колонки не требуются — это валидно по контракту.
            join_type_preview = str(join.get("join_type") or "LEFT").strip().upper()
            joins_without_on_preview = {
                "CROSS", "CROSS JOIN", "NATURAL", "NATURAL JOIN",
            }
            requires_columns = join_type_preview not in joins_without_on_preview
            if not from_join_table or not to_table:
                return {
                    "error": "Structured SQL builder received join without from_table/to_table",
                    "join": join,
                }
            if requires_columns and (not from_col or not to_col):
                return {
                    "error": "Structured SQL builder received join without from_column/to_column",
                    "join": join,
                }

            from_join_table = str(from_join_table)
            to_table = str(to_table)
            if from_join_table in joined_tables and to_table not in joined_tables:
                join_target = to_table
            elif to_table in joined_tables and from_join_table not in joined_tables:
                join_target = from_join_table
            elif from_join_table in joined_tables and to_table in joined_tables:
                continue
            else:
                remaining_joins.append(join)
                continue

            join_type_value = str(join.get("join_type") or "LEFT").strip().upper()
            # JOIN_TYPE_ALIASES импортирован из constants — единый источник истины.
            JOINS_WITHOUT_ON = {"CROSS", "NATURAL"}
            if join_type_value not in JOIN_TYPE_ALIASES:
                return {
                    "error": "Structured SQL builder received unsupported join_type",
                    "join_type": join.get("join_type"),
                    "join": join,
                }
            join_type = JOIN_TYPE_ALIASES[join_type_value]
            join_keyword = "JOIN" if join_type == "JOIN" else f"{join_type} JOIN"
            if join_type in JOINS_WITHOUT_ON:
                if join.get("from_column") or join.get("to_column"):
                    return {
                        "error": (
                            "Structured SQL builder received join columns for a "
                            f"{join_type} JOIN which does not accept ON clause"
                        ),
                        "join_type": join_type,
                        "join": join,
                    }
                sql_parts.append(
                    f"{join_keyword} {quote_identifier(join_target, dsn=effective_dsn)}"
                )
            else:
                sql_parts.append(
                    f"{join_keyword} {quote_identifier(join_target, dsn=effective_dsn)} "
                    f"ON {quote_identifier(f'{from_join_table}.{from_col}', dsn=effective_dsn)} = "
                    f"{quote_identifier(f'{to_table}.{to_col}', dsn=effective_dsn)}"
                )
            joined_tables.add(join_target)
            made_progress = True

        if not made_progress:
            # Disconnected component: оставшиеся joins не достижимы из FROM-таблицы.
            logger.warning(
                "JOIN graph has disconnected components: %s (from_table=%s)",
                remaining_joins, from_table,
            )
            return {
                "error": "Structured SQL builder received joins that are not connected to the FROM table",
                "joins": remaining_joins,
                "from_table": from_table,
            }
        pending_joins = remaining_joins

    referenced_entity_tables = {
        str(entity.get("table"))
        for entity in (metrics + dimensions)
        if isinstance(entity, dict) and entity.get("table")
    }
    unjoined_entity_tables = sorted(referenced_entity_tables - joined_tables)
    if unjoined_entity_tables:
        return {
            "error": "Structured SQL builder entity tables are not connected to the FROM table",
            "tables": unjoined_entity_tables,
            "from_table": from_table,
        }

    filter_clauses = build_filter_clauses(filters, joined_tables, dsn=effective_dsn)
    if filter_clauses.get("error"):
        return filter_clauses
    where_parts = filter_clauses.get("where_parts", [])
    if where_parts:
        sql_parts.append(f"WHERE {' AND '.join(where_parts)}")

    if group_by_parts:
        sql_parts.append(f"GROUP BY {', '.join(group_by_parts)}")

    sql_query = " ".join(sql_parts)
    db_schema = _get_schema_from_context(context)
    if db_schema:
        schema_validation = schema_validator.validate_sql_against_schema(
            sql_query,
            db_schema,
            dsn=effective_dsn,
        )
        if not schema_validation.get("is_valid", True):
            return {
                "error": "Generated SQL failed schema validation",
                "schema_issues": schema_validation.get("issues", []),
                "sql_query": sql_query,
            }
    return {"sql_query": sql_query}


def _get_linked_entities(context: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    linked = context.get("linked_entities", context)
    return linked if isinstance(linked, dict) else {}


def _get_schema_from_context(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(context, dict):
        return None
    schema_info = context.get("schema_info")
    return schema_info if isinstance(schema_info, dict) and schema_info else None


def _get_dsn_from_context(context: Dict[str, Any]) -> str | None:
    for key in ("dsn", "database_dsn", "db_dsn"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            return value
    metadata = context.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("dsn")
        if isinstance(value, str) and value.strip():
            return value
    return None


def metric_aggregate(metric: Dict[str, Any]) -> Optional[str]:
    raw_func = (
        metric.get("aggregation")
        or metric.get("aggregate")
        or metric.get("function")
        or metric.get("agg")
    )
    if raw_func is None:
        name = str(metric.get("name") or "").strip().lower()
        if name in {"count", "cnt"}:
            raw_func = "count"
        elif name in {"average", "avg", "mean"}:
            raw_func = "avg"
        elif name in {"min", "minimum"}:
            raw_func = "min"
        elif name in {"max", "maximum"}:
            raw_func = "max"
        else:
            # W2-T5: fail-fast вместо silent fallback на SUM.
            # Раньше дефолт SUM применялся к любым колонкам — включая email/
            # date — что давало бессмысленный SQL. Контракт builder: caller
            # (linking_orchestrator/LLM-pipeline) обязан явно указать
            # aggregation/aggregate/function/agg или дать canonical-name из
            # списка count|avg|min|max. См. AGENTS.md «никаких silent fallback».
            raise ValueError(
                f"metric.aggregation is required for metric: {metric}"
            )

    func = str(raw_func).strip().upper()
    aliases = {"AVERAGE": "AVG", "MEAN": "AVG", "TOTAL": "SUM", "CNT": "COUNT"}
    func = aliases.get(func, func)
    return func if func in {"COUNT", "SUM", "AVG", "MIN", "MAX"} else None


def build_filter_clauses(
    filters: Any,
    available_tables: Optional[Set[str]] = None,
    dsn: str | None = None,
) -> Dict[str, Any]:
    if not filters:
        return {"where_parts": []}
    if not isinstance(filters, dict):
        return {"error": "Structured SQL builder filters must be a dict"}

    where_parts: List[str] = []
    for filter_name, filter_info in filters.items():
        if not isinstance(filter_info, dict):
            return {
                "error": "Structured SQL builder filter must include table, column, and value",
                "filter": filter_name,
            }
        table = filter_info.get("table")
        column = filter_info.get("column")
        if not table or not column:
            return {
                "error": "Structured SQL builder filter must include table and column",
                "filter": filter_name,
            }
        if available_tables is not None and str(table) not in available_tables:
            return {
                "error": "Structured SQL builder filter table is not connected to the FROM table",
                "filter": filter_name,
                "table": table,
            }
        if "value" not in filter_info:
            return {
                "error": "Structured SQL builder filter must include value",
                "filter": filter_name,
            }
        value = filter_info.get("value")
        expr = quote_identifier(f"{table}.{column}", dsn=dsn)
        conditions = filter_value_conditions(expr, value, filter_info, dsn=dsn)
        if conditions is None:
            return {
                "error": "Structured SQL builder received unsupported filter operator",
                "filter": filter_name,
                "operator": filter_info.get("operator"),
            }
        where_parts.extend(conditions)

    return {"where_parts": where_parts}


def filter_value_conditions(
    expr: str,
    value: Any,
    filter_info: Dict[str, Any],
    dsn: str | None = None,
) -> Optional[List[str]]:
    operator = str(filter_info.get("operator") or "=").strip().upper()
    operator_aliases = {
        "EQ": "=",
        "NE": "!=",
        "GT": ">",
        "GTE": ">=",
        "GE": ">=",
        "LT": "<",
        "LTE": "<=",
        "LE": "<=",
    }
    operator = operator_aliases.get(operator, operator)

    if isinstance(value, dict):
        if "operator" in value:
            if "value" not in value:
                return None
            nested_info = dict(filter_info)
            nested_info["operator"] = value.get("operator")
            nested_value = value.get("value")
            return filter_value_conditions(expr, nested_value, nested_info, dsn=dsn)

        conditions: List[str] = []
        start_val = value.get("start")
        end_val = value.get("end")
        if start_val is not None and end_val is not None:
            try:
                if float(start_val) > float(end_val):
                    logger.warning(
                        "Range filter inverted: start=%s > end=%s — result will be empty",
                        start_val, end_val,
                    )
            except (TypeError, ValueError):
                # Не числовые границы (например, даты-строки) — пропускаем проверку.
                pass
        if start_val is not None:
            conditions.append(f"{expr} >= {sql_literal(start_val, dsn=dsn)}")
        if end_val is not None:
            conditions.append(f"{expr} <= {sql_literal(end_val, dsn=dsn)}")
        if value.get("min") is not None:
            conditions.append(f"{expr} >= {sql_literal(value['min'], dsn=dsn)}")
        if value.get("max") is not None:
            conditions.append(f"{expr} <= {sql_literal(value['max'], dsn=dsn)}")
        if "values" in value:
            if not isinstance(value["values"], list):
                return None
            in_conditions = filter_value_conditions(
                expr, value["values"], {"operator": "IN"}, dsn=dsn
            )
            if in_conditions is None:
                return None
            conditions.extend(in_conditions)
        if "value" in value:
            nested_conditions = filter_value_conditions(
                expr, value.get("value"), filter_info, dsn=dsn
            )
            if nested_conditions is None:
                return None
            conditions.extend(nested_conditions)
        return conditions if conditions else None

    if value is None:
        if operator in {"=", "IS"}:
            return [f"{expr} IS NULL"]
        if operator in {"!=", "<>", "IS NOT"}:
            return [f"{expr} IS NOT NULL"]
        return None

    if isinstance(value, (list, tuple, set)):
        if operator not in {"=", "IN"}:
            return None
        values = list(value)
        if not values:
            return ["1 = 0"]
        literals = ", ".join(sql_literal(item, dsn=dsn) for item in values)
        return [f"{expr} IN ({literals})"]

    if operator not in {"=", "!=", "<>", ">", ">=", "<", "<=", "LIKE"}:
        return None
    return [f"{expr} {operator} {sql_literal(value, dsn=dsn)}"]


def sql_literal(value: Any, dsn: str | None = None) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    # Decimal — числовой литерал без кавычек: str(Decimal) даёт точное число.
    from decimal import Decimal
    if isinstance(value, Decimal):
        return str(value)
    # bytes / bytearray — стандарт SQL:2008 hex-литерал X'...'.
    # Поддерживается Postgres (как bit-string), MySQL, SQLite, Impala и др.
    if isinstance(value, (bytes, bytearray)):
        return f"X'{bytes(value).hex()}'"
    return sql_string_literal(value, dsn=dsn)
