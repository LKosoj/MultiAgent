"""DB execution подмодуль core (Phase 7 декомпозиция).

Реализация: secure_db_executor + 8 хелперов парсинга/нормализации DESCRIBE.

Singletons передаются через keyword-only аргументы из фасада.
Внешние зависимости (get_plugin) разрешаются через фасадный модуль
(`custom_tools.text_to_sql.core`), чтобы тесты могли подменять их monkeypatch'ем.
"""
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MissingDSNError(RuntimeError):
    """DSN не передан явно и env-fallback DB_DSN не разрешён.

    AGENTS.md запрещает silent fallback на ENV для secure-вызовов БД: вызывающий
    код обязан передавать DSN из контекста pipeline. Старый env-fallback
    остаётся доступен только через явный opt-in
    ``SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1`` (с warning).

    W1-review: наследование сменено ValueError → RuntimeError для consistency
    с прочими fail-fast-исключениями группы B (SQLPostprocessError,
    MorphemesIndexUnavailable, SchemaIncludeFilterError).
    """


# Кортеж примитивных JSON-типов вынесен на уровень модуля, чтобы не создавать
# новый объект при каждом вызове _normalize_jsonable с list/dict-значением.
_JSONABLE_PRIMITIVES = (bool, int, float, str, type(None))


