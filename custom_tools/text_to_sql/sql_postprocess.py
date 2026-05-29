"""Диалект-специфичное квотирование SQL через AST sqlglot (EPIC 8.1).

Выделено из `sql_generator.py`. Pure-functions: никакого LLM I/O. Используется
как в основном пути генерации, так и в legacy fallback.
"""
import logging
import os
import re
from typing import Any, Dict, Set

from .dialects import get_sqlglot_dialect, is_sqlglot_enabled

logger = logging.getLogger(__name__)


def _redact_postprocess_error(error: Any) -> str:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        return str(redact_pii_in_payload(_redact_payload(str(error))))
    except Exception:
        return "<redacted>"


class SQLPostprocessError(RuntimeError):
    """Ошибка постобработки SQL (квотирование через AST).

    Поднимается, когда AST-парсинг провалился и нельзя безопасно применить
    диалект-квотинг. AGENTS.md запрещает silent corruption SQL — caller обязан
    обработать ошибку явно либо разрешить manual-fallback через
    ``SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK=1``.
    """


__all__ = [
    "SQLPostprocessError",
    "apply_dialect_quoting",
    "apply_manual_quoting",
    "quote_via_ast",
    "should_quote_name",
    "is_reserved_keyword",
]


_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def should_quote_name(
    name: str,
    dialect_name: str,
    known_tables: Set[str],
    known_columns: Set[str],
    table_aliases: Set[str],
) -> bool:
    """Единый предикат: квотировать ли голый идентификатор.

    Источник истины:
    - если совпадает с алиасом — НЕ квотируем (алиасы остаются голыми);
    - совпадение с известной таблицей/колонкой из linked_entities → квотируем;
    - имя содержит не-ASCII / спец-символы (не подходит под SAFE_IDENTIFIER_RE) → квотируем;
    - имя является зарезервированным словом диалекта sqlglot → квотируем;
    - postgres: имя в смешанном/верхнем регистре требует кавычек, иначе будет lowered.
    """
    if not name:
        return False
    if name in table_aliases:
        return False
    if name in known_tables or name in known_columns:
        return True
    # Не-ASCII / спецсимволы / пробелы
    if not _SAFE_IDENTIFIER_RE.fullmatch(name):
        return True
    # Reserved keyword диалекта sqlglot
    if is_reserved_keyword(name, dialect_name):
        return True
    # Postgres lower-cases unquoted identifiers — mixed/upper нужно квотировать
    if dialect_name == "postgres" and name != name.lower():
        return True
    return False


def is_reserved_keyword(name: str, dialect_name: str) -> bool:
    """Проверяет, является ли name зарезервированным словом sqlglot-диалекта.

    Использует Tokenizer.KEYWORDS (source of truth для парсера sqlglot):
    если name (uppercased) сопоставлен с любым TokenType, кроме VAR, —
    это keyword и его нужно квотировать.
    """
    if dialect_name in {"", "ansi", "sql"}:
        return False

    from sqlglot.tokens import TokenType
    from sqlglot.dialects.dialect import Dialect

    dialect_cls = Dialect.get_or_raise(dialect_name)
    tokenizer_cls = getattr(dialect_cls, "tokenizer_class", None)
    if tokenizer_cls is None:
        return False
    tt = tokenizer_cls.KEYWORDS.get(name.upper())
    if tt is None:
        return False
    return tt is not TokenType.VAR


def apply_dialect_quoting(
    sql_query: str,
    linked_entities: Dict[str, Any],
    dsn: str | None = None,
) -> str:
    """Применяет диалект-специфичные кавычки к SQL запросу через AST sqlglot.

    Стратегия выборочная: квотируем только идентификаторы, для которых это
    действительно необходимо (известные таблицы/колонки из linked_entities,
    reserved keywords, mixed-case в postgres, не-ASCII/спецсимволы).
    Алиасы (column/table) остаются нетронутыми.

    Fail-fast: multi-statement и нестандартные ошибки парсера пробрасываются
    как RuntimeError — silent corruption SQL недопустим. Если AST-парсер падает
    с не-RuntimeError ошибкой, по умолчанию бросаем :class:`SQLPostprocessError`.
    Manual-fallback включается явной opt-in переменной
    ``SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK=1`` и тоже fail-fast.
    """
    sqlglot_enabled = is_sqlglot_enabled()
    if not sqlglot_enabled:
        return apply_manual_quoting(sql_query, linked_entities, dsn=dsn)

    try:
        return quote_via_ast(
            sql_query,
            linked_entities,
            get_sqlglot_dialect(dsn, strict=bool(dsn and str(dsn).strip())),
        )
    except RuntimeError:
        raise
    except Exception as e:
        safe_error = _redact_postprocess_error(e)
        if os.getenv("SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK", "0") != "1":
            raise SQLPostprocessError(
                f"SQLGlot dialect quoting failed: {safe_error}"
            ) from e
        logger.warning(
            "SQLGlot AST quoting failed (%s); manual-fallback enabled via "
            "SQL_GENERATION_ALLOW_MANUAL_QUOTING_FALLBACK=1",
            safe_error,
        )

    return apply_manual_quoting(sql_query, linked_entities, dsn=dsn)


