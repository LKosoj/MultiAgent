"""
Фасад package custom_tools.text_to_sql.core (Phase 7 декомпозиция).

Сохраняет публичный API оригинального core.py:
- 13 публичных функций text-to-sql пайплайна;
- 4 singletons (nlu_processor, rag_searcher, sql_validator, schema_limiter);
- module-level зависимости call_openai_api и get_plugin
  (доступны для monkeypatch.setattr из тестов).

Архитектура DI: singletons живут здесь, в фасаде. Подмодули принимают их
keyword-only аргументами через wrapper'ы ниже. Внешние сигнатуры публичных
функций остаются неизменны.

FIXME EPIC-8.5 deferred: 13 функций-обёрток ниже выглядят повторяющимися,
но они — намеренный архитектурный якорь, а не legacy-шум. Их нельзя
заменить на ``build_text_to_sql_facade()``-фабрику без регрессии трёх
независимых контрактов:

  1. Monkeypatch anchor: ~45 тестов делают
     ``monkeypatch.setattr(core, "<name>", ...)`` (call_openai_api,
     get_plugin, memory_manager, __file__, плюс сами обёртки).
     Подмодули (``_pii``, ``_db_exec``, ``_sql_generation_api``,
     ``_audit``, ``utils``) явно перечитывают фасад через
     ``getattr(_facade, "call_openai_api", None)`` — это и есть точка
     подмены (EPIC 7.16). Фабрика, прячущая функции внутрь объекта,
     ломает контракт.
  2. AG-UI named-tool resolution: ``tool_definitions/*.yaml`` указывают
     ``implementation_source: custom_tools.sql_tools.<name>``;
     ``custom_tools/sql_tools.py`` реэкспортирует функции из ``core`` по
     именам. AG-UI резолвит инструменты по dotted-path — module-level
     атрибуты обязательны.
  3. Контракт ``test_core_public_api_preserved.py`` фиксирует имена и
     ``__all__`` как часть публичного API.

Рефакторинг отложен до стабилизации AG-UI surface; до тех пор обёртки —
это анкоры monkeypatch + явный контракт, а не дубль ради дубля.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Модульные зависимости, которые могут monkeypatch'иться тестами.
# Подмодули обращаются к ним как `custom_tools.text_to_sql.core.<name>`,
# поэтому реальная замена через monkeypatch.setattr "...core.call_openai_api"
# действительно подменяет точку вызова.
from db_plugins import get_plugin  # noqa: E402

try:
    from utils import call_openai_api  # noqa: E402
except ImportError as exc:
    _call_openai_api_import_error = exc

    def call_openai_api(*args, **kwargs):  # type: ignore[no-redef]
        raise RuntimeError(
            f"call_openai_api is unavailable: {_call_openai_api_import_error}"
        ) from _call_openai_api_import_error

from memory.manager import memory_manager  # noqa: E402

# Внутренние компоненты — singletons.
from ..nlu import NLUProcessor  # noqa: E402
from ..rag import RAGSearcher  # noqa: E402
from ..validators import SQLSafetyValidator, SchemaLimiter  # noqa: E402
from ..sql_generator import SQLGenerator  # noqa: E402
from ..utils import configure_logging  # noqa: E402

# W2-T7: реэкспорт исключения для callers и тестов. code_formatter теперь
# fail-fast'ит forbidden SQL вместо возврата formatted_sql_query с исходником.
from ._sql_generation_api import SQLForbiddenStatementError  # noqa: E402,F401
from ._db_exec import MissingDSNError  # noqa: E402,F401

# Настраиваем логирование (как в оригинальном core.py)
configure_logging()

# Инициализируем процессоры (singletons фасада)
nlu_processor = NLUProcessor()
rag_searcher = RAGSearcher()
sql_validator = SQLSafetyValidator()
schema_limiter = SchemaLimiter()
sql_generator = SQLGenerator()


# === Публичные фасадные функции ===
# Каждая делегирует в _impl с инъекцией singletons как kwargs.
# Внешний контракт (сигнатуры) сохранён.


def natural_language_processing(text: str, session_id: Optional[str] = None) -> Dict[str, List[str]]:
    """Токенизирует и POS-теггирует пользовательский запрос.

    Args:
        text: входной текст на естественном языке.
        session_id: необязательный идентификатор сессии для аудита.

    Returns:
        Словарь с полями `tokens` и `pos_tags`.
    """
    from ._nlu_api import natural_language_processing as _impl
    return _impl(text, session_id, nlu_processor=nlu_processor)


def intent_extraction(text: str, session_id: Optional[str] = None) -> Dict[str, object]:
    """Извлекает intent и сущности из пользовательского запроса.

    Args:
        text: входной текст на естественном языке.
        session_id: необязательный идентификатор сессии для аудита.

    Returns:
        Словарь с полями `intent` и `entities`.
    """
    from ._nlu_api import intent_extraction as _impl
    return _impl(text, session_id, nlu_processor=nlu_processor)


def vector_db_search(query: str, top_k: int = 3) -> List[Dict[str, object]]:
    """Ищет похожие примеры SQL по векторной базе.

    Args:
        query: естественный язык-запрос для поиска.
        top_k: количество лучших совпадений.

    Returns:
        Список словарей с найденными примерами и их similarity-score.
    """
    from ._rag_api import vector_db_search as _impl
    return _impl(query, top_k, rag_searcher=rag_searcher)


def schema_linking(
    entities: dict,
    session_id: Optional[str] = None,
    schema_info: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> Dict[str, object]:
    """Связывает извлечённые сущности с таблицами/колонками схемы.

    Args:
        entities: словарь сущностей из intent_extraction.
        session_id: необязательный идентификатор сессии для кэша.
        schema_info: явная схема БД для линкинга (опционально); если None —
            линкер возьмёт схему из кэша/интроспекции.
        dsn: DSN целевой БД для загрузки sqlrag-схемы и интроспекции.

    Returns:
        Словарь со связанными сущностями, joins и информацией о схеме.

    Note:
        Передача dict-схемы вторым позиционным аргументом (вместо session_id)
        поддерживается как deprecation-shim и эмитит DeprecationWarning.
        Используйте `schema_info=` kwarg.
    """
    from ._schema_linking_api import schema_linking as _impl
    return _impl(
        entities,
        session_id,
        schema_info,
        dsn,
        schema_limiter=schema_limiter,
    )


def sql_generation_plugin(
    context: str,
    user_query: str,
    dsn: Optional[str] = None,
) -> Dict[str, str]:
    """Генерирует SQL по схеме и запросу пользователя.

    Args:
        context: контекст со схемой и подсказками из schema_linking.
        user_query: исходный запрос пользователя.
        dsn: явный DSN для диалект-aware генерации литералов и quoting.

    Returns:
        Словарь с полями `sql_query` и `notes`.
    """
    from ._sql_generation_api import sql_generation_plugin as _impl
    return _impl(context, user_query, dsn=dsn, sql_generator=sql_generator)


def code_formatter(sql_query: str) -> Dict[str, str]:
    """Форматирует SQL и маскирует литералы перед отдачей модели.

    Args:
        sql_query: исходный SQL-запрос.

    Returns:
        Словарь с ключами `formatted_sql` и `masked_sql`.
    """
    from ._sql_generation_api import code_formatter as _impl
    return _impl(sql_query, sql_validator=sql_validator)


def sql_safety_check(sql_query: str, dsn: Optional[str] = None) -> Dict[str, object]:
    """Проверяет SQL на безопасность (запрещённые операторы, IN-list, comments).

    Args:
        sql_query: SQL-запрос для проверки.
        dsn: явный DSN для выбора SQL-диалекта и dialect-specific safety правил.

    Returns:
        Словарь со статусом safety_status и списком violations.
    """
    from ._sql_generation_api import sql_safety_check as _impl
    return _impl(sql_query, sql_validator=sql_validator, dsn=dsn)


def sql_explain(sql_query: str, dsn: Optional[str] = None) -> Dict[str, object]:
    """Возвращает план выполнения SQL через `EXPLAIN`.

    Args:
        sql_query: SQL-запрос для анализа.
        dsn: явный DSN; если None, env ``DB_DSN`` используется только при
            ``SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1`` opt-in.

    Returns:
        Словарь с планом и метаинформацией.
    """
    from ._sql_generation_api import sql_explain as _impl
    return _impl(sql_query, dsn, sql_validator=sql_validator)


def secure_db_executor(
    sql_query: str,
    row_limit: Optional[int] = None,
    dsn: Optional[str] = None,
) -> Dict[str, object]:
    """Безопасно выполняет SELECT/DESCRIBE/EXPLAIN на БД с row-limit.

    Args:
        sql_query: SQL-запрос для выполнения.
        row_limit: необязательный лимит строк (если не задан — читается из env).
        dsn: явный DSN для подключения. Обязателен для реального выполнения,
            кроме dry-run; env ``DB_DSN`` используется только при
            ``SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1`` opt-in. Если DSN отсутствует
            без dry-run/opt-in — поднимается ``MissingDSNError``.

    Returns:
        Словарь с success, data, columns, rows_affected, error_message.
    """
    from ._db_exec import secure_db_executor as _impl
    return _impl(
        sql_query,
        row_limit,
        dsn,
        sql_validator=sql_validator,
        schema_limiter=schema_limiter,
    )


def pii_masking(data: list, columns_to_mask: list, column_names: Optional[list] = None) -> dict:
    """Маскирует PII-колонки в результатах выполнения SQL.

    Args:
        data: список строк-результата (list of lists).
        columns_to_mask: список имён колонок, подлежащих маскировке.
        column_names: имена всех колонок результата (для индексации).

    Returns:
        Словарь с маскированными данными и метаинформацией:
        ``masked_data``, ``pii_detected``, ``masked_columns``, ``reason``.
    """
    # EPIC 7.16: пробрасываем call_openai_api как DI kwarg.
    # При monkeypatch.setattr(core, "call_openai_api", ...) тест меняет
    # ИМЕННО атрибут модуля — прямая ссылка ниже разрешается через
    # LOAD_GLOBAL на module __dict__, поэтому подмена работает прозрачно.
    from ._pii import pii_masking as _impl
    return _impl(
        data,
        columns_to_mask,
        column_names,
        call_openai_api=call_openai_api,
    )


def audit_logger(audit_entry: dict) -> Dict[str, str]:
    """Пишет запись аудита text-to-sql пайплайна.

    Args:
        audit_entry: словарь с полями события (timestamp, action, payload...).

    Returns:
        Словарь со статусом записи и `log_id`.
    """
    from ._audit import audit_logger as _impl
    return _impl(audit_entry)


def save_successful_sql(
    sql_query: str,
    user_query: str = "",
    execution_result: str = "",
    dsn: str | None = None,
) -> Dict[str, str]:
    """Сохраняет успешный SQL в kбазу примеров для дальнейшего обучения.

    Args:
        sql_query: успешно выполненный SQL.
        user_query: исходный естественно-языковой запрос.
        execution_result: краткое описание результата (JSON-строкой).
        dsn: DSN целевой БД для выбора sqlrag/session_id. В workflow-запусках
            может быть передан через runtime metadata; silent DB_DSN fallback
            не используется.

    Returns:
        Словарь со статусом сохранения.
    """
    from ._audit import save_successful_sql as _impl
    return _impl(sql_query, user_query, execution_result, dsn=dsn)


def purge_schema_linking_rag_cache(
    session_id: Optional[str] = None,
    cache_kind: Optional[str] = None,
    agent_name: str = "Schema-RAG-Agent",
) -> int:
    """Инвалидация кэша schema-linking RAG.

    Args:
        session_id: ограничить очистку конкретной сессией (опционально).
        cache_kind: ограничить очистку конкретным типом кэша (опционально).
        agent_name: имя агента, чьи записи деактивируются (EPIC 7.11).
            Default="Schema-RAG-Agent" сохраняет обратную совместимость.

    Returns:
        Количество удалённых записей.
    """
    from ._audit import purge_schema_linking_rag_cache as _impl
    return _impl(session_id, cache_kind, agent_name=agent_name)


# === Приватные хелперы, реэкспортированные для обратной совместимости ===
# `tests/test_sqlglot_integration.py` импортирует эти приватные имена напрямую.
from ._db_exec import (  # noqa: E402,F401
    _normalize_executor_result,
    _describe_identifier_parts_from_text,
    _parse_table_parts_from_describe,
    _parse_table_parts_from_describe_sqlglot,
    _parse_table_name_from_describe_sqlglot,
    _parse_table_name_simple,
    _extract_schema_and_table_from_describe,
    _resolve_describe_table,
    _format_describe_result,
)
from ._sql_generation_api import _format_sql_legacy  # noqa: E402,F401
from ._schema_linking_api import _normalize_schema_linking_entities  # noqa: E402,F401

# PII caller-cache reset (для тестов, монкипатчащих фасадный call_openai_api
# между вызовами при включённом TEXT_TO_SQL_CACHE_PII_LLM_CALLER=1).
# Экспортируется под публичным именем reset_pii_caller_cache.
from ._pii import _reset_call_openai_api_cache as reset_pii_caller_cache  # noqa: E402,F401


# === Публичный API package ===
# Перечисляем только то, что является частью публичного контракта core
# (см. test_core_public_api_preserved.py). Приватные `_*` имена сюда не
# попадают: они доступны через прямой импорт для tests, но не реэкспортируются
# по `from custom_tools.text_to_sql.core import *`.
__all__ = [
    # 13 публичных функций пайплайна
    "natural_language_processing",
    "intent_extraction",
    "vector_db_search",
    "schema_linking",
    "sql_generation_plugin",
    "code_formatter",
    "sql_safety_check",
    "sql_explain",
    "secure_db_executor",
    "pii_masking",
    "audit_logger",
    "save_successful_sql",
    "purge_schema_linking_rag_cache",
    # singletons
    "nlu_processor",
    "rag_searcher",
    "sql_validator",
    "schema_limiter",
    "sql_generator",
    # module-level зависимости (monkeypatch-anchored)
    "call_openai_api",
    "get_plugin",
    "memory_manager",
    # test-utility: сброс кэша фасадного call_openai_api в _pii
    "reset_pii_caller_cache",
    # W2-T7: exception типа для forbidden-SQL fail-fast в code_formatter
    "SQLForbiddenStatementError",
    # T11-orch: публичный контракт ошибки missing DSN (fail-fast)
    "MissingDSNError",
]
