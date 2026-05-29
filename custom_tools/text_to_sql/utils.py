"""
Утилиты для Text-to-SQL пайплайна
"""
import concurrent.futures
import os
import re
import json
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, unquote_plus, urlparse

logger = logging.getLogger(__name__)

_SENSITIVE_DSN_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "credentials",
    "key",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "token",
}
_NON_SECRET_TOKEN_KEYS = {
    "completion_tokens",
    "input_tokens",
    "max_tokens",
    "output_tokens",
    "prompt_tokens",
    "token_count",
    "total_tokens",
}
_SENSITIVE_KEY_PREFIXES = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "credentials",
    "database_dsn",
    "database_url",
    "db_dsn",
    "db_password",
    "db_url",
    "dsn",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "refresh_token",
    "secret",
    "secret_access_key",
    "secret_key",
    "token",
}
_DSN_NAMESPACE_SECRET_KEYS = {
    "user",
    "username",
    "uid",
    "userid",
    "user_id",
    "password",
    "passwd",
    "pwd",
}


# === EPIC 7.3: DSN-маскировка для безопасного логирования ошибок ===
_DSN_CRED_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+\-.]*)://(?P<userinfo>[^@/\s]*)@"
)
# W7-T1: libpq keyword/value-форма ("host=... user=... password=secret").
# psycopg/libpq принимает такие строки наравне с URI; OperationalError может
# содержать всю строку подключения целиком, поэтому маскируем секретные
# keyword'ы отдельным regex. Покрываем: password, passwd (исторические libpq
# алиасы). passfile сознательно НЕ маскируем — это путь к pgpass-файлу,
# не сам пароль; маскировать путь — потеря диагностики без выигрыша по
# безопасности.
# W1-review: значение сужено до query/whitespace границ с отдельной поддержкой
# braced values. ``;`` внутри секрета маскируется, но ``;KEY=`` считается
# началом следующего ODBC/libpq-like параметра.
_LIBPQ_PASSWORD_RE = re.compile(
    r"\b(?P<key>password|passwd)\s*=\s*(?P<val>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    flags=re.IGNORECASE,
)
# ODBC connection string: ``Driver={...};Server=...;Pwd=secret;Password=secret``.
# Отличается от libpq формы значением-разделителем (``;`` вместо whitespace) и
# набором допустимых ключей (``Pwd``/``Password``, case-insensitive).
_ODBC_PASSWORD_RE = re.compile(
    r"\b(?P<key>Password|Pwd)\s*=\s*(?P<val>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    flags=re.IGNORECASE,
)
# URL query-string секреты: ``?password=...&token=...``. SQLAlchemy и ряд
# драйверов кладут учётные данные не в userinfo, а в query (особенно для
# Azure/AWS managed-identity и DSN с дополнительными ``connect_args``).
# Покрываем ключи: password/pwd/auth/token/secret/api_key/apikey —
# конфликты с легитимными query-параметрами (например, ``token=table``)
# редки и приемлемы, цена ложного маскирования меньше утечки.
_URL_QUERY_CRED_RE = re.compile(
    r"(?P<sep>[?&;])(?P<key>[^=&;\s]+)=(?P<val>\{[^}]*\}|(?:[^&;\s]|;(?![A-Za-z_][A-Za-z0-9_]*\s*=))+)",
    flags=re.IGNORECASE,
)
_SECRET_KEY_PATTERN = r"[A-Za-z0-9_%+\-.\[\]]+"
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"\b(?P<key>{_SECRET_KEY_PATTERN})"
    r"\s*(?:=|:(?!\s*[^\s,&;]*=))\s*"
    r"(?P<val>\{[^}]*\}|(?:[^\s,&;]|;(?![A-Za-z_][A-Za-z0-9_]*\s*[:=]))+)",
    flags=re.IGNORECASE,
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?P<key_quote>['\"])(?P<key>{_SECRET_KEY_PATTERN})"
    rf"(?P=key_quote)\s*:\s*)"
    r"(?P<value_quote>['\"])(?P<val>(?:\\.|(?!(?P=value_quote)).)*)"
    r"(?P=value_quote)",
    flags=re.IGNORECASE,
)
_AUTHORIZATION_SECRET_RE = re.compile(
    r"(?P<prefix>\bauthorization\s*[:=]\s*)"
    r"(?P<val>(?:Bearer|Basic|Digest|Token)\s+[^\s,&;]+|[^\s,&;]+)",
    flags=re.IGNORECASE,
)


