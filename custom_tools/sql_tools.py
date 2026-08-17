"""
Text-to-SQL пайплайн - совместимый интерфейс
Рефакторированная версия с импортом из модульных компонентов
"""

# Импортируем все функции из рефакторированных модулей
from .text_to_sql.core import (
    # NLU функции
    natural_language_processing,
    intent_extraction,
    
    # RAG функции
    vector_db_search,
    
    # Schema linking
    schema_linking,
    
    # SQL Generation
    sql_generation_plugin,
    code_formatter,
    
    # SQL Verification  
    sql_safety_check as _sql_safety_check_internal,
    sql_explain as _sql_explain_internal,
    
    # Execution and Audit
    secure_db_executor,
    finalize_text_to_sql_run,
    pii_masking,
    audit_logger,
    save_successful_sql as _save_successful_sql_trusted,
    successful_sql_retrieval,
    
    # Cache Management
    purge_schema_linking_rag_cache,
)

# Экспортируем schema_info для использования в других модулях
__all__ = [
    'natural_language_processing', 'intent_extraction', 'vector_db_search',
    'schema_linking', 'sql_generation_plugin', 'code_formatter',
    'sql_safety_check', 'sql_explain', 'secure_db_executor',
    'finalize_text_to_sql_run',
    'pii_masking', 'audit_logger', 'save_successful_sql',
    'successful_sql_retrieval',
    'purge_schema_linking_rag_cache', 'get_distinct_values', 'schema_info'
]

# Импортируем утилиты для обратной совместимости
from .text_to_sql.utils import (
    dsn_to_sanitized_name as _dsn_to_sanitized_name,
    get_runtime_context_dsn as _get_runtime_context_dsn,
    get_schema_version as _get_schema_version,
    redact_text_to_sql_value as _redact_text_to_sql_value,
    split_schema_table as _split_schema_table,
    optimize_column_info,
)

from .text_to_sql.dialects import (
    get_current_dialect_label as _get_current_dialect_label,
    get_current_dialect_name as _get_current_dialect_name,
)
from .text_to_sql.core._db_exec import (
    QueryExecutionRequest,
    QueryExecutor,
    QueryPurpose,
)
from workflow.deadline import WorkflowDeadlineExceeded

# Настройка логирования
import logging
logger = logging.getLogger(__name__)


def sql_safety_check(sql_query: str) -> dict:
    """Проверяет SQL, который модель передаёт только через sql_query.

    Trusted core получает policy из runtime context или настроенного при
    запуске validator (объекта проверки).

    Args:
        sql_query: SQL-запрос для проверки.

    Returns:
        Результат статической и LLM-проверки безопасности.
    """
    return _sql_safety_check_internal(sql_query)


def sql_explain(sql_query: str) -> dict:
    """Строит EXPLAIN для SQL, который модель передаёт только через sql_query.

    Trusted core получает policy из runtime context или настроенного при
    запуске validator (объекта проверки); DSN — из runtime context или через
    явный операторский opt-in DB_DSN при SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1;
    DeadlineBudget — из runtime context. Dry-run задаёт только оператор через
    TEXT_TO_SQL_DRY_RUN_ONLY.

    Args:
        sql_query: SQL-запрос для анализа.

    Returns:
        План выполнения или безопасный отказ.
    """
    return _sql_explain_internal(sql_query)



def _resolve_sql_tool_dsn(dsn: str | None) -> str | None:
    if isinstance(dsn, str) and dsn.strip():
        return dsn
    return _get_runtime_context_dsn()


def _redact_sql_tool_error(error: Exception) -> str:
    return str(_redact_text_to_sql_value(error))


def _redact_sql_tool_value(value):
    return _redact_text_to_sql_value(value)


