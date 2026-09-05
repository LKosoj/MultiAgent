"""DB execution подмодуль core (Phase 7 декомпозиция).

Реализация: secure_db_executor + 8 хелперов парсинга/нормализации DESCRIBE.

Singletons передаются через keyword-only аргументы из фасада.
Внешние зависимости (get_plugin) разрешаются через фасадный модуль
(`custom_tools.text_to_sql.core`), чтобы тесты могли подменять их monkeypatch'ем.
"""
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

from db_plugins.base import (
    CapabilityState,
    DatabaseCapabilities,
    DatabaseCapabilityError,
    EnforcementMode,
    PluginHealth,
    RequiredDatabaseCapabilities,
    validate_required_capabilities,
)
from tool_runtime_context import SupervisorExecutionEvidence

if TYPE_CHECKING:
    from ..validators import TextToSqlSafetyPolicy

from workflow.models import (
    bound_text_to_sql_error,
    preflight_text_to_sql_json_value,
    text_to_sql_executor_contract_error,
    text_to_sql_type_name,
)
from workflow.deadline import DeadlineBudget, WorkflowDeadlineExceeded

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


class QueryPurpose(str, Enum):
    """Closed set of admitted Text-to-SQL database read purposes."""

    FINAL = "FINAL"
    EXPLAIN = "EXPLAIN"
    DISTINCT = "DISTINCT"
    GROUNDING = "GROUNDING"
    CONTAINMENT = "CONTAINMENT"
    RESEARCH = "RESEARCH"


QueryParameter = str | int | float | bool | None


# Purposes for which dry_run_only short-circuits execution before any DB
# connection is opened. EXPLAIN is a preparation read like FINAL — dry-run
# must gate it too, not just FINAL (T14).
_DRY_RUN_GATED_PURPOSES = frozenset({QueryPurpose.FINAL, QueryPurpose.EXPLAIN})


@dataclass(frozen=True, slots=True)
class QueryExecutionRequest:
    """One purpose-bound database read submitted to :class:`QueryExecutor`."""

    sql_query: str
    purpose: QueryPurpose
    row_limit: Optional[int] = None
    dsn: Optional[str] = None
    dry_run_only: Optional[bool] = None
    safety_policy: Optional["TextToSqlSafetyPolicy"] = None
    deadline: Optional[DeadlineBudget] = None
    parameters: tuple[QueryParameter, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.sql_query, str):
            raise TypeError("sql_query must be a string")
        if not isinstance(self.purpose, QueryPurpose):
            raise TypeError("purpose must be a QueryPurpose")
        if self.dry_run_only is not None and type(self.dry_run_only) is not bool:
            raise TypeError("dry_run_only must be a boolean or null")
        if self.deadline is not None and not isinstance(
            self.deadline,
            DeadlineBudget,
        ):
            raise TypeError("deadline must be a DeadlineBudget or null")
        if type(self.parameters) is not tuple:
            raise TypeError("parameters must be an immutable tuple")
        for value in self.parameters:
            if value is None or type(value) in {str, int, bool}:
                continue
            if type(value) is float:
                if not math.isfinite(value):
                    raise ValueError("parameters must contain finite JSON numbers")
                continue
            raise TypeError("parameters must contain exact JSON scalar values")
        if self.parameters and self.purpose is not QueryPurpose.RESEARCH:
            raise ValueError("bound parameters are only supported for RESEARCH")


@dataclass(frozen=True, slots=True)
class QueryExecutionResult:
    """Typed view over the compatibility executor response mapping."""

    purpose: QueryPurpose
    outcome: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, QueryPurpose):
            raise TypeError("purpose must be a QueryPurpose")
        if not isinstance(self.outcome, Mapping):
            raise TypeError("outcome must be a mapping")
        object.__setattr__(self, "outcome", dict(self.outcome))

    @property
    def success(self) -> bool:
        return self.outcome.get("success") is True

    @property
    def data(self) -> list[Any]:
        value = self.outcome.get("data")
        return list(value) if isinstance(value, (list, tuple)) else []

    @property
    def columns(self) -> list[Any]:
        value = self.outcome.get("columns")
        return list(value) if isinstance(value, (list, tuple)) else []

    @property
    def error_message(self) -> Optional[str]:
        value = self.outcome.get("error_message")
        return value if isinstance(value, str) else None

    @property
    def skipped_execution(self) -> bool:
        return self.outcome.get("skipped_execution") is True

    @property
    def executed(self) -> bool:
        return not self.skipped_execution

    @property
    def failure_code(self) -> Optional[str]:
        if self.success:
            return None
        explicit = self.outcome.get("pre_execution_error_code")
        if isinstance(explicit, str) and explicit:
            return explicit
        if self.outcome.get("safety_issues"):
            return "SAFETY_REJECTED"
        return "EXECUTION_FAILED"

    def to_mapping(self) -> Dict[str, Any]:
        return dict(self.outcome)


