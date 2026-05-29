"""
Диалект-специфичная логика для разных СУБД
"""
import logging
import os

logger = logging.getLogger(__name__)


def _resolve_dsn(dsn: str | None) -> str:
    """Возвращает только явно переданный DSN; env fallback запрещён."""
    if dsn is not None:
        return dsn
    return ""


def _has_explicit_dsn(dsn: str | None) -> bool:
    return isinstance(dsn, str) and bool(dsn.strip())


def _strict_kwargs(dsn: str | None) -> dict:
    return {"strict": True} if _has_explicit_dsn(dsn) else {}


def _plugin_for(dsn: str | None, *, strict: bool = False):
    """Lazy-получение плагина для DSN; ``None`` если dsn пуст."""
    resolved = _resolve_dsn(dsn)
    if not resolved:
        return None
    from db_plugins import get_plugin  # lazy: избегаем circular import на module level
    plugin = get_plugin(resolved)
    if plugin is None and strict:
        raise RuntimeError(f"No DB plugin found for explicit DSN scheme: {resolved.split(':', 1)[0]}")
    return plugin


def _build_sqlglot_dialect_mapping() -> dict:
    """Собирает mapping внутренних диалектов → sqlglot-диалектов через plugin-discovery.

    Каждый плагин должен сообщить свой sqlglot dialect — либо через публичный
    атрибут ``sqlglot_dialect`` (приоритет, source of truth), либо через
    protected helper ``_sqlglot_dialect_name()``.
    """
    mapping: dict = {"sql": "ansi"}
    try:
        from db_plugins.manager import _PLUGINS  # lazy
    except Exception:
        return mapping

    for _scheme, plugin in _PLUGINS.items():
        dialect = getattr(plugin, "dialect", None)
        if not isinstance(dialect, str) or not dialect:
            continue
        key = dialect.lower()
        # 1) Public атрибут плагина — source of truth.
        public_attr = getattr(plugin, "sqlglot_dialect", None)
        if isinstance(public_attr, str) and public_attr:
            mapping.setdefault(key, public_attr)
            continue
        # 2) Protected helper — fallback.
        getter = getattr(plugin, "_sqlglot_dialect_name", None)
        if callable(getter):
            try:
                sqlglot_name = getter()
            except Exception:
                sqlglot_name = None
            if isinstance(sqlglot_name, str) and sqlglot_name:
                mapping.setdefault(key, sqlglot_name)
    return mapping


# Маппинг внутренних названий диалектов на sqlglot диалекты (plugin-driven).
SQLGLOT_DIALECT_MAPPING: dict = _build_sqlglot_dialect_mapping()


def refresh_sqlglot_dialect_mapping() -> dict:
    """Пересобирает SQLGLOT_DIALECT_MAPPING через plugin-discovery.

    Полезно в тестах после регистрации/подмены плагинов.
    """
    global SQLGLOT_DIALECT_MAPPING
    SQLGLOT_DIALECT_MAPPING = _build_sqlglot_dialect_mapping()
    return SQLGLOT_DIALECT_MAPPING


def get_current_dialect_label(dsn: str | None = None, *, strict: bool = False) -> str:
    """Получает человекочитаемое название диалекта текущей БД.

    Args:
        dsn: Явный DSN. Если не указан, возвращается нейтральный SQL label.
    """
    try:
        plugin = _plugin_for(dsn, strict=strict)
        if plugin is None:
            return "SQL"
        label = getattr(plugin, "dialect_label", None)
        if isinstance(label, str) and label.strip():
            return label
        dialect = getattr(plugin, "dialect", "")
        return (str(dialect).upper() if isinstance(dialect, str) and dialect else "SQL")
    except Exception:
        if strict and _has_explicit_dsn(dsn):
            raise
        return "SQL"


def get_current_dialect_name(dsn: str | None = None, *, strict: bool = False) -> str:
    """Получает внутреннее название диалекта текущей БД.

    Args:
        dsn: Явный DSN. Если не указан, возвращается нейтральный SQL dialect.
    """
    try:
        plugin = _plugin_for(dsn, strict=strict)
        if plugin is None:
            return "sql"
        dialect = getattr(plugin, "dialect", "sql")
        if isinstance(dialect, str) and dialect.strip():
            return dialect.lower()
        if strict and _has_explicit_dsn(dsn):
            raise RuntimeError("DB plugin did not declare a dialect for explicit DSN")
        return "sql"
    except Exception:
        if strict and _has_explicit_dsn(dsn):
            raise
        return "sql"


def _split_identifier_parts(identifier: str, sqlglot_dialect: str) -> list[str]:
    """Разбивает qualified identifier на части через sqlglot.

    sqlglot корректно учитывает квотированные части (например, ``"a.b"`` как
    одна часть с точкой внутри), чего наивный split не делает. При ошибках
    парсинга используется fallback на простой ``split('.')`` ради обратной
    совместимости с не-SQL входом.
    """
    text = identifier or ""
    if not text:
        return []
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import ParseError, TokenError

        read_dialect = "" if sqlglot_dialect in {"ansi", "sql", ""} else sqlglot_dialect
        node = sqlglot.parse_one(text, read=read_dialect, into=exp.Column)
        # node.parts даёт идентификаторы в логическом порядке
        # [catalog?, db?, table?, column], тогда как find_all(Identifier)
        # обходит дерево pre-order и переворачивает порядок.
        parts = [getattr(p, "name", "") for p in node.parts]
        parts = [p for p in parts if p != ""]
        if parts:
            return parts
    except (ParseError, TokenError, ImportError) as exc:
        logger.warning(
            "sqlglot не смог распарсить identifier %r (dialect=%r): %s; используем split('.')",
            text, sqlglot_dialect, exc,
        )
    return [p for p in text.split(".") if p != ""]