def get_runtime_context_dsn() -> Optional[str]:
    """Return workflow-provided DSN for tool calls, if one is active."""
    try:
        from tool_runtime_context import get_tool_runtime_value
    except ImportError:
        return None

    dsn = get_tool_runtime_value("dsn")
    if isinstance(dsn, str) and dsn.strip():
        return dsn
    return None


def _normalize_dsn_key(key: Any) -> str:
    text = unquote_plus(str(key))
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_").casefold()


def is_sensitive_secret_key(key: Any) -> bool:
    normalized = _normalize_dsn_key(key)
    if normalized in _NON_SECRET_TOKEN_KEYS:
        return False
    if any(
        normalized == prefix or normalized.startswith(f"{prefix}_")
        for prefix in _SENSITIVE_KEY_PREFIXES
    ):
        return True
    return (
        normalized in _SENSITIVE_DSN_QUERY_KEYS
        or "api_key" in normalized
        or "apikey" in normalized
        or "authorization" in normalized
        or "credentials" in normalized
        or "private_key" in normalized
        or "secret_access_key" in normalized
        or "secret_key" in normalized
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or normalized.endswith("_password")
    )


def mask_dsn_value(dsn: str) -> str:
    """Маскирует одиночный DSN-литерал (например, значение env DB_DSN).

    Поддерживает формы:
      * URI: ``scheme://user:password@host/db``;
      * libpq keyword/value: ``host=... password=secret``;
      * ODBC connection string: ``...;Pwd=secret;...``;
      * URL query: ``?password=...`` / ``&token=...``.
    """
    if not dsn:
        return dsn or ""

    def _sub_uri(match: "re.Match[str]") -> str:
        return f"{match.group('scheme')}://***:***@"

    def _sub_libpq(match: "re.Match[str]") -> str:
        # Сохраняем оригинальный регистр ключа (libpq case-insensitive,
        # но логи человеку читать удобнее в исходном виде).
        return f"{match.group('key')}=***"

    def _sub_odbc(match: "re.Match[str]") -> str:
        return f"{match.group('key')}=***"

    def _sub_url_query(match: "re.Match[str]") -> str:
        key = match.group("key")
        normalized_key = _normalize_dsn_key(key)
        if normalized_key == "odbc_connect":
            return f"{match.group('sep')}{key}=***"
        if normalized_key not in _DSN_NAMESPACE_SECRET_KEYS and not is_sensitive_secret_key(key):
            return match.group(0)
        return f"{match.group('sep')}{key}=***"

    masked = _DSN_CRED_RE.sub(_sub_uri, dsn)
    # W1-review: URL-query маскируем РАНЬШЕ libpq, иначе LIBPQ_PASSWORD_RE
    # съедает хвост ``?password=...&driver=ODBC+Driver+17`` и теряет driver.
    masked = _URL_QUERY_CRED_RE.sub(_sub_url_query, masked)
    masked = _AUTHORIZATION_SECRET_RE.sub(r"\g<prefix>***", masked)
    masked = _QUOTED_SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('value_quote')}***{match.group('value_quote')}"
            if (
                _normalize_dsn_key(match.group("key")) in _DSN_NAMESPACE_SECRET_KEYS
                or is_sensitive_secret_key(match.group("key"))
            )
            else match.group(0)
        ),
        masked,
    )
    masked = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('key')}{match.group(0)[len(match.group('key')):match.start('val') - match.start()]}***"
            if (
                _normalize_dsn_key(match.group("key")) in _DSN_NAMESPACE_SECRET_KEYS
                or is_sensitive_secret_key(match.group("key"))
            )
            else match.group(0)
        ),
        masked,
    )
    masked = _LIBPQ_PASSWORD_RE.sub(_sub_libpq, masked)
    masked = _ODBC_PASSWORD_RE.sub(_sub_odbc, masked)
    return masked


def mask_dsn(text: str) -> str:
    """Маскирует учётные данные в DSN-подобных подстроках произвольного текста.

    Заменяет user[:password]@ на ***:***@ (URI-форма) и
    ``password=...``/``passwd=...`` на ``password=***`` (libpq keyword-форма).
    Дополнительно подменяет literal-вхождения значения env ``DB_DSN`` — на
    случай драйверов, которые кладут DSN в текст ошибки целиком. Пустой/None
    становится пустой строкой.
    """
    if not text:
        return text or ""

    masked = mask_dsn_value(text)

    dsn_env = os.getenv("DB_DSN", "")
    if dsn_env and dsn_env in masked:
        masked = masked.replace(dsn_env, mask_dsn_value(dsn_env))
    return masked