def _normalize_jsonable(value: Any) -> Any:
    """Рекурсивно приводит value к JSON-сериализуемому виду без round-trip через json.

    Поведение эквивалентно `json.loads(json.dumps(value, ensure_ascii=False, default=str))`:
    - dict → dict, ключи приводятся к str (как json.dumps);
    - list/tuple → list (с рекурсией по элементам);
    - bool/int/float/None/str — без изменений;
    - всё прочее (Decimal/datetime/UUID/bytes/...) — `str(value)` через default=str.
    Этот же путь сохраняет историческое представление datetime/bytes (str(),
    а не isoformat()/decode()), чтобы не менять внешний контракт executor.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        # Быстрый путь: если все элементы — примитивы, не рекурсируем
        if all(isinstance(v, _JSONABLE_PRIMITIVES) for v in value):
            return list(value)
        return [_normalize_jsonable(v) for v in value]
    if isinstance(value, dict):
        # Быстрый путь: если все ключи str и все значения — примитивы
        if all(isinstance(k, str) and isinstance(v, _JSONABLE_PRIMITIVES) for k, v in value.items()):
            return dict(value)
        return {str(k): _normalize_jsonable(v) for k, v in value.items()}
    return str(value)


def _normalize_executor_result(
    result: Dict[str, Any],
    *,
    start_time: float,
    sql_query: str,
    row_limit: Optional[int],
    safety_issues: Optional[List[Dict[str, Any]]] = None,
    dry_run_only: bool = False,
    skipped_execution: bool = False,
) -> Dict[str, Any]:
    normalized = dict(result or {})
    normalized.setdefault("success", False)
    normalized["data"] = _normalize_jsonable(normalized.get("data", []))
    normalized["columns"] = _normalize_jsonable(normalized.get("columns", []))
    normalized.setdefault("rows_affected", 0)
    normalized.setdefault("execution_time_ms", int((time.time() - start_time) * 1000))
    normalized.setdefault("error_message", None)
    normalized["safety_issues"] = safety_issues or normalized.get("safety_issues") or []
    normalized["dry_run_only"] = bool(dry_run_only or normalized.get("dry_run_only", False))
    normalized["skipped_execution"] = bool(skipped_execution or normalized.get("skipped_execution", False))
    normalized["sql_query"] = sql_query
    normalized["applied_row_limit"] = row_limit
    return normalized


def _describe_identifier_parts_from_text(sql_query: str) -> list[str]:
    match = re.match(r"^\s*(?:DESCRIBE|DESC)\b(?P<rest>.*)$", sql_query, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise ValueError("Invalid DESCRIBE syntax")

    target = match.group("rest").strip()
    if not target:
        raise ValueError("Invalid DESCRIBE syntax")

    parts: list[str] = []
    current: list[str] = []
    quote_end: str | None = None
    index = 0
    while index < len(target):
        char = target[index]
        if quote_end:
            if char == quote_end:
                if index + 1 < len(target) and target[index + 1] == quote_end:
                    current.append(char)
                    index += 2
                    continue
                quote_end = None
            else:
                current.append(char)
            index += 1
            continue
        if char in {'"', '`'}:
            quote_end = char
            index += 1
            continue
        if char == '[':
            quote_end = ']'
            index += 1
            continue
        if char == '.':
            part = "".join(current).strip()
            if not part:
                raise ValueError("Invalid DESCRIBE identifier")
            parts.append(part)
            current = []
            index += 1
            continue
        if char.isspace():
            if current:
                remainder = target[index:].strip()
                if remainder == "":
                    break
                if remainder.startswith(";") and remainder[1:].strip() == "":
                    break
                raise ValueError("Invalid DESCRIBE syntax")
            index += 1
            continue
        if char == ";":
            if not current:
                raise ValueError("Invalid DESCRIBE identifier")
            if target[index + 1:].strip():
                raise ValueError("Invalid DESCRIBE syntax")
            break
        current.append(char)
        index += 1

    if quote_end:
        raise ValueError("Invalid DESCRIBE identifier")

    part = "".join(current).strip()
    if not part:
        raise ValueError("Invalid DESCRIBE identifier")
    parts.append(part)
    return parts


def _parse_table_parts_from_describe(sql_query: str) -> list[str]:
    """Извлекает части identifier из DESCRIBE/DESC, не разделяя точки внутри кавычек.

    Единый парсер DESCRIBE-target (EPIC 7.26): текстовый разбор всегда
    используется как source of truth, sqlglot привлекается только как
    валидатор синтаксиса при включённом USE_SQLGLOT (sqlglot.tokenize), что
    позволяет ловить мусорные DESCRIBE-формы ещё до execute_select.
    """
    from ..dialects import is_sqlglot_enabled, get_sqlglot_dialect

    if not is_sqlglot_enabled():
        return _describe_identifier_parts_from_text(sql_query)

    try:
        import sqlglot

        dialect = get_sqlglot_dialect()
        sqlglot.tokenize(sql_query, dialect=None if dialect == "ansi" else dialect)
    except Exception as e:
        raise ValueError(f"sqlglot failed to parse DESCRIBE target: {e}") from e

    return _describe_identifier_parts_from_text(sql_query)


# === EPIC 7.26: deprecated алиасы, сохраняем для совместимости. ===
def _parse_table_parts_from_describe_sqlglot(sql_query: str) -> list[str]:
    """Deprecated alias для :func:`_parse_table_parts_from_describe` (EPIC 7.26)."""
    return _parse_table_parts_from_describe(sql_query)


def _parse_table_name_from_describe_sqlglot(sql_query: str) -> str:
    """Deprecated alias: возвращает имя таблицы через текстовый парсер DESCRIBE."""
    return ".".join(_parse_table_parts_from_describe(sql_query))


def _parse_table_name_simple(sql_query: str) -> str:
    """Deprecated alias: возвращает имя таблицы через текстовый парсер DESCRIBE."""
    return ".".join(_describe_identifier_parts_from_text(sql_query))


def _extract_schema_and_table_from_describe(sql_query: str) -> tuple[str | None, str]:
    """Извлекает схему и таблицу из DESCRIBE команды."""
    parts = _parse_table_parts_from_describe(sql_query)
    if len(parts) == 2:
        return parts[0], parts[1]  # schema.table
    if len(parts) == 3:
        return parts[1], parts[2]  # database.schema.table -> schema, table
    return None, ".".join(parts)


def _resolve_describe_table(
    schema_info: Dict[str, Any],
    schema_name: Optional[str],
    table_name: str,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Resolve DESCRIBE target against exact, qualified, then unique short names."""
    if not isinstance(schema_info, dict):
        return None

    qualified_name = f"{schema_name}.{table_name}" if schema_name else None
    if qualified_name and qualified_name in schema_info and isinstance(schema_info[qualified_name], dict):
        return qualified_name, schema_info[qualified_name]

    candidates: list[tuple[str, Dict[str, Any]]] = []
    for key in [table_name]:
        if key and key in schema_info and isinstance(schema_info[key], dict):
            candidates.append((key, schema_info[key]))

    if not schema_name:
        short_matches = [
            (key, value)
            for key, value in schema_info.items()
            if isinstance(value, dict) and str(key).split(".")[-1] == table_name
        ]
        candidates.extend(short_matches)

    unique: dict[str, Dict[str, Any]] = {}
    for key, value in candidates:
        unique[key] = value
    if len(unique) == 1:
        key, value = next(iter(unique.items()))
        return key, value
    if len(unique) > 1:
        matches = ", ".join(sorted(unique))
        raise ValueError(f"Ambiguous table name '{table_name}': {matches}")
    return None