def save_successful_sql(
    sql_query: str,
    user_query: str = "",
    execution_result: str = "",
    dsn: str | None = None,
) -> dict:
    """Сохраняет успешно выполненный SQL-запрос в базу примеров для дальнейшего использования в RAG.

    Trust boundary: evidence-флаги (namespace_version_key, approved, executed,
    execution_success, audited) недоступны через этот инструмент и устанавливаются
    только доверенным terminal-путём (custom_tools/text_to_sql/core/_terminal.py).

    Args:
        sql_query: SQL-запрос для сохранения.
        user_query: Исходный запрос пользователя на естественном языке.
        execution_result: JSON-строка с результатом выполнения SQL-запроса.
        dsn: DSN целевой БД; если не передан, берётся из runtime context.

    Returns:
        Словарь со статусом сохранения.
    """
    return _save_successful_sql_trusted(sql_query, user_query, execution_result, dsn=dsn)


def get_distinct_values(table_name: str, column_name: str, limit: int = 500, dsn: str | None = None) -> dict:
    """Получает список всех уникальных значений из указанного поля таблицы.
    
    Args:
        table_name: Имя таблицы (с указанием схемы, если необходимо)
        column_name: Имя колонки для получения уникальных значений
        limit: Максимальное количество значений для возврата
        dsn: DSN подключения; если не передан, берётся из runtime context
    
    Returns:
        Словарь с результатами: success, values, count, error_message
    """
    logger.info(f"Getting distinct values from {table_name}.{column_name}")
    
    try:
        from db_plugins import get_plugin
        
        effective_dsn = _resolve_sql_tool_dsn(dsn)
        if not effective_dsn:
            return {
                "success": False,
                "values": [],
                "count": 0,
                "error_message": "DSN is required: pass dsn parameter or provide workflow runtime context."
            }
        
        plugin = get_plugin(effective_dsn)
        if not hasattr(plugin, 'build_distinct_values_query'):
            return {
                "success": False,
                "values": [],
                "count": 0,
                "error_message": "Database plugin does not support distinct values query generation."
            }
        if not hasattr(plugin, 'execute_select'):
            return {
                "success": False,
                "values": [],
                "count": 0,
                "error_message": "Database plugin does not support SELECT execution."
            }

        sql_query = plugin.build_distinct_values_query(
            table_name,
            column_name,
            limit,
        )
        logger.info("Executing DISTINCT preparation read")
        result = QueryExecutor(get_plugin=get_plugin).execute(
            QueryExecutionRequest(
                sql_query=sql_query,
                purpose=QueryPurpose.DISTINCT,
                row_limit=limit,
                dsn=effective_dsn,
            )
        )
        if result.success:
            values = [
                str(_redact_sql_tool_value(str(row[0])))
                for row in result.data
            ]
            return {
                "success": True,
                "values": values,
                "count": len(values),
                "error_message": None
            }

        safe_error = _redact_sql_tool_error(
            RuntimeError(
                result.error_message or "Unknown error during query execution"
            )
        )
        return {
            "success": False,
            "values": [],
            "count": 0,
            "error_message": safe_error
        }
    except WorkflowDeadlineExceeded:
        raise
    except Exception as e:
        safe_error = _redact_sql_tool_error(e)
        logger.error("Error getting distinct values: %s", safe_error)
        return {
            "success": False,
            "values": [],
            "count": 0,
            "error_message": f"Database error: {safe_error}"
        }


def _resolve_schema_info_table(table_name: str, schema_info_data: dict) -> tuple[str | None, dict | None, list[str]]:
    if table_name in schema_info_data:
        return table_name, schema_info_data[table_name], []

    table_name_without_schema = table_name.split('.')[-1] if '.' in table_name else table_name
    matches = [
        full_table_name
        for full_table_name in schema_info_data.keys()
        if full_table_name.split('.')[-1] == table_name_without_schema
    ]
    if len(matches) == 1:
        match = matches[0]
        return match, schema_info_data[match], []
    if len(matches) > 1:
        return None, None, matches
    return None, None, []