def redact_text_to_sql_value(value: Any) -> Any:
    """Fail-closed redaction for Text-to-SQL prompt/log/API boundaries."""
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        def redact_string(text: Any) -> str:
            masked = mask_dsn(str(text))
            return str(redact_pii_in_payload(_redact_payload(masked)))

        def redact_key(key: Any) -> Any:
            if isinstance(key, str):
                return redact_string(key)
            if key is None or isinstance(key, (int, float, bool)):
                return key
            return redact_string(key)

        def visit(item: Any) -> Any:
            if isinstance(item, BaseException):
                return redact_string(item)
            if isinstance(item, dict):
                return {redact_key(key): visit(child) for key, child in item.items()}
            if isinstance(item, list):
                return [visit(child) for child in item]
            if isinstance(item, tuple):
                return tuple(visit(child) for child in item)
            if isinstance(item, str):
                return redact_string(item)
            return item

        return visit(value)
    except Exception:
        return "<redacted>"


# === EPIC 7.27: защита от долгого sqlglot.parse ===
# Module-level executor: per-call overhead минимален, daemon-потоки не блокируют
# завершение процесса. max_workers=2 — для редких таймаутов; нормальные вызовы
# быстрые, так что очередь не создаётся.
_SQLGLOT_PARSE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="sqlglot-parse",
)


def _get_max_sql_length() -> int:
    raw = os.getenv("TEXT_TO_SQL_MAX_SQL_LENGTH", "50000")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid TEXT_TO_SQL_MAX_SQL_LENGTH: {raw!r}") from exc


def _get_sqlglot_parse_timeout_s() -> float:
    raw = os.getenv("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S", "5")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid TEXT_TO_SQL_SQLGLOT_TIMEOUT_S: {raw!r}") from exc
    if value <= 0:
        raise ValueError("TEXT_TO_SQL_SQLGLOT_TIMEOUT_S must be positive")
    return value


def parse_with_timeout(
    sql: str,
    *,
    read: Optional[str] = None,
    timeout_s: Optional[float] = None,
) -> List[Any]:
    """Обёртка над ``sqlglot.parse`` с length-cap'ом и таймаутом.

    Бросает ValueError при превышении ``TEXT_TO_SQL_MAX_SQL_LENGTH`` и
    ``TimeoutError`` при превышении ``timeout_s`` (по умолчанию
    ``TEXT_TO_SQL_SQLGLOT_TIMEOUT_S``). Метрика
    ``record_sqlglot_metric("parse_timeout")`` инкрементируется при таймауте.
    """
    if not isinstance(sql, str):
        raise TypeError(f"parse_with_timeout expects str, got {type(sql).__name__}")

    max_len = _get_max_sql_length()
    if len(sql) > max_len:
        raise ValueError(f"SQL exceeds max length ({len(sql)} > {max_len})")

    effective_timeout = timeout_s if timeout_s is not None else _get_sqlglot_parse_timeout_s()

    import sqlglot  # lazy

    future = _SQLGLOT_PARSE_EXECUTOR.submit(sqlglot.parse, sql, read=read)
    try:
        return future.result(timeout=effective_timeout)
    except concurrent.futures.TimeoutError as exc:
        try:
            from .validators import record_sqlglot_metric
            record_sqlglot_metric("parse_timeout")
        except Exception:
            logger.debug("record_sqlglot_metric unavailable; skipping parse_timeout metric")
        raise TimeoutError(
            f"sqlglot.parse exceeded timeout of {effective_timeout}s"
        ) from exc


def get_repo_root() -> Path:
    """Возвращает корень репозитория относительно `utils.__file__`.

    utils.py всегда файл (не package), поэтому путь стабилен:
    `custom_tools/text_to_sql/utils.py` → `parents[2]` = repo root.

    Использовать как единый источник истины для модулей, которым не нужна
    совместимость с monkeypatch'ингом core-фасада (см. `get_facade_repo_root`).
    """
    return Path(__file__).resolve().parents[2]