def quote_via_ast(sql_query: str, linked_entities: Dict[str, Any], dialect: str) -> str:
    """AST-based селективное квотирование. Используется и в основном, и в fallback пути."""
    import sqlglot
    from sqlglot import exp

    # Собираем known идентификаторы
    tables: Set[str] = set()
    columns: Set[str] = set()
    if isinstance(linked_entities, dict):
        for entity_kind in ("metrics", "dimensions"):
            for entity in linked_entities.get(entity_kind, []) or []:
                if isinstance(entity, dict):
                    if entity.get("table"):
                        tables.add(str(entity["table"]))
                    if entity.get("column"):
                        columns.add(str(entity["column"]))

    read_dialect = None if dialect == "ansi" else dialect
    statements = sqlglot.parse(sql_query.strip(), read=read_dialect)
    if not statements:
        # W1-review (#12): default fail-fast вместо silent return.
        # Прежнее «default soft» нарушало AGENTS.md и было непоследовательно с
        # прочими ветками функции — все они fail-fast. SQL_POSTPROCESS_STRICT
        # env-флаг удалён: единственное поведение — RuntimeError при empty AST.
        logger.warning(
            "sqlglot.parse returned empty AST for SQL (fail-fast, no silent return): %s",
            _redact_postprocess_error(sql_query[:200]),
        )
        raise RuntimeError("Failed to parse SQL: empty AST")

    non_null = [s for s in statements if s is not None]
    if len(non_null) > 1:
        raise RuntimeError(
            "SQL generator produced multiple statements, expected single SELECT"
        )
    if not non_null:
        logger.warning(
            "sqlglot.parse returned only null statements for SQL (fail-fast): %s",
            _redact_postprocess_error(sql_query[:200]),
        )
        raise RuntimeError("Failed to parse SQL: empty AST")

    stmt = non_null[0]

    # Собираем алиасы таблиц
    table_aliases: Set[str] = set()
    for table_node in stmt.find_all(exp.Table):
        alias = table_node.alias
        if alias:
            table_aliases.add(alias)

    # Обходим все Identifier и проставляем quoted по предикату.
    for ident in stmt.find_all(exp.Identifier):
        if ident.args.get("quoted"):
            # Уже квотирован в исходном SQL — сохраняем.
            continue
        parent = ident.parent
        arg_key = ident.arg_key
        # Алиасы (column alias и table alias) — оставляем голыми.
        if isinstance(parent, (exp.Alias, exp.TableAlias)):
            continue
        # Column reference на алиас таблицы: arg_key='table' и имя в table_aliases.
        if (
            isinstance(parent, exp.Column)
            and arg_key == "table"
            and ident.name in table_aliases
        ):
            continue
        if should_quote_name(
            ident.name, dialect, tables, columns, table_aliases
        ):
            ident.set("quoted", True)

    sqlglot_dialect = None if dialect == "ansi" else dialect
    result = stmt.sql(dialect=sqlglot_dialect)
    logger.debug(
        "Applied AST dialect quoting (%s). Original: %s... -> Result: %s...",
        dialect,
        _redact_postprocess_error(sql_query[:100]),
        _redact_postprocess_error(result[:100]),
    )
    return result


def apply_manual_quoting(
    sql_query: str,
    linked_entities: Dict[str, Any],
    dsn: str | None = None,
) -> str:
    """Манульное квотирование через AST sqlglot — используется в legacy/fallback пути.

    Сливается с apply_dialect_quoting через общий хелпер quote_via_ast. Любая
    ошибка парсера (не-RuntimeError) трактуется как fail-fast: исходный SQL
    нельзя возвращать молча, потому что вызывающий код считает результат уже
    "квотированным". AGENTS.md: silent corruption SQL недопустим.
    """
    try:
        return quote_via_ast(
            sql_query,
            linked_entities,
            get_sqlglot_dialect(dsn, strict=bool(dsn and str(dsn).strip())),
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise SQLPostprocessError(
            f"Manual quoting via AST failed: {_redact_postprocess_error(exc)}"
        ) from exc