def _format_describe_result(table_columns: Dict[str, Dict[str, str]], table_name: str) -> Dict[str, object]:
    """Преобразует результат introspect_schema в формат DESCRIBE."""
    import time
    start_time = time.time()
    # Явная проверка формы: вложенная структура {description, columns: {...}}
    # vs flat {col1: {...}, col2: {...}} — иначе колонка с именем "columns" сломает выбор.
    if (
        isinstance(table_columns, dict)
        and "columns" in table_columns
        and isinstance(table_columns["columns"], dict)
    ):
        columns = table_columns["columns"]
    else:
        columns = table_columns or {}

    # Конвертируем в формат, похожий на стандартный DESCRIBE
    rows = []
    for col_name, col_info in columns.items():
        if not isinstance(col_info, dict):
            continue
        not_null_value = col_info.get("not_null")
        if isinstance(not_null_value, bool):
            is_not_null = bool(not_null_value)
        else:
            is_not_null = str(not_null_value).strip().lower() in {"true", "yes", "1", "y"}
        rows.append([
            col_name,
            col_info.get("type", ""),
            "NO" if is_not_null else "YES",
            col_info.get("constraint_type", ""),
            col_info.get("default_value", ""),
            col_info.get("description", "")
        ])

    elapsed = int((time.time() - start_time) * 1000)

    return {
        "success": True,
        "data": rows,
        "columns": ["Field", "Type", "Null", "Key", "Default", "Comment"],
        "rows_affected": len(rows),
        "execution_time_ms": elapsed,
        "error_message": None
    }