def get_facade_repo_root() -> Path:
    """Возвращает корень репозитория относительно core-фасада.

    Контракт: тесты monkeypatch'ят `core.__file__` и ожидают, что repo_root
    вычисляется относительно фасадного модуля. В оригинале core был файлом
    (`core.py`) — `parents[2]` давало <repo>. После Phase 7 декомпозиции
    core — package; для package (`__init__.py`) нужен `parents[3]`.

    Используется в `core/_audit.py` для путей `logs/audit.log` и `sqlrag/*.md`,
    чтобы тестовый monkeypatch продолжал работать.
    """
    from custom_tools.text_to_sql import core as _facade
    facade_path = Path(_facade.__file__).resolve()
    return facade_path.parents[3] if facade_path.name == "__init__.py" else facade_path.parents[2]


def get_table_columns(table_schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Получает колонки таблицы из нового формата схемы."""
    columns = table_schema.get("columns", {}) if isinstance(table_schema, dict) else {}
    if isinstance(columns, dict) and columns:
        return columns
    if isinstance(table_schema, dict):
        return {
            key: value
            for key, value in table_schema.items()
            if isinstance(value, dict) and key not in {"description", "metadata"}
        }
    return {}

def get_table_description(table_schema: Dict[str, Any]) -> str:
    """Получает описание таблицы из нового формата схемы."""
    return str(table_schema.get("description", "")).strip()

def set_table_description(table_schema: Dict[str, Any], description: str) -> None:
    """Устанавливает описание таблицы в новом формате схемы."""
    table_schema["description"] = description

def create_table_schema(description: str = "", columns: Dict[str, Dict[str, Any]] = None) -> Dict[str, Any]:
    """Создает схему таблицы в новом формате."""
    schema = {}
    schema["description"] = description
    schema["columns"] = columns or {}
    return schema

def parse_llm_json_response(response: str) -> Dict[str, Any]:
    """Парсит JSON-ответ LLM без мутаций исходного текста (W2-T6).

    Стратегия (в строгом порядке, без слепых replace):
      1. ``json.loads(raw)`` — самый частый happy-path, когда LLM вернул
         корректный JSON.
      2. Извлечь содержимое из ``code-fence`` (```json ... ``` или ``` ... ```),
         если LLM обернул JSON в markdown.
      3. Substring между первым ``{`` и последним ``}`` — последний шанс, когда
         LLM приписал prose до/после JSON.

    Никаких ``replace('\\\\u', '\\u')``/``replace('\\\\n', '\\n')``: подобные
    «починки» ломают корректный JSON с экранированными литералами (например,
    SQL ``SELECT '\\n'`` после «исправления» превратится в literal newline).

    Args:
        response: Сырой ответ от LLM.

    Returns:
        Распарсенный JSON-объект.

    Raises:
        ValueError: Если ответ пустой или не удалось распарсить ни одной
            из стратегий. Raw-payload помещается в DEBUG-лог, не в exception.
    """
    if not response or not response.strip():
        raise ValueError("Empty response from LLM")

    raw = response.strip()

    # Шаг 1: пытаемся распарсить как есть — без какой-либо предобработки.
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_err:
        last_err: Exception = first_err

    # Шаг 2: code-fence. Сначала ```json, потом generic ```.
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fence_match is not None:
        candidate = fence_match.group(1).strip()
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                last_err = e

    # Шаг 3: substring между первым '{' и последним '}'.
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = raw[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            last_err = e

    # Все три стратегии провалились — fail-fast.
    logger.debug("LLM JSON parse failed; raw payload: %r", raw)
    raise ValueError(f"LLM JSON parse failed: {last_err}")

# Кэш для мемоизации get_schema_version
_schema_version_cache: Dict[str, str] = {}


_KEY_VALUE_DSN_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_%+\-.]*)\s*=\s*"
    r"(?P<value>\{[^}]*\}|[^;\s]+)"
)
_KEY_VALUE_DSN_IDENTITY_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("driver", ("driver",)),
    ("host", ("host", "server", "addr", "address")),
    ("port", ("port",)),
    ("database", ("dbname", "database", "db")),
    ("schema", ("schema",)),
    ("service", ("service",)),
)


def _stable_dsn_hash_name(dsn: str) -> str:
    digest = hashlib.sha256(str(dsn).encode("utf-8")).hexdigest()[:16]
    return f"db_{digest}"


def _strip_braced_dsn_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "{" and value[-1] == "}":
        return value[1:-1]
    return value


def _keyword_dsn_identity_tokens(dsn: str) -> Optional[List[str]]:
    matches = list(_KEY_VALUE_DSN_RE.finditer(dsn))
    if not matches:
        return None

    values: Dict[str, str] = {}
    for match in matches:
        raw_key = match.group("key")
        normalized_key = _normalize_dsn_key(raw_key)
        if normalized_key in _DSN_NAMESPACE_SECRET_KEYS or is_sensitive_secret_key(raw_key):
            continue
        value = _strip_braced_dsn_value(match.group("value"))
        if value:
            values.setdefault(normalized_key, value)

    tokens: List[str] = []
    for label, aliases in _KEY_VALUE_DSN_IDENTITY_KEYS:
        for alias in aliases:
            if alias in values:
                tokens.extend([label, values[alias]])
                break
    return tokens


def _url_query_dsn_identity_tokens(query: str) -> Optional[List[str]]:
    if not query:
        return None

    values: Dict[str, str] = {}
    for raw_key, raw_value in parse_qsl(query, keep_blank_values=False):
        normalized_key = _normalize_dsn_key(raw_key)
        if normalized_key in _DSN_NAMESPACE_SECRET_KEYS or is_sensitive_secret_key(raw_key):
            continue
        if normalized_key == "odbc_connect":
            tokens = _keyword_dsn_identity_tokens(raw_value)
            if tokens:
                return tokens
            continue
        if raw_value:
            values.setdefault(normalized_key, raw_value)

    tokens: List[str] = []
    for label, aliases in _KEY_VALUE_DSN_IDENTITY_KEYS:
        for alias in aliases:
            if alias in values:
                tokens.extend([label, values[alias]])
                break
    return tokens or None


def _sanitize_dsn_name_tokens(tokens: List[str], fallback_dsn: str) -> str:
    if not tokens:
        return _stable_dsn_hash_name(fallback_dsn)
    base = "_".join(str(token) for token in tokens if token)
    base = base.lower()
    base = re.sub(r"[^a-z0-9_]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("_")
    return base or _stable_dsn_hash_name(fallback_dsn)


def dsn_to_sanitized_name(dsn: str) -> str:
    """Преобразует DSN в безопасное имя файла."""
    if not dsn:
        return "db"
    try:
        p = urlparse(dsn)
        scheme = (p.scheme or "").strip().lower()
        if not scheme:
            tokens = _keyword_dsn_identity_tokens(dsn)
            if tokens is None:
                return _stable_dsn_hash_name(dsn)
            return _sanitize_dsn_name_tokens(tokens, dsn)

        host = (p.hostname or "").strip().lower()
        port = str(p.port) if p.port else ""
        path = (p.path or "").strip("/")
        db = path
        schema_part = ""
        # Стандарт: db.schema (кроме файловых БД)
        if path and "." in path and not path.endswith((".db", ".duckdb", ".sqlite")):
            db, schema_part = path.split(".", 1)
        # DuckDB форматы: /path/file.db/schema ИЛИ /path/file.db.schema
        elif scheme == "duckdb":
            if ".db." in path:
                db, schema_part = path.split(".db.", 1)
            elif ".duckdb." in path:
                db, schema_part = path.split(".duckdb.", 1)
            else:
                parts = path.split("/")
                if len(parts) >= 2 and parts[-2].endswith((".db", ".duckdb")):
                    db = "/".join(parts[:-1])
                    schema_part = parts[-1]
        query_tokens = _url_query_dsn_identity_tokens(p.query)
        identity_tokens = [t for t in [host, port, db, schema_part] if t]
        if query_tokens:
            identity_tokens.extend(query_tokens)
        if identity_tokens:
            return _sanitize_dsn_name_tokens([scheme, *identity_tokens], dsn)
        return _stable_dsn_hash_name(mask_dsn_value(dsn))
    except Exception as exc:
        logger.warning("dsn_to_sanitized_name: ошибка при парсинге DSN, используется hash-имя: %s", exc)
        return _stable_dsn_hash_name(dsn)


_DRY_RUN_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})
_DRY_RUN_FALSE_TOKENS = frozenset({"", "0", "false", "no", "off"})


def coerce_strict_bool(value: Any, *, default: bool = False, field_name: str = "flag") -> bool:
    """Строгая нормализация булевых значений (fail-fast).

    Принимает:
    - ``None`` → ``default``;
    - ``bool`` без изменений;
    - ``int`` 0 / 1;
    - строки `1/0/true/false/yes/no/on/off` (любой регистр, пустая строка = False).

    На любое другое значение поднимает ``ValueError`` — никакого silent
    приведения через ``bool(value)``. Используется для AG-UI service actions
    и workflow-параметров, где нечёткость недопустима.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _DRY_RUN_TRUE_TOKENS:
            return True
        if normalized in _DRY_RUN_FALSE_TOKENS:
            return False
    raise ValueError(f"{field_name} must be boolean")


def is_dry_run_only(payload_flag: Optional[bool] = None) -> bool:
    """Единая точка чтения dry-run флага.

    Источники:
    - переменная окружения ``TEXT_TO_SQL_DRY_RUN_ONLY`` (1/true/yes/on);
    - явный per-request `payload_flag` (если передан и truthy).

    ENV и payload объединяются через OR — любой источник включает dry-run.
    Невалидные значения ENV (например, ``"maybe"``) **не** молча проглатываются —
    вызывают ``ValueError`` (fail-fast по AGENTS.md, без silent fallback).
    """
    env_value = os.getenv("TEXT_TO_SQL_DRY_RUN_ONLY")
    env_flag = False
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in _DRY_RUN_TRUE_TOKENS:
            env_flag = True
        elif normalized in _DRY_RUN_FALSE_TOKENS:
            env_flag = False
        else:
            # Невалидное значение — фейлим явно, чтобы не было silent fallback.
            raise ValueError(
                f"TEXT_TO_SQL_DRY_RUN_ONLY has unsupported value {env_value!r}; "
                "use 1/0/true/false/yes/no/on/off"
            )
    if payload_flag is True:
        return True
    return env_flag


def get_schema_version(db_schema: Optional[Dict[str, Any]] = None) -> str:
    """Получает версию схемы для кэширования с мемоизацией."""
    global _schema_version_cache
    
    # Проверяем переменную окружения (высший приоритет)
    env_version = os.getenv("SCHEMA_VERSION", "").strip()
    if env_version:
        return env_version
    
    # Создаем ключ кэша
    if db_schema:
        cache_key = f"schema_hash_{hashlib.md5(json.dumps(db_schema, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()}"
    else:
        dsn = os.getenv("DB_DSN", "")
        cache_key = f"dsn_{hashlib.md5(dsn.encode('utf-8')).hexdigest()}" if dsn else "default"

    # Проверяем кэш
    if cache_key in _schema_version_cache:
        logger.debug(
            "get_schema_version: cache hit для ключа %s, версия %s",
            cache_key,
            _schema_version_cache[cache_key],
        )
        return _schema_version_cache[cache_key]

    # Вычисляем версию
    try:
        if db_schema:
            payload = json.dumps(db_schema, ensure_ascii=False, sort_keys=True, default=str)
            version = hashlib.md5(payload.encode("utf-8")).hexdigest()
        else:
            # Попытка получить схему из текущего соединения для более стабильного кэширования
            dsn = os.getenv("DB_DSN", "")
            if dsn:
                logger.warning(
                    "get_schema_version: db_schema=None, выполняется интроспекция из DB_DSN; "
                    "для отключения передавайте db_schema явно"
                )
                try:
                    from db_plugins import get_plugin  # lazy: 3.18 — без top-level import
                    plugin = get_plugin(dsn)
                    conn = plugin.connect(dsn)
                    try:
                        # Извлекаем schema из DB_DSN формата /db.schema или /file.db/schema
                        p = urlparse(dsn)
                        path = (p.path or "").strip("/")
                        schema_arg = None
                        
                        if dsn.startswith("duckdb://"):
                            # DuckDB может иметь схему в формате:
                            # duckdb:///path/file.db/schema_name
                            # duckdb:///path/file.db.schema_name
                            
                            # Сначала проверяем формат file.db.schema (более специфичный)
                            if ".db." in path:
                                # file.db.schema -> берем все после последней .db.
                                schema_part = path.split(".db.")[-1]
                                if schema_part and "/" not in schema_part:  # Простое имя схемы без путей
                                    schema_arg = schema_part
                            elif ".duckdb." in path:
                                # file.duckdb.schema -> берем все после последней .duckdb.
                                schema_part = path.split(".duckdb.")[-1]
                                if schema_part and "/" not in schema_part:  # Простое имя схемы без путей
                                    schema_arg = schema_part
                            elif "/" in path and path.count("/") > 0:
                                # Формат: /path/file.db/schema_name
                                parts = path.split("/")
                                if len(parts) >= 2 and parts[-2].endswith((".db", ".duckdb")):
                                    schema_arg = parts[-1]  # Последняя часть - схема
                        elif path and "." in path and not path.endswith((".db", ".duckdb", ".sqlite")):
                            # Для других БД: стандартный формат db.schema
                            _, schema_arg = path.split(".", 1)
                        current_schema = plugin.introspect_schema(conn, schema_arg) or {}
                        if current_schema:
                            payload = json.dumps(current_schema, ensure_ascii=False, sort_keys=True, default=str)
                            version = hashlib.md5(payload.encode("utf-8")).hexdigest()
                        else:
                            version = "unknown"
                    finally:
                        plugin.close(conn)
                except Exception:
                    version = "unknown"
            else:
                version = "unknown"
    except Exception:
        version = "unknown"
    
    # Сохраняем в кэш
    if version == "unknown":
        logger.warning(
            "get_schema_version: не удалось вычислить версию схемы (ключ %s); кэшируется \"unknown\"",
            cache_key,
        )
    _schema_version_cache[cache_key] = version
    return version


def clear_schema_version_cache() -> None:
    """Очищает кэш версий схемы."""
    global _schema_version_cache
    _schema_version_cache.clear()


def split_schema_table(qualified: str) -> tuple[Optional[str], str]:
    """Разбивает квалифицированное имя таблицы на схему и таблицу."""
    parts = (qualified or "").split(".")
    if len(parts) >= 2:
        return ".".join(parts[:-1]) or None, parts[-1]
    return None, qualified


def optimize_column_info(col_info: Dict[str, Any], include_name: bool = False) -> Dict[str, Any]:
    """Оптимизирует информацию о колонке, удаляя незначащие поля.
    
    Убирает поля, которые содержат только значения по умолчанию:
    - constraint_type: "" (пустая строка)
    - references: "" (пустая строка) 
    - not_null: "False" или "" (пустая строка)
    - default_value: "" (пустая строка)
    
    Оставляет только значимые поля и обязательные type, description.
    
    Args:
        col_info: Словарь с информацией о колонке
        include_name: Включать ли поле name в результат
        
    Returns:
        Оптимизированный словарь с информацией о колонке
    """
    if not isinstance(col_info, dict):
        return {}
        
    # Всегда сохраняем тип и описание
    optimized_col = {
        "type": col_info.get("type", ""),
        "description": col_info.get("description", "")
    }
    
    # Включаем name если нужно
    if include_name:
        optimized_col["name"] = col_info.get("name", "")
    
    # Добавляем только значимые поля
    constraint_type = col_info.get("constraint_type", "")
    if constraint_type and constraint_type.strip():
        optimized_col["constraint_type"] = constraint_type
    
    references = col_info.get("references", "")
    if references and references.strip():
        optimized_col["references"] = references
    
    not_null = col_info.get("not_null", "")
    if not_null and not_null.strip() and not_null.lower() != "false":
        optimized_col["not_null"] = not_null
    
    default_value = col_info.get("default_value", "")
    if default_value and default_value.strip():
        optimized_col["default_value"] = default_value
    
    # Обрабатываем is_primary_key (может быть в разных форматах)
    is_primary_key = col_info.get("is_primary_key")
    if is_primary_key:
        # Нормализуем булевое значение
        if isinstance(is_primary_key, str):
            is_primary_key = is_primary_key.lower() in ('true', '1', 'yes', 'on')
        if is_primary_key:
            optimized_col["is_primary_key"] = True
    
    return optimized_col


def configure_logging() -> None:
    """Настраивает логирование для модуля, если оно еще не настроено."""
    # Проверяем, настроено ли уже логирование на уровне root logger
    root_logger = logging.getLogger()
    if root_logger.handlers or root_logger.level != logging.WARNING:
        # Логирование уже настроено приложением, не трогаем
        return
    
    # Настраиваем только если логирование не инициализировано
    # ВАЖНО: Библиотеки не должны настраивать root logger через basicConfig
    # Это должно делать только главное приложение
    if not logger.handlers and not root_logger.handlers:
        # Создаем хендлер только для нашего логгера модуля
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s - %(message)s"
        ))
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