class QueryExecutor:
    """Purpose-aware entry point shared by final and preparation reads."""

    def __init__(
        self,
        *,
        get_plugin: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._get_plugin = get_plugin

    def execute(self, request: QueryExecutionRequest) -> QueryExecutionResult:
        if not isinstance(request, QueryExecutionRequest):
            raise TypeError("request must be a QueryExecutionRequest")

        from tool_runtime_context import (
            get_runtime_context_deadline,
            get_runtime_context_safety_policy,
            get_runtime_context_supervisor_evidence,
        )

        runtime_deadline = get_runtime_context_deadline()
        runtime_safety_policy = get_runtime_context_safety_policy()
        if request.purpose is QueryPurpose.RESEARCH:
            deadline = _narrow_research_deadline(
                request.deadline,
                runtime_deadline,
            )
            safety_policy = _narrow_research_safety_policy(
                request.safety_policy,
                runtime_safety_policy,
            )
        else:
            deadline = request.deadline or runtime_deadline
            safety_policy = request.safety_policy or runtime_safety_policy
        supervisor_evidence = get_runtime_context_supervisor_evidence()
        outcome = _secure_db_executor(
            request.sql_query,
            request.row_limit,
            request.dsn,
            dry_run_only=request.dry_run_only,
            safety_policy=safety_policy,
            purpose=request.purpose,
            deadline=deadline,
            supervisor_evidence=supervisor_evidence,
            plugin_resolver=self._get_plugin,
            parameters=request.parameters,
        )
        return QueryExecutionResult(request.purpose, outcome)


def _narrow_research_deadline(
    requested: Optional[DeadlineBudget],
    runtime: Optional[DeadlineBudget],
) -> Optional[DeadlineBudget]:
    """Keep a research request inside the authoritative runtime deadline."""
    if runtime is None or requested is None:
        return requested or runtime
    if (
        requested.deadline_monotonic > runtime.deadline_monotonic
        or requested.deadline_at_ms > runtime.deadline_at_ms
    ):
        raise ValueError("RESEARCH request deadline cannot widen runtime deadline")
    return requested


def _narrow_research_safety_policy(
    requested: Optional["TextToSqlSafetyPolicy"],
    runtime: Optional["TextToSqlSafetyPolicy"],
) -> Optional["TextToSqlSafetyPolicy"]:
    """Allow a research request to add restrictions, never remove runtime ones."""
    if runtime is None or requested is None:
        return requested or runtime

    from ..validators import TextToSqlSafetyPolicy

    if not isinstance(requested, TextToSqlSafetyPolicy):
        raise TypeError("safety_policy must be a TextToSqlSafetyPolicy or null")
    if not isinstance(runtime, TextToSqlSafetyPolicy):
        raise TypeError("runtime safety_policy must be a TextToSqlSafetyPolicy")

    def normalized(values: Any) -> frozenset[str]:
        return frozenset(str(value).upper() for value in values)

    requested_restrictions = (
        normalized(requested.forbidden_keywords),
        normalized(requested.forbidden_functions),
        normalized(requested.ast_forbidden_stmt_classes),
        normalized(requested.ast_forbidden_command_words),
    )
    runtime_restrictions = (
        normalized(runtime.forbidden_keywords),
        normalized(runtime.forbidden_functions),
        normalized(runtime.ast_forbidden_stmt_classes),
        normalized(runtime.ast_forbidden_command_words),
    )
    if (
        any(
            not requested_values.issuperset(runtime_values)
            for requested_values, runtime_values in zip(
                requested_restrictions,
                runtime_restrictions,
            )
        )
        or requested.max_query_length > runtime.max_query_length
        or requested.max_in_list_size > runtime.max_in_list_size
    ):
        raise ValueError("RESEARCH request safety_policy cannot widen runtime policy")
    return requested


# Кортеж примитивных JSON-типов вынесен на уровень модуля, чтобы не создавать
# новый объект при каждом вызове _normalize_jsonable с list/dict-значением.
_JSONABLE_PRIMITIVES = (bool, int, float, str, type(None))
_PRE_EXECUTION_ERROR_CODES = frozenset({
    "INVALID_ROW_LIMIT",
    "INVALID_ROW_LIMIT_CAP",
    "INVALID_STATEMENT_TIMEOUT",
})


def _stringify_json_leaf(value: Any) -> str:
    try:
        return str(value)
    except Exception as exc:
        raise TypeError(
            "executor result value cannot be converted to JSON text: "
            f"{bound_text_to_sql_error(exc)}"
        ) from None


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
        return {
            _stringify_json_leaf(k): _normalize_jsonable(v)
            for k, v in value.items()
        }
    return _stringify_json_leaf(value)


def _normalize_executor_result(
    result: Any,
    *,
    start_time: float,
    sql_query: str,
    row_limit: Optional[int],
    safety_issues: Optional[List[Dict[str, Any]]] = None,
    dry_run_only: bool = False,
    skipped_execution: bool = False,
    pre_execution_error_code: Optional[str] = None,
) -> Dict[str, Any]:
    if (
        pre_execution_error_code is not None
        and pre_execution_error_code not in _PRE_EXECUTION_ERROR_CODES
    ):
        raise ValueError("unsupported pre_execution_error_code")
    preflight_text_to_sql_json_value(
        result,
        field_name="executor strategy result",
        allow_non_json_leaves=True,
    )
    if not isinstance(result, dict):
        try:
            dict(result)
        except Exception as exc:
            raise TypeError(bound_text_to_sql_error(exc)) from None
        raise TypeError(
            "executor strategy returned "
            f"{text_to_sql_type_name(result)}, expected object"
        )
    normalized = dict(result)
    normalized.setdefault("success", False)
    normalized["data"] = _normalize_jsonable(normalized.get("data", []))
    normalized["columns"] = _normalize_jsonable(normalized.get("columns", []))
    normalized.setdefault("rows_affected", 0)
    normalized.setdefault("execution_time_ms", int((time.time() - start_time) * 1000))
    normalized.setdefault("error_message", None)
    normalized["safety_issues"] = safety_issues or normalized.get("safety_issues") or []
    normalized["dry_run_only"] = bool(dry_run_only)
    normalized["skipped_execution"] = bool(skipped_execution)
    normalized["sql_query"] = sql_query
    normalized["applied_row_limit"] = row_limit
    if row_limit is None and pre_execution_error_code is not None:
        normalized["pre_execution_error_code"] = pre_execution_error_code
    return normalized


def _validate_research_strategy_result(result: Any, row_limit: int) -> None:
    """Keep RESEARCH rows typed; legacy purposes retain compatibility coercion."""
    if not isinstance(result, dict) or result.get("success") is not True:
        return
    columns = result.get("columns")
    rows = result.get("data")
    if not isinstance(columns, (list, tuple)) or not all(
        type(column) is str and column for column in columns
    ):
        raise TypeError("RESEARCH result columns must be non-empty exact strings")
    if len(columns) != len(set(columns)):
        raise ValueError("RESEARCH result columns must be unique")
    if not isinstance(rows, (list, tuple)) or len(rows) > row_limit:
        raise ValueError("RESEARCH result rows exceed the closed row bound")
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != len(columns):
            raise TypeError("RESEARCH result rows must match the declared columns")
        for value in row:
            if value is None or type(value) in {str, int, bool}:
                continue
            if type(value) is float and math.isfinite(value):
                continue
            raise TypeError("RESEARCH result cells must be exact JSON scalars")


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
    валидатор синтаксиса через sqlglot.tokenize, что
    позволяет ловить мусорные DESCRIBE-формы ещё до execute_select.
    """
    from ..dialects import get_sqlglot_dialect

    try:
        import sqlglot

        dialect = get_sqlglot_dialect()
        sqlglot.tokenize(sql_query, dialect=None if dialect == "ansi" else dialect)
    except Exception as e:
        raise ValueError(
            "sqlglot failed to parse DESCRIBE target: "
            f"{bound_text_to_sql_error(e)}"
        ) from e

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


def _attempted_execution_failure(
    *,
    start_time: float,
    sql_query: str,
    row_limit: int,
    error: Any,
) -> Dict[str, Any]:
    message = bound_text_to_sql_error(error) or "database execution failed"
    return _normalize_executor_result(
        _build_failure_result(start_time, message),
        start_time=start_time,
        sql_query=sql_query,
        row_limit=row_limit,
        dry_run_only=False,
        skipped_execution=False,
    )


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

# Маппинг первого ключевого слова → kind для совместимого regex-классификатора.
_REGEX_KEYWORD_MAP: Dict[str, _StatementKind] = {
    "SELECT": "select",
    "WITH": "select",
    "DESCRIBE": "describe",
    "DESC": "describe",
    "EXPLAIN": "explain",
    "SHOW": "show",
}


def _classify_statement_regex(sql_query: str) -> _StatementKind:
    """Простой regex-классификатор, сохранённый для совместимости unit-тестов.

    Удаляет leading комментарии (-- и /* */), извлекает первое ключевое слово
    и маппирует его в kind. Runtime-маршрутизация всегда использует sqlglot.

    Ограничение: ожидает, что SQL начинается с ключевого слова после комментариев;
    leading non-alpha символы (например ';') не удаляются и приводят к 'unknown'.
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

    При деградации sqlglot import/parse возвращаем "unknown" (fail-closed:
    executor отклонит стейтмент как unsupported). Regex fallback не используется
    runtime-путём, даже если окружение выставило старый ``USE_SQLGLOT=0``.
    """
    from ..dialects import get_sqlglot_dialect
    from ..utils import parse_with_timeout

    try:
        from sqlglot import exp
        from sqlglot import errors as sqlglot_errors
    except Exception as e:
        logger.error(
            "sqlglot import failed inside _classify_statement: %s",
            bound_text_to_sql_error(e),
        )
        return "unknown"

    dialect = get_sqlglot_dialect(dsn, strict=bool(dsn and str(dsn).strip()))
    read = None if dialect == "ansi" else dialect

    try:
        statements = parse_with_timeout(sql_query, read=read)
    except (TimeoutError, sqlglot_errors.ParseError, sqlglot_errors.TokenError) as e:
        logger.warning(
            "sqlglot parse failed in _classify_statement: %s",
            bound_text_to_sql_error(e),
        )
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
        logger.warning(
            "Failed to parse DESCRIBE with introspect_schema: %s",
            bound_text_to_sql_error(e),
        )
        return _build_failure_result(
            start,
            "DESCRIBE introspection failed: "
            f"{bound_text_to_sql_error(e)}",
        )

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
    if not isinstance(explain_result, dict):
        logger.warning(
            "_explain_strategy: plugin.explain() returned unexpected type %s; "
            "expected dict. Degrading to failure result.",
            text_to_sql_type_name(explain_result),
        )
        return _build_failure_result(start, "EXPLAIN plugin returned invalid response format")
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

_PREPARATION_PURPOSE_KINDS: Dict[QueryPurpose, frozenset[str]] = {
    QueryPurpose.EXPLAIN: frozenset({"explain"}),
    QueryPurpose.DISTINCT: frozenset({"select"}),
    QueryPurpose.GROUNDING: frozenset({"select"}),
    QueryPurpose.CONTAINMENT: frozenset({"select"}),
    QueryPurpose.RESEARCH: frozenset({"select"}),
}


def _validate_query_purpose(
    purpose: QueryPurpose,
    statement_kind: _StatementKind,
) -> None:
    allowed = _PREPARATION_PURPOSE_KINDS.get(purpose)
    if allowed is None:
        return
    if statement_kind not in allowed:
        expected = "/".join(sorted(kind.upper() for kind in allowed))
        raise ValueError(
            f"{purpose.value} purpose requires {expected} statement, "
            f"got {statement_kind.upper()}"
        )


def _research_limit_value(limit: Any) -> int:
    """Return a positive literal LIMIT/FETCH count, rejecting dynamic bounds."""
    try:
        from sqlglot import exp
    except ImportError as exc:
        raise ValueError("RESEARCH requires sqlglot SQL parsing") from exc

    expression = getattr(limit, "args", {}).get("expression")
    if expression is None:
        expression = getattr(limit, "args", {}).get("count")
    while isinstance(expression, exp.Paren):
        expression = expression.this
    if not isinstance(expression, exp.Literal) or expression.is_string:
        raise ValueError("RESEARCH requires a positive literal row LIMIT")
    text = expression.this
    if not isinstance(text, str) or not re.fullmatch(r"[1-9][0-9]*", text):
        raise ValueError("RESEARCH requires a positive literal row LIMIT")
    return int(text)


def _validate_research_query(
    sql_query: str,
    row_limit: int,
    dsn: Optional[str],
    parameters: tuple[QueryParameter, ...],
) -> None:
    """Admit one bounded read query before any connection.

    This deliberately uses the normal dialect resolver and parser instead of
    SQLite syntax.  It is an execution boundary, not a query rewriter: callers
    must state their own positive SQL LIMIT.
    """
    from ..dialects import get_sqlglot_dialect
    from ..utils import parse_with_timeout

    try:
        from sqlglot import exp
    except ImportError as exc:
        raise ValueError("RESEARCH requires sqlglot SQL parsing") from exc

    dialect = get_sqlglot_dialect(dsn, strict=bool(dsn and dsn.strip()))
    try:
        statements = parse_with_timeout(
            sql_query,
            read=None if dialect == "ansi" else dialect,
        )
    except Exception as exc:
        raise ValueError(
            "RESEARCH requires one valid bounded SELECT statement"
        ) from exc
    statements = [statement for statement in (statements or []) if statement is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("RESEARCH purpose requires exactly one SELECT statement")

    statement = statements[0]
    unsafe_nodes = tuple(
        node_class
        for node_class in (
            getattr(exp, "DML", None),
            getattr(exp, "DDL", None),
            getattr(exp, "Command", None),
            getattr(exp, "Transaction", None),
            getattr(exp, "Commit", None),
            getattr(exp, "Rollback", None),
            getattr(exp, "Set", None),
            getattr(exp, "Use", None),
            getattr(exp, "Grant", None),
            getattr(exp, "Pragma", None),
        )
        if node_class is not None
    )
    if unsafe_nodes and any(statement.find_all(*unsafe_nodes)):
        raise ValueError("RESEARCH purpose requires a read-only SELECT statement")
    if statement.find(exp.Into, exp.Lock) is not None:
        raise ValueError("RESEARCH purpose requires a read-only SELECT statement")

    columns_class = getattr(exp, "Columns", None)
    has_unsafe_star = any(
        not (
            isinstance(star.parent, exp.Count)
            and star.parent.this is star
            or (
                isinstance(star.parent, exp.Select)
                and star in star.parent.expressions
                and all(value is None for value in star.args.values())
            )
        )
        for star in statement.find_all(exp.Star)
    )
    if has_unsafe_star or (
        columns_class is not None and any(statement.find_all(columns_class))
    ):
        raise ValueError("RESEARCH purpose does not allow dynamic star projections")

    query_limit = statement.args.get("limit")
    if query_limit is None:
        raise ValueError("RESEARCH purpose requires an explicit positive row LIMIT")
    if _research_limit_value(query_limit) > row_limit:
        raise ValueError("RESEARCH SQL LIMIT cannot exceed request row_limit")

    placeholders = list(statement.find_all(exp.Placeholder))
    parameter_class = getattr(exp, "Parameter", None)
    if (
        any(placeholder.this is not None for placeholder in placeholders)
        or (
            parameter_class is not None
            and any(statement.find_all(parameter_class))
        )
    ):
        raise ValueError("RESEARCH requires anonymous positional parameters")
    if len(placeholders) != len(parameters):
        raise ValueError("RESEARCH parameter count does not match SQL placeholders")


def _require_query_deadline(
    deadline: Optional[DeadlineBudget],
    boundary: str,
) -> Optional[float]:
    if deadline is None:
        return None
    return deadline.require_remaining(boundary)


def _deadline_statement_timeout_ms(
    configured_timeout_ms: int,
    deadline: Optional[DeadlineBudget],
) -> int:
    remaining = _require_query_deadline(deadline, "database query execution")
    if remaining is None:
        return configured_timeout_ms
    remaining_ms = max(1, math.floor(remaining * 1000))
    return min(configured_timeout_ms, remaining_ms)


def _parse_positive_int_env(
    env_name: str,
    default: str,
    start: float,
    sql_query: str,
    *,
    evidence_row_limit: Optional[int] = None,
    null_row_limit_error_code: Optional[str] = None,
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    raw_value = os.getenv(env_name, default)
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None, _normalize_executor_result(
            _build_failure_result(start, f"Invalid {env_name}: {raw_value!r}"),
            start_time=start,
            sql_query=sql_query,
            row_limit=evidence_row_limit,
            skipped_execution=True,
            pre_execution_error_code=null_row_limit_error_code,
        )
    if parsed <= 0:
        return None, _normalize_executor_result(
            _build_failure_result(start, f"{env_name} must be a positive integer"),
            start_time=start,
            sql_query=sql_query,
            row_limit=evidence_row_limit,
            skipped_execution=True,
            pre_execution_error_code=null_row_limit_error_code,
        )
    return parsed, None


def _parse_statement_timeout_ms(
    start: float,
    sql_query: str,
    row_limit: Optional[int] = None,
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    return _parse_positive_int_env(
        "DB_EXECUTOR_STATEMENT_TIMEOUT_MS",
        "30000",
        start,
        sql_query,
        evidence_row_limit=row_limit,
        null_row_limit_error_code="INVALID_STATEMENT_TIMEOUT",
    )


def _parse_row_limit(
    row_limit: Optional[int], start: float, sql_query: str
) -> tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Парсит row_limit/cap и возвращает (value, failure_normalized | None)."""
    if row_limit is None:
        raw_row_limit = os.getenv("DB_EXECUTOR_ROW_LIMIT", "500")
        try:
            parsed = int(raw_row_limit)
        except (TypeError, ValueError):
            return None, _normalize_executor_result(
                _build_failure_result(start, f"Invalid DB_EXECUTOR_ROW_LIMIT: {raw_row_limit!r}"),
                start_time=start,
                sql_query=sql_query,
                row_limit=None,
                skipped_execution=True,
                pre_execution_error_code="INVALID_ROW_LIMIT",
            )
    else:
        raw_row_limit = row_limit
        try:
            parsed = int(row_limit)
        except (TypeError, ValueError):
            return None, _normalize_executor_result(
                _build_failure_result(start, f"Invalid row_limit: {raw_row_limit!r}"),
                start_time=start,
                sql_query=sql_query,
                row_limit=None,
                skipped_execution=True,
                pre_execution_error_code="INVALID_ROW_LIMIT",
            )

    if parsed <= 0:
        return None, _normalize_executor_result(
            _build_failure_result(start, "row_limit must be a positive integer"),
            start_time=start,
            sql_query=sql_query,
            row_limit=None,
            skipped_execution=True,
            pre_execution_error_code="INVALID_ROW_LIMIT",
        )

    max_row_limit, cap_failure = _parse_positive_int_env(
        "DB_EXECUTOR_MAX_ROW_LIMIT",
        "10000",
        start,
        sql_query,
        evidence_row_limit=parsed,
        null_row_limit_error_code="INVALID_ROW_LIMIT_CAP",
    )
    if cap_failure is not None:
        return None, cap_failure
    assert max_row_limit is not None
    if parsed > max_row_limit:
        return None, _normalize_executor_result(
            _build_failure_result(
                start,
                f"row_limit {parsed} exceeds DB_EXECUTOR_MAX_ROW_LIMIT {max_row_limit}",
            ),
            start_time=start,
            sql_query=sql_query,
            row_limit=parsed,
            skipped_execution=True,
        )
    return parsed, None


def _enforce_research_runtime_row_limit(
    row_limit: int,
    start: float,
    sql_query: str,
) -> Optional[Dict[str, Any]]:
    authoritative_limit, failure = _parse_positive_int_env(
        "DB_EXECUTOR_ROW_LIMIT",
        "500",
        start,
        sql_query,
        evidence_row_limit=row_limit,
        null_row_limit_error_code="INVALID_ROW_LIMIT",
    )
    if failure is not None:
        return failure
    assert authoritative_limit is not None
    if row_limit <= authoritative_limit:
        return None
    return _normalize_executor_result(
        _build_failure_result(
            start,
            f"row_limit {row_limit} exceeds DB_EXECUTOR_ROW_LIMIT "
            f"{authoritative_limit}",
        ),
        start_time=start,
        sql_query=sql_query,
        row_limit=row_limit,
        skipped_execution=True,
    )


def _apply_statement_timeout(
    plugin,
    conn,
    timeout_ms: int,
    start: float,
    sql_query: str,
    row_limit: int,
    *,
    capabilities: DatabaseCapabilities,
) -> tuple[Optional[Dict[str, Any]], EnforcementMode]:
    timeout_capability = capabilities.statement_timeout
    if timeout_capability.state is not CapabilityState.SUPPORTED:
        failure = _normalize_executor_result(
            _build_failure_result(
                start,
                "Statement timeout capability is unavailable "
                f"({timeout_capability.reason_code})",
            ),
            start_time=start,
            sql_query=sql_query,
            row_limit=row_limit,
            skipped_execution=True,
        )
        failure["capability_error"] = {
            "capability": "statement_timeout",
            "reason_code": timeout_capability.reason_code,
        }
        return failure, EnforcementMode.NONE
    if timeout_capability.mode is EnforcementMode.SUPERVISOR:
        return None, EnforcementMode.SUPERVISOR
    if timeout_capability.mode not in {
        EnforcementMode.DATABASE,
        EnforcementMode.DRIVER,
    }:
        raise RuntimeError("invalid statement timeout enforcement mode")
    setter = getattr(plugin, "set_statement_timeout", None)
    if not callable(setter):
        failure = _normalize_executor_result(
            _build_failure_result(
                start,
                "Declared statement timeout setter is unavailable",
            ),
            start_time=start,
            sql_query=sql_query,
            row_limit=row_limit,
            skipped_execution=True,
        )
        failure["capability_error"] = {
            "capability": "statement_timeout",
            "reason_code": "DECLARED_TIMEOUT_SETTER_MISSING",
        }
        return failure, EnforcementMode.NONE
    try:
        setter(conn, timeout_ms)
    except Exception as e:
        from ..utils import mask_dsn

        return _normalize_executor_result(
            _build_failure_result(
                start,
                mask_dsn(bound_text_to_sql_error(e)),
            ),
            start_time=start,
            sql_query=sql_query,
            row_limit=row_limit,
            skipped_execution=True,
        ), EnforcementMode.NONE
    return None, timeout_capability.mode


def _validate_probe_health(
    declared: DatabaseCapabilities,
    health: PluginHealth,
) -> None:
    if health.dialect != declared.dialect:
        raise DatabaseCapabilityError(
            "production_ready",
            "CAPABILITY_PROBE_DIALECT_MISMATCH",
        )
    for capability_name in declared.to_mapping():
        if capability_name == "dialect":
            continue
        declared_capability = getattr(declared, capability_name)
        observed_capability = getattr(health.capabilities, capability_name)
        if (
            observed_capability.state is CapabilityState.SUPPORTED
            and declared_capability.state is not CapabilityState.SUPPORTED
        ):
            raise DatabaseCapabilityError(
                capability_name,
                "CAPABILITY_PROBE_UPGRADE_FORBIDDEN",
            )
        if (
            observed_capability.state is CapabilityState.SUPPORTED
            and observed_capability.mode is not declared_capability.mode
        ):
            raise DatabaseCapabilityError(
                capability_name,
                "CAPABILITY_PROBE_MODE_CHANGE_FORBIDDEN",
            )


def _secure_db_executor(
    sql_query: str,
    row_limit: Optional[int] = None,
    dsn: Optional[str] = None,
    *,
    dry_run_only: Optional[bool] = None,
    safety_policy: Optional["TextToSqlSafetyPolicy"] = None,
    purpose: QueryPurpose = QueryPurpose.FINAL,
    deadline: Optional[DeadlineBudget] = None,
    supervisor_evidence: Optional[SupervisorExecutionEvidence] = None,
    plugin_resolver: Optional[Callable[[str], Any]] = None,
    parameters: tuple[QueryParameter, ...] = (),
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
    if dry_run_only is not None and type(dry_run_only) is not bool:
        raise TypeError("dry_run_only must be a boolean or null")
    if not isinstance(purpose, QueryPurpose):
        raise TypeError("purpose must be a QueryPurpose")
    if deadline is not None and not isinstance(deadline, DeadlineBudget):
        raise TypeError("deadline must be a DeadlineBudget or null")
    if supervisor_evidence is not None and not isinstance(
        supervisor_evidence,
        SupervisorExecutionEvidence,
    ):
        raise TypeError(
            "supervisor_evidence must be a SupervisorExecutionEvidence or null"
        )
    if type(parameters) is not tuple:
        raise TypeError("parameters must be an immutable tuple")

    logger.info("Executing SQL query securely")
    start = time.time()
    _require_query_deadline(deadline, "database query preparation")

    from ..utils import get_runtime_context_dsn, is_dry_run_only, mask_dsn

    dry_run_only = is_dry_run_only(payload_flag=dry_run_only)

    row_limit, failure = _parse_row_limit(row_limit, start, sql_query)
    if failure is not None:
        failure["dry_run_only"] = dry_run_only
        failure["skipped_execution"] = True
        return failure
    assert row_limit is not None  # для типчекеров
    if purpose is QueryPurpose.RESEARCH:
        failure = _enforce_research_runtime_row_limit(
            row_limit,
            start,
            sql_query,
        )
        if failure is not None:
            failure["dry_run_only"] = dry_run_only
            failure["skipped_execution"] = True
            return failure
    statement_timeout_ms, failure = _parse_statement_timeout_ms(
        start,
        sql_query,
        row_limit,
    )
    if failure is not None:
        failure["dry_run_only"] = dry_run_only
        failure["skipped_execution"] = True
        return failure
    assert statement_timeout_ms is not None

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
    dry_run_gated = dry_run_only and purpose in _DRY_RUN_GATED_PURPOSES
    if not effective_dsn and not dry_run_gated:
        raise MissingDSNError(
            "DSN required: pass dsn via parameter from pipeline context. "
            "Silent DB_DSN env fallback disabled — set "
            "SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1 to opt-in (not recommended)."
        )
    dsn = effective_dsn

    statement_kind: Optional[_StatementKind] = None
    if purpose is not QueryPurpose.FINAL:
        statement_kind = _classify_statement(sql_query, dsn=dsn)
        _validate_query_purpose(purpose, statement_kind)
    if purpose is QueryPurpose.RESEARCH:
        _validate_research_query(sql_query, row_limit, dsn, parameters)

    # Через фасад: тесты monkeypatch'ят core.sql_safety_check / core.get_plugin.
    from custom_tools.text_to_sql import core as _facade

    safety_kwargs: Dict[str, Any] = {"dsn": dsn or ""}
    if purpose is QueryPurpose.RESEARCH:
        safety_kwargs["static_only"] = True
    if safety_policy is not None:
        safety_kwargs["safety_policy"] = safety_policy
    safety = _facade.sql_safety_check(sql_query, **safety_kwargs)
    if not isinstance(safety, dict):
        return _normalize_executor_result(
            _build_failure_result(
                start,
                "Invalid safety check result structure: expected dict, got "
                f"{text_to_sql_type_name(safety)}",
            ),
            start_time=start,
            sql_query=sql_query,
            row_limit=row_limit,
            dry_run_only=dry_run_only,
            skipped_execution=True,
        )
    if not safety.get("is_safe", False):
        issues = safety.get("issues") or []
        return _normalize_executor_result(
            _build_failure_result(start, "Unsafe query.", safety_issues=issues),
            start_time=start, sql_query=sql_query, row_limit=row_limit,
            dry_run_only=dry_run_only,
            skipped_execution=True,
            safety_issues=issues,
        )

    _require_query_deadline(deadline, "database connection")
    if dry_run_gated:
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
        get_plugin = plugin_resolver or _facade.get_plugin
        plugin = get_plugin(dsn)
        capability_getter = getattr(plugin, "get_capabilities", None)
        if not callable(capability_getter):
            raise DatabaseCapabilityError(
                "capability_contract",
                "CAPABILITY_DECLARATION_MISSING",
            )
        capabilities = capability_getter(dsn)
        if not isinstance(capabilities, DatabaseCapabilities):
            raise TypeError("plugin capability declaration is invalid")
        statement_kind = statement_kind or _classify_statement(sql_query, dsn=dsn)
        _validate_query_purpose(purpose, statement_kind)
        required_capabilities = RequiredDatabaseCapabilities(
            explain=statement_kind == "explain",
            introspection=statement_kind == "describe",
            parameter_binding=bool(parameters),
            allow_supervisor=(
                supervisor_evidence is not None and deadline is not None
            ),
        )
        validate_required_capabilities(capabilities, required_capabilities)
    except DatabaseCapabilityError as exc:
        failure = _normalize_executor_result(
            _build_failure_result(start, str(exc)),
            start_time=start,
            sql_query=sql_query,
            row_limit=row_limit,
            skipped_execution=True,
        )
        failure["capability_error"] = {
            "capability": exc.capability,
            "reason_code": exc.reason_code,
        }
        failure["timeout_enforcement_mode"] = "none"
        failure["cancellation_enforcement_mode"] = "none"
        return failure
    except WorkflowDeadlineExceeded:
        raise
    except Exception as e:
        return _normalize_executor_result(
            _build_failure_result(
                start,
                mask_dsn(bound_text_to_sql_error(e)),
            ),
            start_time=start,
            sql_query=sql_query,
            row_limit=row_limit,
            skipped_execution=True,
        )

    try:
        conn = plugin.connect(dsn)
    except WorkflowDeadlineExceeded:
        raise
    except Exception as e:
        # EPIC 7.7: если connect не открыл соединение — finally не должен звать close.
        # EPIC 7.3: маскируем DSN в тексте ошибки.
        return _normalize_executor_result(
            _build_failure_result(
                start,
                mask_dsn(bound_text_to_sql_error(e)),
            ),
            start_time=start, sql_query=sql_query, row_limit=row_limit,
            skipped_execution=True,
        )

    strategy_started = False
    execution_result: Dict[str, Any]
    timeout_mode = EnforcementMode.NONE
    cancellation_mode = EnforcementMode.NONE
    try:
        try:
            probe = getattr(plugin, "probe_capabilities", None)
            if not callable(probe):
                raise DatabaseCapabilityError(
                    "production_ready",
                    "CAPABILITY_PROBE_MISSING",
                )
            health = probe(conn, dsn)
            if not isinstance(health, PluginHealth):
                raise TypeError("plugin capability probe returned an invalid result")
            _validate_probe_health(capabilities, health)
            validate_required_capabilities(
                health.capabilities,
                required_capabilities,
            )
            if not health.production_ready:
                reason_code = (
                    health.reason_codes[0]
                    if health.reason_codes
                    else "PLUGIN_NOT_PRODUCTION_READY"
                )
                raise DatabaseCapabilityError("production_ready", reason_code)
        except DatabaseCapabilityError as exc:
            execution_result = _normalize_executor_result(
                _build_failure_result(start, str(exc)),
                start_time=start,
                sql_query=sql_query,
                row_limit=row_limit,
                skipped_execution=True,
            )
            execution_result["capability_error"] = {
                "capability": exc.capability,
                "reason_code": exc.reason_code,
            }
        except WorkflowDeadlineExceeded:
            raise
        except Exception as exc:
            execution_result = _normalize_executor_result(
                _build_failure_result(
                    start,
                    "Database capability probe failed: "
                    f"{mask_dsn(bound_text_to_sql_error(exc))}",
                ),
                start_time=start,
                sql_query=sql_query,
                row_limit=row_limit,
                skipped_execution=True,
            )
            execution_result["capability_error"] = {
                "capability": "production_ready",
                "reason_code": "CAPABILITY_PROBE_FAILED",
            }
        else:
            capabilities = health.capabilities
            cancellation_mode = capabilities.cancellation.mode
            kind = statement_kind
            _validate_query_purpose(purpose, kind)
            strategy = _STRATEGIES.get(kind)
            if strategy is None:
                execution_result = _normalize_executor_result(
                    _build_failure_result(
                        start,
                        f"Unsupported or unrecognised statement type (classified as '{kind}').",
                    ),
                    start_time=start,
                    sql_query=sql_query,
                    row_limit=row_limit,
                    skipped_execution=True,
                )
            else:
                effective_statement_timeout_ms = _deadline_statement_timeout_ms(
                    statement_timeout_ms,
                    deadline,
                )
                timeout_failure, timeout_mode = _apply_statement_timeout(
                    plugin,
                    conn,
                    effective_statement_timeout_ms,
                    start,
                    sql_query,
                    row_limit,
                    capabilities=capabilities,
                )
                if timeout_failure is not None:
                    execution_result = timeout_failure
                else:
                    strategy_started = True
                    try:
                        if parameters:
                            execute_bound = getattr(
                                plugin,
                                "execute_select_bound",
                                None,
                            )
                            if not callable(execute_bound):
                                raise RuntimeError(
                                    "declared parameter binding implementation is missing"
                                )
                            raw_result = execute_bound(
                                conn,
                                sql_query,
                                parameters,
                                row_limit,
                            )
                        else:
                            raw_result = strategy(
                                sql_query=sql_query,
                                plugin=plugin,
                                conn=conn,
                                start=start,
                                row_limit=row_limit,
                            )
                    except WorkflowDeadlineExceeded:
                        raise
                    except Exception as exc:
                        execution_result = _attempted_execution_failure(
                            start_time=start,
                            sql_query=sql_query,
                            row_limit=row_limit,
                            error=mask_dsn(bound_text_to_sql_error(exc)),
                        )
                    else:
                        try:
                            if purpose is QueryPurpose.RESEARCH:
                                _validate_research_strategy_result(
                                    raw_result,
                                    row_limit,
                                )
                            normalized_result = _normalize_executor_result(
                                raw_result,
                                start_time=start,
                                sql_query=sql_query,
                                row_limit=row_limit,
                            )
                        except Exception as exc:
                            detail = mask_dsn(bound_text_to_sql_error(exc))
                            execution_result = _attempted_execution_failure(
                                start_time=start,
                                sql_query=sql_query,
                                row_limit=row_limit,
                                error=(
                                    "Executor result normalization failed: "
                                    f"{detail}"
                                ),
                            )
                        else:
                            contract_error = text_to_sql_executor_contract_error(
                                normalized_result,
                                expected_dry_run_only=False,
                                expected_sql_query=sql_query,
                                expected_row_limit=row_limit,
                            )
                            if contract_error is None:
                                execution_result = normalized_result
                            else:
                                execution_result = _attempted_execution_failure(
                                    start_time=start,
                                    sql_query=sql_query,
                                    row_limit=row_limit,
                                    error=(
                                        "Executor result contract invalid: "
                                        f"{contract_error}"
                                    ),
                                )
    finally:
        # Every admitted connection is closed, including probe rejection.
        if conn is not None and plugin is not None:
            try:
                plugin.close(conn)
            except Exception as close_err:
                close_error = mask_dsn(bound_text_to_sql_error(close_err))
                logger.warning(
                    "Не удалось закрыть соединение plugin.close: %s",
                    close_error,
                )
                if strategy_started:
                    execution_result = _attempted_execution_failure(
                        start_time=start,
                        sql_query=sql_query,
                        row_limit=row_limit,
                        error=f"Database connection close failed: {close_error}",
                    )
    execution_result["timeout_enforcement_mode"] = timeout_mode.value
    execution_result["cancellation_enforcement_mode"] = cancellation_mode.value
    # W4-4.1: surface the safety check's LLM-advisory verdict (and any advisory
    # findings) on the executor result so the terminal finalizer can quote it
    # in the run's provenance footer. `safety` was computed above, before the
    # DSN connection was even opened, and is untouched since.
    llm_audit = safety.get("llm_audit")
    if isinstance(llm_audit, str):
        execution_result["llm_audit"] = llm_audit
    advisory_issues = safety.get("advisory_issues")
    if isinstance(advisory_issues, list):
        execution_result["advisory_issues"] = advisory_issues
    return execution_result


def secure_db_executor(
    sql_query: str,
    row_limit: Optional[int] = None,
    dsn: Optional[str] = None,
    *,
    dry_run_only: Optional[bool] = None,
    safety_policy: Optional["TextToSqlSafetyPolicy"] = None,
    sql_validator,
) -> Dict[str, object]:
    from custom_tools.text_to_sql import core as _facade

    policy = _facade._request_safety_policy(
        safety_policy,
        sql_validator=sql_validator,
        fallback_to_startup_policy=True,
    )

    result = QueryExecutor().execute(
        QueryExecutionRequest(
            sql_query=sql_query,
            purpose=QueryPurpose.FINAL,
            row_limit=row_limit,
            dsn=dsn,
            dry_run_only=dry_run_only,
            safety_policy=safety_policy,
        )
    ).to_mapping()
    result["profile_name"] = policy.profile_name
    result["policy_version"] = policy.policy_version
    return result