def quote_identifier(identifier: str, dsn: str | None = None) -> str:
    """Квотирует идентификатор с учетом диалекта и экранированием внутренних кавычек.

    Разбивает qualified-имена через sqlglot, поэтому подходит для
    ``schema.table.column``. Для алиасов и одиночных имён, в которых точка
    должна остаться частью имени, используйте :func:`quote_single_identifier`.

    Args:
        identifier: Имя таблицы/колонки, возможно квалифицированное.
        dsn: Явный DSN. Если не указан, используется нейтральный SQL dialect.
    """
    strict_kwargs = _strict_kwargs(dsn)
    dialect = get_current_dialect_name(dsn, **strict_kwargs)
    sqlglot_dialect = get_sqlglot_dialect(dsn, **strict_kwargs)
    parts = _split_identifier_parts(identifier, sqlglot_dialect)
    if dialect in {"mysql", "impala"}:
        # Экранируем бэктики внутри идентификатора
        return ".".join(f"`{p.replace('`', '``')}`" for p in parts)
    # Экранируем двойные кавычки внутри идентификатора
    return ".".join('"' + p.replace('"', '""') + '"' for p in parts)


def quote_single_identifier(identifier: str, dsn: str | None = None) -> str:
    """Квотирует одиночный идентификатор без разбиения по точке.

    Используется для column/table алиасов: алиас вида ``foo.bar`` должен
    оставаться единым именем (с точкой внутри), а не превращаться в
    ``"foo"."bar"`` — это синтаксическая ошибка в позиции AS.
    """
    dialect = get_current_dialect_name(dsn, **_strict_kwargs(dsn))
    name = identifier or ""
    if not name:
        return ""
    if dialect in {"mysql", "impala"}:
        return f"`{name.replace('`', '``')}`"
    return '"' + name.replace('"', '""') + '"'


def escape_sql_string(value) -> str:
    """Простейшее экранирование одинарных кавычек по стандарту SQL: ' → ''"""
    s = str(value)
    return s.replace("'", "''")


def sql_string_literal(value, dsn: str | None = None) -> str:
    """Безопасный строковый литерал, диалект-aware.

    Возвращает строку вида ``'...'`` с обрамляющими кавычками. Делегирует в
    sqlglot; при недоступности sqlglot падает fail-fast. ``NUL`` (``\\x00``)
    запрещён.
    """
    s = "" if value is None else str(value)
    if "\x00" in s:
        raise ValueError("NUL byte not allowed in SQL string literal")
    try:
        from sqlglot import exp
    except ImportError as exc:
        raise RuntimeError("sqlglot is required for sql_string_literal") from exc

    sqlglot_dialect = get_sqlglot_dialect(dsn, **_strict_kwargs(dsn))
    if sqlglot_dialect in {"", "ansi", "sql"}:
        return exp.Literal.string(s).sql()
    return exp.Literal.string(s).sql(dialect=sqlglot_dialect)


def get_sqlglot_dialect(dsn: str | None = None, *, strict: bool = False) -> str:
    """Получает диалект sqlglot для текущей БД."""
    if strict:
        current_dialect = get_current_dialect_name(dsn, strict=True)
    else:
        current_dialect = get_current_dialect_name(dsn)
    mapped = SQLGLOT_DIALECT_MAPPING.get(current_dialect)
    if isinstance(mapped, str) and mapped:
        return mapped
    if strict and _has_explicit_dsn(dsn):
        raise RuntimeError(
            f"sqlglot dialect is not configured for DB dialect {current_dialect!r}"
        )
    return "ansi"


def is_sqlglot_enabled() -> bool:
    """Проверяет, включен ли режим sqlglot."""
    return os.getenv("USE_SQLGLOT", "1") == "1"


def get_identifier_quote_char(dialect: str | None = None) -> str:
    """Возвращает символ-обрамитель идентификатора для диалекта.

    Для MySQL/Impala — обратная кавычка (`), для остальных стандартных
    диалектов SQL (Postgres, SQLite, DuckDB, ANSI и пр.) — двойная кавычка.
    Если ``dialect`` не передан, берётся текущий диалект через
    :func:`get_current_dialect_name`.
    """
    d = (dialect or get_current_dialect_name() or "").lower()
    if d in {"mysql", "impala"}:
        return "`"
    return '"'


def double_quote_is_string(dialect: str | None = None) -> bool:
    """В MySQL двойные кавычки могут обозначать строковый литерал.

    Во всех остальных стандартных диалектах ``"..."`` — это
    quoted identifier. Возвращаем True только для MySQL.
    """
    d = (dialect or get_current_dialect_name() or "").lower()
    return d == "mysql"