def schema_info(table_name: str, dsn: str | None = None) -> dict:
    """Получает детальную информацию о структуре конкретной таблицы из кэша схемы.
    
    Args:
        table_name: Имя таблицы (может включать схему, например 'main.bdmo_salary_full')
        dsn: DSN подключения; если не передан, берётся из runtime context
    
    Returns:
        Словарь с информацией о таблице: success, table_info, error_message
    """
    logger.info(f"Getting schema info for table: {table_name}")
    
    try:
        import os
        from pathlib import Path
        from .text_to_sql.schema_loader import SchemaLoader
        from .text_to_sql.schema_linker import SchemaLinker
        from .text_to_sql.validators import SchemaLimiter
        from .text_to_sql.utils import get_table_columns, get_table_description
        
        effective_dsn = _resolve_sql_tool_dsn(dsn)
        if not effective_dsn:
            return {
                "success": False,
                "table_info": {},
                "error_message": "DSN is required: pass dsn parameter or provide workflow runtime context."
            }
        
        if not table_name or not table_name.strip():
            return {
                "success": False,
                "table_info": {},
                "error_message": "table_name is required."
            }
        
        repo_root = Path(__file__).resolve().parents[1]
        loader = SchemaLoader(repo_root)
        schema_source = "sqlrag_cache"
        schema_info_data = loader._load_sqlrag_schema(effective_dsn) or {}
        if schema_info_data:
            schema_info_data = loader._normalize_table_names(schema_info_data, effective_dsn)
        elif os.getenv("TEXT_TO_SQL_SCHEMA_INFO_ALLOW_INTROSPECTION", "0").strip().lower() in {"1", "true", "yes", "on"}:
            schema_source = "live_introspection"
            linker = SchemaLinker(SchemaLimiter())
            schema_info_data = linker._get_database_schema({}, dsn=effective_dsn)
        else:
            return {
                "success": False,
                "table_info": {},
                "error_message": (
                    "Schema cache file not found or disabled. Run schema introspection first, "
                    "or set TEXT_TO_SQL_SCHEMA_INFO_ALLOW_INTROSPECTION=1 to explicitly allow live introspection."
                )
            }
        if not isinstance(schema_info_data, dict) or not schema_info_data:
            return {
                "success": False,
                "table_info": {},
                "error_message": "Database schema is empty or unavailable."
            }

        found_table_name, table_info, ambiguous_matches = _resolve_schema_info_table(
            table_name.strip(),
            schema_info_data,
        )
        if ambiguous_matches:
            return {
                "success": False,
                "table_info": {},
                "error_message": (
                    f"Ambiguous table name '{table_name}'. Matches: {ambiguous_matches}. "
                    "Use a fully qualified table name."
                )
            }
        
        if table_info is None:
            available_tables = list(schema_info_data.keys())
            return {
                "success": False,
                "table_info": {},
                "error_message": f"Table '{table_name}' not found in schema cache. Available tables: {available_tables}"
            }
        
        # Формируем результат с детальной информацией о таблице
        # table_info имеет структуру: {"description": "...", "columns": {...}}
        table_description = get_table_description(table_info)
        columns_data = get_table_columns(table_info)
        
        columns_count = len(columns_data)
        column_details = []
        
        for column_name, column_info in columns_data.items():
            if not isinstance(column_info, dict):
                column_info = {"type": str(column_info), "description": ""}
            # Создаем входные данные для оптимизации
            column_input = {
                "name": column_name,
                "type": column_info.get("type", ""),
                "description": column_info.get("description", ""),
                "not_null": column_info.get("not_null", "False"),  # Оставляем как строку для optimize_column_info
                "default_value": column_info.get("default_value", ""),
                "is_primary_key": column_info.get("is_primary_key", "False"),  # Оставляем как строку для optimize_column_info
                "constraint_type": column_info.get("constraint_type", ""),
                "references": column_info.get("references", "")
            }
            
            # Применяем оптимизацию с включением поля name
            column_detail = optimize_column_info(column_input, include_name=True)
            column_details.append(column_detail)
        
        return {
            "success": True,
            "table_info": {
                "table_name": found_table_name,
                "description": table_description,
                "columns_count": columns_count,
                "columns": column_details,
                "schema_source": schema_source,
            },
            "error_message": None
        }
        
    except Exception as e:
        safe_error = _redact_sql_tool_error(e)
        logger.error("Error getting schema info for table %s: %s", table_name, safe_error)
        return {
            "success": False,
            "table_info": {},
            "error_message": f"Error loading schema info: {safe_error}"
        }