# === EPIC 7.25: общий конструктор failure-результата ===
def _build_failure_result(
    start_time: float,
    error_message: str,
    *,
    safety_issues: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Строит минимальный failure-dict для executor (EPIC 7.25).

    Возвращает dict с гарантированными ключами success/data/columns/
    rows_affected/execution_time_ms/error_message. ``_normalize_executor_result``
    добавит остальные поля (sql_query, applied_row_limit, ...) уже на выходе
    из executor.
    """
    result: Dict[str, Any] = {
        "success": False,
        "data": [],
        "columns": [],
        "rows_affected": 0,
        "execution_time_ms": int((time.time() - start_time) * 1000),
        "error_message": error_message,
    }
    if safety_issues is not None:
        result["safety_issues"] = safety_issues
    return result


# === EPIC 7.5: вычленение тела EXPLAIN через regex (поддерживает скобки) ===
_EXPLAIN_BODY_RE = re.compile(
    r"^\s*EXPLAIN\b\s*(?:\([^)]*\))?\s*(?P<body>.+)$",
    flags=re.IGNORECASE | re.DOTALL,
)


def _extract_explain_body(sql_query: str) -> str:
    """Возвращает SQL без префикса EXPLAIN/EXPLAIN(...). Бросает ValueError, если не EXPLAIN."""
    match = _EXPLAIN_BODY_RE.match(sql_query or "")
    if not match:
        raise ValueError("EXPLAIN body extraction failed: not an EXPLAIN statement")
    body = match.group("body").strip()
    if not body:
        raise ValueError("EXPLAIN body is empty")
    return body


# === EPIC 7.2 / 7.6: routing через sqlglot AST ===
_StatementKind = str  # "select" | "describe" | "explain" | "show" | "unknown"

# Маппинг первого ключевого слова → kind для regex-классификатора (USE_SQLGLOT=0).
_REGEX_KEYWORD_MAP: Dict[str, _StatementKind] = {
    "SELECT": "select",
    "WITH": "select",
    "DESCRIBE": "describe",
    "DESC": "describe",
    "EXPLAIN": "explain",
    "SHOW": "show",
}


def _classify_statement_regex(sql_query: str) -> _StatementKind:
    """Простой regex-классификатор для legacy-режима (USE_SQLGLOT=0).

    Удаляет leading комментарии (-- и /* */), извлекает первое ключевое слово
    и маппирует его в kind. Используется только при отключённом sqlglot.

    Ограничение: ожидает, что SQL начинается с ключевого слова после комментариев;
    leading non-alpha символы (например ';') не удаляются и приводят к 'unknown'.
    Для production-использования включите sqlglot (USE_SQLGLOT=1).
    """
    # Убираем leading однострочные и блочные комментарии
    stripped = re.sub(r"(--[^\n\r]*|/\*.*?\*/)", " ", sql_query, flags=re.DOTALL).strip()
    # Ищем первое ключевое слово (буквы и нижнее подчёркивание)
    match = re.match(r"([A-Za-z_]+)", stripped)
    if not match:
        return "unknown"
    first_kw = match.group(1).upper()
    return _REGEX_KEYWORD_MAP.get(first_kw, "unknown")


def _classify_statement(sql_query: str, dsn: Optional[str] = None) -> _StatementKind:
    """Определяет тип SQL-стейтмента через AST sqlglot.

    Без startswith-эвристик: парсим sqlglot.parse_one с защитой по timeout/length,
    различаем exp.Describe / exp.Command(name=...) / exp.Select / exp.With / set-операции.

    Поведение при деградации НАМЕРЕННО асимметрично:
      * USE_SQLGLOT=0 — regex-классификатор (legacy-маршрутизация
        SELECT/DESCRIBE/EXPLAIN/SHOW), т.к. AST-путь полностью выключен;
      * USE_SQLGLOT=1, но sqlglot import/parse падает — возвращаем "unknown"
        (fail-closed: executor отклонит стейтмент как unsupported). Здесь НЕ
        откатываемся на regex, чтобы не возвращать те самые startswith-эвристики,
        которые AST-режим как раз и заменяет.
    """
    from ..dialects import is_sqlglot_enabled, get_sqlglot_dialect
    from ..utils import parse_with_timeout

    if not is_sqlglot_enabled():
        # При отключённом sqlglot используем regex-классификатор.
        # Это позволяет legacy-режиму корректно маршрутизировать SELECT/DESCRIBE/EXPLAIN/SHOW.
        # ВАЖНО (безопасность): sql_safety_check выполняется ДО routing — этот
        # fallback НЕ обходит проверку безопасности, он лишь классифицирует уже
        # допущенный стейтмент. В этом режиме (USE_SQLGLOT=0) сам sql_safety_check
        # тоже работает по regex-пути, согласованно с regex-классификацией здесь.
        logger.warning(
            "sqlglot disabled: statement routing uses regex fallback "
            "(USE_SQLGLOT=0). For production use enable sqlglot."
        )
        return _classify_statement_regex(sql_query)

    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot import errors as sqlglot_errors
    except Exception as e:
        logger.error("sqlglot import failed inside _classify_statement: %s", e)
        return "unknown"

    dialect = get_sqlglot_dialect(dsn, strict=bool(dsn and str(dsn).strip()))
    read = None if dialect == "ansi" else dialect

    try:
        statements = parse_with_timeout(sql_query, read=read)
    except (TimeoutError, sqlglot_errors.ParseError, sqlglot_errors.TokenError) as e:
        logger.warning("sqlglot parse failed in _classify_statement: %s", e)
        return "unknown"

    statements = [s for s in (statements or []) if s is not None]
    if len(statements) != 1:
        return "unknown"

    stmt = statements[0]

    if isinstance(stmt, getattr(exp, "Describe", ())):
        return "describe"

    if isinstance(stmt, exp.Command):
        command_name = (getattr(stmt, "name", "") or "").upper()
        if command_name in {"DESCRIBE", "DESC"}:
            return "describe"
        if command_name == "EXPLAIN":
            return "explain"
        if command_name == "SHOW":
            return "show"
        return "unknown"

    if isinstance(stmt, exp.Select):
        return "select"

    set_op_classes = tuple(
        cls for cls in (
            getattr(exp, name, None) for name in ("Union", "Intersect", "Except")
        ) if cls is not None
    )
    if set_op_classes and isinstance(stmt, set_op_classes):
        return "select"

    if isinstance(stmt, exp.With):
        inner = getattr(stmt, "this", None)
        if isinstance(inner, exp.Select):
            return "select"
        if set_op_classes and isinstance(inner, set_op_classes):
            return "select"
        return "unknown"

    return "unknown"


# === EPIC 7.1: стратегии выполнения ===

def _describe_strategy(
    *, sql_query: str, plugin, conn, start: float, row_limit: int
) -> Dict[str, Any]:
    if not hasattr(plugin, "introspect_schema"):
        return _build_failure_result(start, "DESCRIBE requires plugin introspect_schema support")
    try:
        schema_name, table_name = _extract_schema_and_table_from_describe(sql_query)
        schema_info = plugin.introspect_schema(conn, schema=schema_name, table_name=table_name)
        resolved = _resolve_describe_table(schema_info, schema_name, table_name)
    except Exception as e:
        logger.warning("Failed to parse DESCRIBE with introspect_schema: %s", e)
        return _build_failure_result(start, f"DESCRIBE introspection failed: {e}")

    if not resolved:
        return _build_failure_result(start, f"Table '{table_name}' not found")
    resolved_name, table_info = resolved
    return _format_describe_result(table_info, resolved_name)


def _explain_strategy(
    *, sql_query: str, plugin, conn, start: float, row_limit: int
) -> Dict[str, Any]:
    if not hasattr(plugin, "explain"):
        return _build_failure_result(start, "EXPLAIN requires plugin explain support")
    explain_body = _extract_explain_body(sql_query)
    explain_result = plugin.explain(conn, explain_body)
    # Проверяем наличие issues (например, EXPLAIN_UNSUPPORTED) и plan=None.
    issues = explain_result.get("issues") or []
    plan = explain_result.get("plan")
    if plan is None or any(i.get("issue_type") == "EXPLAIN_UNSUPPORTED" for i in issues):
        first_issue = issues[0] if issues else {}
        error_msg = first_issue.get("description") or "EXPLAIN not supported by this plugin"
        return _build_failure_result(start, error_msg)
    elapsed = int((time.time() - start) * 1000)
    return {
        "success": True,
        "data": [[plan]],
        "columns": ["Plan"],
        "rows_affected": 1,
        "execution_time_ms": elapsed,
        "error_message": None,
        "explain_result": explain_result,
    }


def _select_strategy(
    *, sql_query: str, plugin, conn, start: float, row_limit: int
) -> Dict[str, Any]:
    return plugin.execute_select(conn, sql_query, row_limit=row_limit)


def _show_strategy(
    *, sql_query: str, plugin, conn, start: float, row_limit: int
) -> Dict[str, Any]:
    if not hasattr(plugin, "show"):
        return _build_failure_result(start, "SHOW requires plugin show support")
    return plugin.show(conn, sql_query)


_STRATEGIES: Dict[str, Any] = {
    "describe": _describe_strategy,
    "explain": _explain_strategy,
    "select": _select_strategy,
    "show": _show_strategy,
}


def _parse_row_limit(
    row_limit: Optional[int], start: float, sql_query: str
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Парсит row_limit и возвращает (value, failure_normalized | None)."""
    if row_limit is None:
        raw_row_limit = os.getenv("DB_EXECUTOR_ROW_LIMIT", "500")
        try:
            parsed = int(raw_row_limit)
        except (TypeError, ValueError):
            return None, _normalize_executor_result(
                _build_failure_result(start, f"Invalid DB_EXECUTOR_ROW_LIMIT: {raw_row_limit!r}"),
                start_time=start, sql_query=sql_query, row_limit=None,
            )
    else:
        raw_row_limit = row_limit
        try:
            parsed = int(row_limit)
        except (TypeError, ValueError):
            return None, _normalize_executor_result(
                _build_failure_result(start, f"Invalid row_limit: {raw_row_limit!r}"),
                start_time=start, sql_query=sql_query, row_limit=None,
            )

    if parsed <= 0:
        return None, _normalize_executor_result(
            _build_failure_result(start, "row_limit must be a positive integer"),
            start_time=start, sql_query=sql_query, row_limit=None,
        )
    return parsed, None


def secure_db_executor(
    sql_query: str,
    row_limit: Optional[int] = None,
    dsn: Optional[str] = None,
    *,
    sql_validator,
    schema_limiter,
) -> Dict[str, object]:
    """Безопасное выполнение SELECT и разрешённых команд (DESCRIBE/EXPLAIN/SHOW).

    Поток выполнения (EPIC 7.1):
    1. row_limit парсится/валидируется через _parse_row_limit.
    2. DSN resolution для real execution; dry-run может работать без DSN.
    3. sql_safety_check (с LLM-аудитом, см. 7.4).
    4. dry-run guard.
    5. Открытие соединения; finally закрывает только если оно реально открылось.
    6. _classify_statement(sql) → стратегия выполнения.
    7. _normalize_executor_result оборачивает результат для контракта.

    DSN resolution (W1-T1):
    - Если ``dsn`` передан явным аргументом — используется он.
    - Если ``dsn=None`` и ``SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1`` — можно явно
      opt-in'ом использовать ``DB_DSN``.
    - Если оба источника пусты и это не dry-run — ``MissingDSNError`` без silent fallback.
    """
    logger.info("Executing SQL query securely")
    start = time.time()

    row_limit, failure = _parse_row_limit(row_limit, start, sql_query)
    if failure is not None:
        return failure
    assert row_limit is not None  # для типчекеров

    from ..utils import get_runtime_context_dsn, is_dry_run_only, mask_dsn

    dry_run_only = is_dry_run_only()

    # Явный параметр имеет приоритет. ENV-fallback на DB_DSN считается silent
    # деградацией безопасности (AGENTS.md): он работает только при явном
    # opt-in SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1, иначе MissingDSNError.
    effective_dsn = dsn if (isinstance(dsn, str) and dsn.strip()) else get_runtime_context_dsn()
    if effective_dsn is None:
        allow_env = os.getenv("SECURE_DB_EXECUTOR_ALLOW_ENV_DSN", "0") == "1"
        env_dsn = os.getenv("DB_DSN")
        if allow_env and env_dsn and env_dsn.strip():
            logger.warning(
                "secure_db_executor: dsn parameter MISSING; using DB_DSN env "
                "(SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1 opt-in)"
            )
            effective_dsn = env_dsn
    if not effective_dsn and not dry_run_only:
        raise MissingDSNError(
            "DSN required: pass dsn via parameter from pipeline context. "
            "Silent DB_DSN env fallback disabled — set "
            "SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1 to opt-in (not recommended)."
        )
    dsn = effective_dsn

    # Через фасад: тесты monkeypatch'ят core.sql_safety_check / core.get_plugin.
    from custom_tools.text_to_sql import core as _facade

    safety = _facade.sql_safety_check(sql_query, dsn=dsn or "")
    if not isinstance(safety, dict):
        return _normalize_executor_result(
            _build_failure_result(
                start,
                f"Invalid safety check result structure: expected dict, got {type(safety).__name__}",
            ),
            start_time=start, sql_query=sql_query, row_limit=row_limit,
        )
    if not safety.get("is_safe", False):
        issues = safety.get("issues") or []
        return _normalize_executor_result(
            _build_failure_result(start, "Unsafe query.", safety_issues=issues),
            start_time=start, sql_query=sql_query, row_limit=row_limit,
            safety_issues=issues,
        )

    if dry_run_only:
        return _normalize_executor_result({
            "success": True,
            "data": [],
            "columns": [],
            "rows_affected": 0,
            "execution_time_ms": int((time.time() - start) * 1000),
            "error_message": None,
            "dry_run_only": True,
            "skipped_execution": True,
            "sql_query": sql_query,
        }, start_time=start, sql_query=sql_query, row_limit=row_limit,
            dry_run_only=True, skipped_execution=True)

    plugin = None
    conn = None
    try:
        plugin = _facade.get_plugin(dsn)
        conn = plugin.connect(dsn)
    except Exception as e:
        # EPIC 7.7: если connect не открыл соединение — finally не должен звать close.
        # EPIC 7.3: маскируем DSN в тексте ошибки.
        return _normalize_executor_result(
            _build_failure_result(start, mask_dsn(str(e))),
            start_time=start, sql_query=sql_query, row_limit=row_limit,
        )

    try:
        kind = _classify_statement(sql_query, dsn=dsn)
        strategy = _STRATEGIES.get(kind)
        if strategy is None:
            return _normalize_executor_result(
                _build_failure_result(
                    start,
                    f"Unsupported or unrecognised statement type (classified as '{kind}').",
                ),
                start_time=start, sql_query=sql_query, row_limit=row_limit,
            )
        try:
            result = strategy(
                sql_query=sql_query, plugin=plugin, conn=conn,
                start=start, row_limit=row_limit,
            )
        except Exception as e:
            return _normalize_executor_result(
                _build_failure_result(start, mask_dsn(str(e))),
                start_time=start, sql_query=sql_query, row_limit=row_limit,
            )
        return _normalize_executor_result(
            result, start_time=start, sql_query=sql_query, row_limit=row_limit,
        )
    finally:
        # EPIC 7.7: close зовётся только если conn реально открылся.
        if conn is not None and plugin is not None:
            try:
                plugin.close(conn)
            except Exception as close_err:
                logger.warning(
                    "Не удалось закрыть соединение plugin.close: %s",
                    mask_dsn(str(close_err)),
                )
