"""SQL Generation API подмодуль core (Phase 7 декомпозиция).

Реализация: sql_generation_plugin, code_formatter, _format_sql_legacy,
sql_safety_check, sql_explain.

Singletons передаются через keyword-only аргументы из фасада.
Внешние зависимости (call_openai_api, get_plugin) разрешаются через
импорт фасадного модуля (`custom_tools.text_to_sql.core`), чтобы тесты
могли подменять их через monkeypatch.
"""
import atexit
import concurrent.futures
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, Iterable, Optional, Tuple

from ..prompts import build_sql_safety_prompt
from ..utils import redact_text_to_sql_value

logger = logging.getLogger(__name__)


def _redact_sql_api_value(value):
    return redact_text_to_sql_value(value)


class SQLForbiddenStatementError(ValueError):
    """SQL содержит запрещённый keyword (DROP, INSERT, ...).

    W2-T7: ранее ``code_formatter`` возвращал ``formatted_sql_query`` с
    префиксом ``"-- ERROR: ..."`` и **самим исходным SQL** в теле, что давало
    утечку опасного запроса любому caller'у, который снимает префикс ``-- ``
    или парсит только первую строку. Теперь — fail-fast: исходник доступен в
    атрибуте ``sql_query`` (для логирования вызывающим), но НЕ помещается
    в любое поле, которое могло бы попасть в downstream-форматировщик.
    """

    def __init__(self, forbidden_keyword: str, sql_query: str) -> None:
        super().__init__(
            f"Forbidden SQL keyword '{forbidden_keyword}' detected"
        )
        self.forbidden_keyword = forbidden_keyword
        self.sql_query = sql_query


# === EPIC 7.4 / W1-T3: timeout + TTL-кеш + multi-worker pool для LLM safety audit ===
# W1-T3: убрали SPOF (max_workers=1). Один зависший LLM-вызов больше не парализует
# весь pipeline до TCP-таймаута: параллельные запросы получают свободный воркер.
# Floor=2 — даже при невалидной env-конфигурации никогда не возвращаемся к SPOF.
def _resolve_llm_safety_workers() -> int:
    raw = os.getenv("TEXT_TO_SQL_LLM_SAFETY_WORKERS", "4")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid TEXT_TO_SQL_LLM_SAFETY_WORKERS: {raw!r}"
        ) from exc
    return max(2, value)


_LLM_SAFETY_AUDIT_MAX_WORKERS = _resolve_llm_safety_workers()
_LLM_SAFETY_AUDIT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_LLM_SAFETY_AUDIT_MAX_WORKERS,
    thread_name_prefix="llm-safety-audit",
)
# Явный shutdown на atexit: cancel_futures отменяет ещё не стартовавшие задачи,
# wait=False — не висим на TCP-таймауте зависшего LLM-вызова. Поддерживается с 3.9.
atexit.register(
    _LLM_SAFETY_AUDIT_EXECUTOR.shutdown,
    wait=False,
    cancel_futures=True,
)


# W1-T3: порог перегрузки пула. Если в очереди ждут >= workers*2 задач (т.е.
# в дополнение к занятым воркерам ещё столько же висит в backlog) — считаем,
# что LLM-аудит залип и fail-closed'им новый запрос вместо тихого ожидания
# TCP-таймаута. Каждая задача и так ограничена _get_llm_safety_timeout_s, так
# что backlog растёт только при системной проблеме с LLM endpoint.
_LLM_SAFETY_QUEUE_OVERLOAD_FACTOR = 2
_LLM_SAFETY_CACHE: "OrderedDict[str, Tuple[float, Dict[str, object]]]" = OrderedDict()
_LLM_SAFETY_CACHE_LOCK = threading.RLock()

# M6: опциональный negative-TTL кэш для timeout-результатов.
# По умолчанию выключен: timeout не должен залипать после восстановления LLM.
# Включается только явным TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_S > 0.
_LLM_SAFETY_TIMEOUT_CACHE: "OrderedDict[str, float]" = OrderedDict()


# Дефолт negative-TTL вынесен в константу: используется и как env-default,
# и как fail-safe значение при некорректной конфигурации (см. ниже).
_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_DEFAULT_S = 0.0


def _get_llm_safety_timeout_negative_ttl_s() -> float:
    # Fail-safe: этот геттер вызывается из negative-TTL путей (cache_check вне
    # try-блока в sql_safety_check и cache_put внутри `except TimeoutError`).
    # ValueError отсюда вылетел бы МИМО `except (RuntimeError, ..., ValueError)`
    # (тот ловит только тело try) и сломал бы fail-closed. Поэтому при
    # некорректном env логируем warning и возвращаем дефолт, а не бросаем.
    raw = os.getenv(
        "TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_S",
        str(_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_DEFAULT_S),
    )
    try:
        value = float(raw)
        if value < 0:
            raise ValueError("must be non-negative")
    except (TypeError, ValueError):
        logger.warning(
            "Invalid TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_S=%r; "
            "using default %ss",
            raw,
            _LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_DEFAULT_S,
        )
        return _LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_DEFAULT_S
    return value


def _llm_safety_timeout_cache_check(key: str) -> bool:
    """Возвращает True если key находится в negative-TTL кэше (ещё не протух)."""
    neg_ttl = _get_llm_safety_timeout_negative_ttl_s()
    if neg_ttl == 0:
        return False
    now = time.time()
    with _LLM_SAFETY_CACHE_LOCK:
        ts = _LLM_SAFETY_TIMEOUT_CACHE.get(key)
        if ts is None:
            return False
        if now - ts > neg_ttl:
            _LLM_SAFETY_TIMEOUT_CACHE.pop(key, None)
            return False
        return True


def _llm_safety_timeout_cache_put(key: str) -> None:
    """Добавляет key в negative-TTL кэш с текущим временем."""
    neg_ttl = _get_llm_safety_timeout_negative_ttl_s()
    if neg_ttl == 0:
        return
    cap = _get_llm_safety_timeout_cache_max()
    now = time.time()
    with _LLM_SAFETY_CACHE_LOCK:
        _LLM_SAFETY_TIMEOUT_CACHE[key] = now
        _LLM_SAFETY_TIMEOUT_CACHE.move_to_end(key)
        while len(_LLM_SAFETY_TIMEOUT_CACHE) > cap:
            _LLM_SAFETY_TIMEOUT_CACHE.popitem(last=False)


def _get_llm_safety_timeout_s() -> float:
    raw = os.getenv("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S", "30")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S: {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S must be positive")
    return value


def _get_llm_safety_cache_ttl_s() -> float:
    raw = os.getenv("TEXT_TO_SQL_LLM_SAFETY_TTL_S", "300")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid TEXT_TO_SQL_LLM_SAFETY_TTL_S: {raw!r}"
        ) from exc
    if value < 0:
        raise ValueError("TEXT_TO_SQL_LLM_SAFETY_TTL_S must be non-negative")
    return value


def _get_llm_safety_cache_max() -> int:
    raw = os.getenv("TEXT_TO_SQL_LLM_SAFETY_CACHE_MAX", "512")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid TEXT_TO_SQL_LLM_SAFETY_CACHE_MAX: {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError("TEXT_TO_SQL_LLM_SAFETY_CACHE_MAX must be positive")
    return value


def _get_llm_safety_timeout_cache_max() -> int:
    raw = os.getenv("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_CACHE_MAX", "512")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_CACHE_MAX: {raw!r}"
        ) from exc
    if value <= 0:
        raise ValueError("TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_CACHE_MAX must be positive")
    return value


def _clear_llm_safety_cache() -> None:
    """Полная очистка TTL-кеша LLM safety (используется в тестах)."""
    with _LLM_SAFETY_CACHE_LOCK:
        _LLM_SAFETY_CACHE.clear()
        _LLM_SAFETY_TIMEOUT_CACHE.clear()


def _llm_safety_cache_key(sql_query: str, dsn: Optional[str] = None) -> str:
    from ..dialects import get_current_dialect_name
    from ..validators.safety_config import load_safety_profile

    dialect_name = get_current_dialect_name(dsn, strict=bool(dsn and str(dsn).strip()))
    profile = load_safety_profile()
    payload = json.dumps(
        {
            "dialect": dialect_name,
            "sql_query": sql_query,
            "safety": {
                "source_path": profile.source_path,
                "profile_name": profile.profile_name,
                "forbidden_keywords": profile.forbidden_keywords,
                "forbidden_functions": profile.forbidden_functions,
                "ast_forbidden_stmt_classes": profile.ast_forbidden_stmt_classes,
                "ast_forbidden_command_words": sorted(profile.ast_forbidden_command_words),
                "max_query_length": profile.max_query_length,
                "max_in_list_size": profile.max_in_list_size,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        default=list,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clone_cached_response(value: Dict[str, object]) -> Dict[str, object]:
    """Глубокая копия закешированного safety-ответа для изоляции от мутаций caller'ом.

    W8-T11: ранее clone делался двумя разными способами на двух разных сайтах
    (``copy.deepcopy`` в _llm_safety_cache_get, ``json.loads(json.dumps(...))``
    в _llm_safety_cache_put). Унифицируем на ``copy.deepcopy``: для чистых
    Python-структур (dict/list/str/bool/int/float) он быстрее JSON round-trip
    и не требует, чтобы значения были JSON-сериализуемы.

    safety_result имеет фиксированную форму (см. sql_safety_check) и содержит
    только Python-примитивы + вложенные dict/list — deepcopy безопасен.
    Caller, мутирующий результат (например, добавляющий issue в .issues), не
    мутирует cache-запись.
    """
    return copy.deepcopy(value)


def _llm_safety_cache_get(key: str) -> Optional[Dict[str, object]]:
    ttl = _get_llm_safety_cache_ttl_s()
    if ttl == 0:
        return None
    now = time.time()
    with _LLM_SAFETY_CACHE_LOCK:
        entry = _LLM_SAFETY_CACHE.get(key)
        if entry is None:
            return None
        timestamp, value = entry
        if now - timestamp > ttl:
            _LLM_SAFETY_CACHE.pop(key, None)
            return None
        _LLM_SAFETY_CACHE.move_to_end(key)
        return _clone_cached_response(value)


def _llm_safety_cache_put(key: str, value: Dict[str, object]) -> None:
    ttl = _get_llm_safety_cache_ttl_s()
    if ttl == 0:
        return
    # W8-T11: clone через единый helper. Раньше тут был json.loads(json.dumps(...))
    # — отдельный путь от _llm_safety_cache_get. Унифицируем на deepcopy:
    # быстрее на чистых Python-структурах и не требует JSON-сериализуемости.
    # safety_result строится валидатором (list[dict]/str/bool) — Python-only.
    cloned = _clone_cached_response(value)
    cap = _get_llm_safety_cache_max()
    now = time.time()
    with _LLM_SAFETY_CACHE_LOCK:
        # Чистим просроченные записи (lazy eviction).
        expired = [k for k, (ts, _) in _LLM_SAFETY_CACHE.items() if now - ts > ttl]
        for k in expired:
            _LLM_SAFETY_CACHE.pop(k, None)
        _LLM_SAFETY_CACHE[key] = (now, cloned)
        _LLM_SAFETY_CACHE.move_to_end(key)
        # FIFO cap: удаляем самые старые до достижения лимита.
        while len(_LLM_SAFETY_CACHE) > cap:
            _LLM_SAFETY_CACHE.popitem(last=False)


def _run_llm_safety_audit_with_timeout(
    sql_query: str, dsn: Optional[str] = None
) -> Dict[str, object]:
    """Запускает _run_llm_safety_audit с таймаутом (EPIC 7.4 / W1-T3).

    На таймауте бросает TimeoutError; вызывающий обязан явно пометить
    запрос как LLM_AUDIT_TIMEOUT/unsafe, не скрывая деградацию LLM-аудита.

    W1-T3: при перегрузке пула (queue >= max_workers*2) — RuntimeError
    "LLM safety pool overloaded" без silent skip.

    ВАЖНО: future.cancel() НЕ прерывает уже запущенный HTTP-вызов внутри
    call_openai_api (синхронный network I/O). Зависший worker может удерживать
    слот пула вплоть до TCP-таймаута уровня сокета. Поэтому необходим
    multi-worker pool (см. _LLM_SAFETY_AUDIT_MAX_WORKERS) — иначе один
    зависший вызов парализует весь pipeline до TCP-таймаута.
    """
    timeout_s = _get_llm_safety_timeout_s()

    # W1-T3: overload guard перед submit. ThreadPoolExecutor публичного qsize()
    # не имеет, читаем _work_queue.qsize() (CPython implementation detail, но
    # стабильно с Python 3.2+). При недоступности — пропускаем guard, не
    # подменяем функциональность silent fallback'ом.
    work_queue = getattr(_LLM_SAFETY_AUDIT_EXECUTOR, "_work_queue", None)
    if work_queue is not None:
        try:
            queue_size = work_queue.qsize()
        except (NotImplementedError, AttributeError):
            queue_size = None
        if queue_size is not None:
            overload_threshold = _LLM_SAFETY_AUDIT_MAX_WORKERS * _LLM_SAFETY_QUEUE_OVERLOAD_FACTOR
            if queue_size >= overload_threshold:
                raise RuntimeError(
                    f"LLM safety pool overloaded: queue_size={queue_size} "
                    f">= threshold={overload_threshold} "
                    f"(workers={_LLM_SAFETY_AUDIT_MAX_WORKERS})"
                )

    future = _LLM_SAFETY_AUDIT_EXECUTOR.submit(_run_llm_safety_audit, sql_query, dsn)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError as exc:
        # future.cancel() отменит задачу, ЕСЛИ она ещё не стартовала;
        # уже запущенный LLM-вызов прервать нельзя (call_openai_api —
        # синхронный network I/O), но cancel хотя бы освободит очередь от
        # pending-задач и пометит future как "не нужен". Зависший worker
        # держит слот пула до TCP-таймаута; W1-T3 расширил пул до
        # max_workers>=2, чтобы один зависший вызов не парализовал pipeline.
        future.cancel()
        raise TimeoutError(
            f"LLM safety audit exceeded timeout of {timeout_s}s"
        ) from exc


def _detect_forbidden_keyword(upper_sql: str, keywords: Iterable[str]) -> Optional[str]:
    """Возвращает первый найденный запрещённый keyword в `upper_sql` или None.

    Поддерживает multi-word keywords (например, "INSERT INTO"): токены
    разделяются произвольным whitespace (включая переводы строк). Все токены
    экранируются через `re.escape`, чтобы regex-метасимволы внутри keyword
    трактовались как литералы.
    """
    for kw in keywords:
        tokens = kw.split()
        if not tokens:
            continue
        if len(tokens) == 1:
            pattern = rf"\b{re.escape(tokens[0])}\b"
        else:
            pattern = r"\b" + r"\s+".join(re.escape(t) for t in tokens) + r"\b"
        if re.search(pattern, upper_sql):
            return kw
    return None


def _run_llm_safety_audit(sql_query: str, dsn: Optional[str] = None) -> Dict[str, object]:
    """Выполняет LLM-аудит SQL и возвращает распарсенный JSON-ответ.

    Любая ошибка (отсутствие call_openai_api, сетевой сбой, невалидный JSON,
    неверная структура ответа) пробрасывается наверх — без silent fallback.
    """
    from custom_tools.text_to_sql import core as _facade
    call_openai_api = _facade.call_openai_api
    if call_openai_api is None:
        raise RuntimeError("call_openai_api is not available for LLM safety audit")

    prompt = build_sql_safety_prompt(sql_query, dsn=dsn)
    resp = call_openai_api(
        prompt=prompt,
        system_prompt="Ты SQL-аудитор. Ищи уязвимости и узкие места. Верни только JSON.",
        max_tokens=2000,
        response_format={"type": "json_object"},
    )
    from ..utils import parse_llm_json_response
    obj = parse_llm_json_response(resp)
    if not isinstance(obj, dict):
        raise ValueError(f"LLM safety audit returned non-dict payload: {type(obj).__name__}")
    return obj


def sql_generation_plugin(
    context: str,
    user_query: str,
    dsn: Optional[str] = None,
    *,
    sql_generator,
) -> Dict[str, str]:
    """LLM-генерация SQL из linked_entities с безопасными ограничениями и валидацией схемы.

    EPIC 8.8: SQLGenerator передаётся как singleton-DI через kwarg, инстанс
    больше не создаётся на каждый вызов.

    Args:
        context: Контекст с привязанными к схеме сущностями
        user_query: Пользовательский запрос на естественном языке
        dsn: явный DSN для диалект-aware генерации литералов и quoting
        sql_generator: инжектированный SQLGenerator (singleton фасада)

    Returns:
        Словарь с сгенерированным SQL-запросом
    """
    from ..utils import get_runtime_context_dsn

    effective_dsn = (
        dsn if isinstance(dsn, str) and dsn.strip() else get_runtime_context_dsn()
    )
    if effective_dsn is None:
        raise ValueError(
            "sql_generation_plugin requires explicit dsn or workflow runtime metadata"
        )
    return sql_generator.generate_sql(context, user_query, dsn=effective_dsn)


def code_formatter(sql_query: str, *, sql_validator) -> Dict[str, str]:
    """Диалект-осознанное форматирование SQL.

    Args:
        sql_query: SQL-запрос для форматирования

    Returns:
        Словарь с результатом форматирования. Возможные формы:
          - ``{"formatted_sql_query": str}`` — успех (sqlglot или legacy).
          - ``{"error": str}`` — sqlglot включён, но парсинг завершился
            исключением; SQL-тело в ответ НЕ включается (W2-T7 fail-fast).
            Caller обязан проверять наличие ключа "error" и не передавать
            такой ответ downstream без явной обработки.
        Если обнаружен запрещённый keyword — поднимается
        :class:`SQLForbiddenStatementError` (не возврат, а исключение).

    Note:
        Контракт ``tool_definitions/code_formatter.yaml`` объявляет только
        ``formatted_sql_query`` как output-свойство. Ключ ``"error"`` является
        расширением runtime-контракта, не отражённым в yaml-определении инструмента;
        синхронизация yaml — в deferred (файл вне ownership задачи T7-sqlgen).
    """
    logger.info("Formatting SQL code")

    # БЕЗОПАСНОСТЬ: Проверяем на запрещенные команды ДО форматирования.
    # Маскируем строковые литералы, чтобы regex не срабатывал на тексте внутри
    # кавычек (например, "SELECT 'do not DROP table'"). Согласуется с поведением
    # SQLSafetyValidator.validate(), который также маскирует литералы перед regex.
    # Список forbidden_keywords читаем у инжектированного валидатора, а не
    # инстанцируем новый — это единственный source of truth для DI.
    masked_sql = sql_validator._mask_string_literals(sql_query)
    upper_sql = masked_sql.upper().strip()
    forbidden = _detect_forbidden_keyword(upper_sql, sql_validator.forbidden_keywords)
    if forbidden is not None:
        # W2-T7: fail-fast вместо возврата formatted_sql_query с исходником в
        # теле. Утечка опасного SQL через ``"-- ERROR: ...\n-- <SQL>"`` была
        # эксплуатируема caller'ом, который снимает префикс ``-- ``.
        logger.warning(
            "Forbidden SQL keyword '%s' detected; raising fail-fast",
            forbidden,
        )
        raise SQLForbiddenStatementError(forbidden, sql_query)

    # Используем sqlglot если доступен и включен.
    # EPIC 7.28: значение is_sqlglot_enabled() читаем ОДИН РАЗ на вызов
    # и используем во всех ветках (try-success/try-empty/except). Раньше
    # env мог переключиться между двумя вызовами и приводить к
    # несогласованной логике (return-error vs legacy-fallback).
    from ..dialects import is_sqlglot_enabled, get_sqlglot_dialect
    from ..validators import record_sqlglot_metric

    sqlglot_enabled = is_sqlglot_enabled()

    try:
        if sqlglot_enabled:
            from ..utils import parse_with_timeout

            record_sqlglot_metric("format_count")
            record_sqlglot_metric("parse_attempts")

            dialect = get_sqlglot_dialect()
            sqlglot_dialect = None if dialect == "ansi" else dialect
            # EPIC 7.27: парсим через parse_with_timeout — защита от длинных/злых SQL.
            statements = parse_with_timeout(sql_query.strip(), read=sqlglot_dialect)

            if statements:
                formatted_parts = []
                for stmt in statements:
                    formatted_sql = stmt.sql(dialect=sqlglot_dialect, pretty=True)
                    # Добавляем точку с запятой если отсутствует
                    if not formatted_sql.rstrip().endswith(";"):
                        formatted_sql += ";"
                    formatted_parts.append(formatted_sql)

                return {"formatted_sql_query": "\n\n".join(formatted_parts)}
            else:
                record_sqlglot_metric("parse_failures")
                # W2-T7-консистентность: пустой parse при включённом sqlglot —
                # это не «успех» и не должен молча уходить в legacy-форматтер
                # (иначе caller получит legacy-SQL без признака сбоя sqlglot).
                # Эта ветка достижима ТОЛЬКО при sqlglot_enabled=True (см. if выше),
                # поэтому отдельная проверка sqlglot_enabled здесь избыточна.
                # Возвращаем тот же структурированный {error}, что и exception-ветка ниже.
                logger.error(
                    "sqlglot formatting returned no statements (empty parse)"
                )
                return {"error": "sqlglot parse returned no statements"}

    except Exception as e:
        record_sqlglot_metric("parse_failures")
        safe_error = str(_redact_sql_api_value(e))
        # Используем cached sqlglot_enabled — не перечитываем env,
        # чтобы решение оставалось консистентным внутри одного вызова.
        if sqlglot_enabled:
            # W2-T7: не эхоируем SQL-тело в formatted_sql_query — caller
            # мог бы передать его downstream без проверки. Возвращаем только
            # структурированный error без SQL-тела.
            logger.error("sqlglot formatting failed: %s", safe_error)
            return {"error": safe_error}
        record_sqlglot_metric("fallback_count")
        logger.warning("sqlglot formatting failed in legacy mode, using legacy formatter: %s", safe_error)

    # Fallback на текущую реализацию
    return _format_sql_legacy(sql_query)


def _mask_literals_for_legacy(sql: str):
    """Маскирует строковые литералы в одинарных кавычках для fallback-форматтера.

    Возвращает (masked_sql, replacements), где replacements — список
    (placeholder, original) в порядке появления. Поддерживает только
    одинарные кавычки (ANSI-стиль, достаточно для fallback-форматтера).
    Escaped-quote внутри литерала (удвоение '') обрабатывается корректно.
    """
    masked = []
    replacements = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            start = i
            i += 1
            terminated = False
            while i < n:
                c = sql[i]
                if c == "'":
                    i += 1
                    # Удвоенная кавычка '' — escaped quote, не конец литерала
                    if i < n and sql[i] == "'":
                        i += 1
                        continue
                    terminated = True
                    break
                i += 1
            if not terminated:
                # Незакрытый строковый литерал = невалидный SQL. Это legacy
                # ANSI-fallback форматтер, поэтому продолжаем (маскируем до конца
                # строки), но НЕ молча: логируем, иначе деградация не видна.
                logger.warning(
                    "_mask_literals_for_legacy: незакрытый строковый литерал в SQL "
                    "(legacy ANSI-форматтер) — маскирую до конца строки"
                )
            original = sql[start:i]
            placeholder = f"__LIT_{len(replacements)}__"
            replacements.append((placeholder, original))
            masked.append(placeholder)
        else:
            masked.append(ch)
            i += 1
    return "".join(masked), replacements


def _format_sql_legacy(sql_query: str) -> Dict[str, str]:
    """Простое форматирование SQL без внешних зависимостей (fallback).

    Строковые литералы в одинарных кавычках маскируются перед трансформациями
    и восстанавливаются после — чтобы содержимое литералов не претерпевало
    uppercase/newline замен (исправление #10 MEDIUM).
    """
    q = sql_query.strip().rstrip(";") + ";"

    # Маскируем строковые литералы перед любыми трансформациями
    masked_q, replacements = _mask_literals_for_legacy(q)

    # Нормализуем пробелы и переводим ключевые слова в верхний регистр
    keywords = [
        "select", "from", "where", "group by", "order by", "join", "left join", "right join",
        "inner join", "outer join", "having", "limit", "and", "or", "on", "between"
    ]

    normalized = re.sub(r"\s+", " ", masked_q, flags=re.MULTILINE).strip()

    # Вставляем переводы строк
    for kw in [
        " select ", " from ", " where ", " group by ", " order by ", " having ",
        " limit ", " join ", " on ", " and ", " or ", " between ",
    ]:
        normalized = normalized.replace(kw, "\n" + kw.strip() + " ")

    # Верхний регистр ключевых слов
    for kw in sorted(keywords, key=len, reverse=True):
        normalized = re.sub(fr"\b{re.escape(kw)}\b", kw.upper(), normalized, flags=re.IGNORECASE)

    # Восстанавливаем оригинальные строковые литералы
    for placeholder, original in replacements:
        normalized = normalized.replace(placeholder, original)

    return {"formatted_sql_query": normalized}


def sql_safety_check(
    sql_query: str,
    *,
    sql_validator,
    dsn: Optional[str] = None,
) -> Dict[str, object]:
    """Orchestrator: статический слой + LLM-advisory (если static прошёл).

    EPIC 7.4: LLM-аудит выполняется с таймаутом (TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_S,
    default 30c) и кешируется по SHA256(sql_query) в течение TTL
    (TEXT_TO_SQL_LLM_SAFETY_TTL_S, default 300c). Кешируем только успешные
    результаты, чтобы не залипнуть в unsafe навсегда при временном сбое LLM.
    Таймаут НЕ даёт silent fallback: запрос помечается unsafe с явным
    issue_type=LLM_AUDIT_TIMEOUT.

    W9-A10: orchestrator явно разделяет два слоя:
      * static (``sql_validator.validate``) — regex + sqlglot AST, БЕЗ LLM.
      * llm_advisory (``_run_llm_safety_audit_with_timeout``) — non-blocking
        дополнение, запускается ТОЛЬКО если static вернул is_safe=True.
    Если static уже отклонил запрос — LLM не вызывается (экономия токенов и
    времени, плюс QA-слой не зависит от LLM-доступности).
    """
    logger.info("Performing SQL safety check")

    from ..utils import get_runtime_context_dsn

    runtime_dsn = get_runtime_context_dsn()
    effective_dsn = dsn if isinstance(dsn, str) else (runtime_dsn or "")

    # === STATIC LAYER ========================================================
    # Основная статическая проверка (regex + sqlglot AST). Слой автономен:
    # не зависит от LLM-доступности.
    safety_result = sql_validator.validate(sql_query, dsn=effective_dsn)
    safety_result.setdefault("issues", [])
    advisory_issues = safety_result.setdefault("advisory_issues", [])

    # Контракт-инвариант валидатора: validate() ОБЯЗАН вернуть dict с "is_safe".
    # Если ключа нет — это программная ошибка валидатора, а не runtime-сбой LLM.
    # Проверяем ДО try-блока LLM-аудита, чтобы не замаскировать баг под
    # LLM_AUDIT_FAILED (silent corruption -> ложный signal оператору).
    if "is_safe" not in safety_result:
        raise ValueError(
            f"sql_validator.validate must return 'is_safe' key; got: {list(safety_result)}"
        )

    # W9-A10: early-return при static is_safe=False — LLM-advisory не вызываем
    # для уже отклонённого запроса. Аргументы:
    #   1. QA-слой не должен зависеть от LLM (AGENTS.md).
    #   2. Нет смысла тратить токены/время LLM на запрос, который точно отклонён.
    #   3. Caller получает stable contract: safety_status=unsafe, llm_audit=skipped.
    # Кеш не наполняем — статические отказы и так дёшевы (regex + AST).
    if not safety_result.get("is_safe"):
        safety_result["safety_status"] = "unsafe"
        safety_result["llm_audit"] = "skipped_static_unsafe"
        return safety_result

    # === LLM ADVISORY LAYER (non-blocking) ===================================
    # EPIC 7.4: cache lookup выполняется только ПОСЛЕ static layer. Кеш
    # пропускает повторный LLM-аудит, но не regex/AST-валидацию: иначе старый
    # safe-ответ мог бы скрыть новый static-deny после reload конфигурации или
    # изменения валидатора.
    cache_key = _llm_safety_cache_key(sql_query, dsn=effective_dsn)

    # M6: сначала проверяем positive cache — если успешный аудит уже есть
    # (в том числе после восстановления LLM), используем его, минуя negative-TTL.
    cached = _llm_safety_cache_get(cache_key)
    if cached is not None:
        cached_advisory = cached.get("advisory_issues")
        if isinstance(cached_advisory, list):
            safety_result["advisory_issues"] = copy.deepcopy(cached_advisory)
        safety_result["llm_audit"] = cached.get("llm_audit", "ok")
        safety_result["safety_status"] = "safe"
        return safety_result

    # M6: только если positive cache пуст — проверяем negative-TTL кэш.
    # Если этот SQL уже таймаутил в течение последних
    # TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_NEGATIVE_TTL_S секунд (по умолчанию 60),
    # не порождаем новую LLM-задачу.
    if _llm_safety_timeout_cache_check(cache_key):
        safety_result["is_safe"] = False
        safety_result["safety_status"] = "failed"
        safety_result["llm_audit"] = "timeout"
        safety_result["llm_audit_error"] = "repeated timeout (negative-TTL cache hit)"
        safety_result.setdefault("issues", []).append({
            "issue_type": "LLM_AUDIT_TIMEOUT",
            "description": "LLM-based safety audit recently timed out; skipping retry.",
        })
        return safety_result

    # Дополнительный LLM-аудит: содержательные LLM-находки идут в advisory,
    # но runtime-сбой самого LLM-аудита остаётся fail-closed.
    # except перехватывает ТОЛЬКО реальные runtime-ошибки LLM-вызова, а не
    # программные баги (ValueError из контракт-проверок выше).
    try:
        llm_result = _run_llm_safety_audit_with_timeout(sql_query, dsn=effective_dsn)
        llm_issues = llm_result.get("issues") if isinstance(llm_result, dict) else None
        if isinstance(llm_issues, list):
            for item in llm_issues:
                if isinstance(item, dict) and item.get("issue_type") and item.get("description"):
                    advisory_issues.append({
                        "issue_type": f"LLM_{item['issue_type']}",
                        "description": item["description"],
                        "blocking": False,
                    })
        safety_result["llm_audit"] = "ok"
        safety_result["safety_status"] = "safe" if safety_result["is_safe"] else "unsafe"
        # EPIC 7.4: кешируем ТОЛЬКО успешный аудит (llm_audit=ok). Failed/timeout
        # не кешируем — иначе залипнем в unsafe при временной недоступности LLM.
        _llm_safety_cache_put(cache_key, safety_result)
    except TimeoutError as e:
        safe_error = str(_redact_sql_api_value(e))
        logger.error("LLM safety audit timed out: %s", safe_error)
        safety_result["is_safe"] = False
        safety_result["safety_status"] = "failed"
        safety_result["llm_audit"] = "timeout"
        safety_result["llm_audit_error"] = safe_error
        safety_result["issues"].append({
            "issue_type": "LLM_AUDIT_TIMEOUT",
            "description": f"LLM-based safety audit timed out: {safe_error}",
        })
        # M6: кешируем факт таймаута в negative-TTL кэш, чтобы повторные
        # запросы того же SQL не порождали новых LLM-задач во время деградации.
        # Defense-in-depth: ошибка наполнения кэша (например, невалидный
        # TEXT_TO_SQL_LLM_SAFETY_TIMEOUT_CACHE_MAX) не должна вылетать наружу —
        # safety_result уже помечен fail-closed (failed/timeout), кэш опционален.
        try:
            _llm_safety_timeout_cache_put(cache_key)
        except Exception as cache_exc:  # noqa: BLE001 — кэш best-effort
            logger.debug(
                "negative-TTL timeout cache put failed (ignored): %s", cache_exc
            )
    except (RuntimeError, json.JSONDecodeError, ValueError) as e:
        # Узкий список: реальные runtime-ошибки LLM-вызова.
        # - RuntimeError: call_openai_api недоступен, сетевой сбой.
        # - json.JSONDecodeError: невалидный JSON в LLM-ответе.
        # - ValueError: малформированный payload LLM (non-dict; см.
        #   `_run_llm_safety_audit` raise ValueError).
        # Программные баги (AttributeError, TypeError, KeyError) НЕ ловим —
        # они должны падать наружу и быть пойманы тестами/мониторингом.
        # Контракт-проверка `is_safe` вынесена ВЫШЕ try, чтобы её ValueError
        # тоже падал наружу как программный баг валидатора.
        safe_error = str(_redact_sql_api_value(e))
        logger.error("LLM safety audit failed: %s", safe_error)
        safety_result["is_safe"] = False
        safety_result["safety_status"] = "failed"
        safety_result["llm_audit"] = "failed"
        safety_result["llm_audit_error"] = safe_error
        safety_result["issues"].append({
            "issue_type": "LLM_AUDIT_FAILED",
            "description": f"LLM-based safety audit failed: {safe_error}",
        })

    return safety_result


def sql_explain(
    sql_query: str,
    dsn: Optional[str] = None,
    *,
    sql_validator,
) -> Dict[str, object]:
    """EXPLAIN/PLAN для разных СУБД.

    Args:
        sql_query: SQL-запрос для анализа плана выполнения
        dsn: явный DSN. Если None, env ``DB_DSN`` используется только при
            явном ``SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1`` opt-in. Без DSN план
            возвращается с issue ``EXPLAIN_ERROR``.

    Returns:
        Словарь с планом выполнения и метриками
    """
    logger.info("Explaining SQL query")

    from ..utils import get_runtime_context_dsn, is_dry_run_only, mask_dsn
    dry_run_only = is_dry_run_only()

    # W1-T1: explicit dsn > explicit env opt-in. Вычисляем DSN ДО safety:
    # dialect/safety layer умеет брать DB_DSN при dsn=None, а sql_explain не
    # должен даже статически/LLM-аудировать запрос в режиме silent env-fallback.
    effective_dsn = dsn if (isinstance(dsn, str) and dsn.strip()) else get_runtime_context_dsn()
    if effective_dsn is None:
        allow_env = os.getenv("SECURE_DB_EXECUTOR_ALLOW_ENV_DSN", "0") == "1"
        env_dsn = os.getenv("DB_DSN")
        if allow_env and env_dsn and env_dsn.strip():
            logger.warning(
                "sql_explain: dsn parameter MISSING; using DB_DSN env "
                "(SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1 opt-in)"
            )
            effective_dsn = env_dsn

    if not dry_run_only and not effective_dsn:
        return {
            "plan": None,
            "estimated_cost": None,
            "rows_to_scan": None,
            "issues": [{
                "issue_type": "EXPLAIN_ERROR",
                "description": (
                    "DSN required: pass dsn via parameter. Silent DB_DSN env "
                    "fallback disabled — set SECURE_DB_EXECUTOR_ALLOW_ENV_DSN=1 "
                    "to opt-in (not recommended)."
                ),
            }],
        }

    # Сначала проверяем безопасность — через фасад, чтобы тесты могли
    # monkeypatch'ить `core.sql_safety_check` (контракт идентичен _db_exec.py).
    # Для dry-run без DSN передаём пустую строку, а не None: иначе dialect layer
    # может использовать DB_DSN как legacy fallback.
    from custom_tools.text_to_sql import core as _facade
    safety = _facade.sql_safety_check(sql_query, dsn=effective_dsn or "")
    if not isinstance(safety, dict):
        # Fail-closed: structure-failure не приравниваем к safe.
        return {
            "plan": None,
            "estimated_cost": None,
            "rows_to_scan": None,
            "issues": [{
                "issue_type": "SAFETY_RESULT_INVALID",
                "description": f"sql_safety_check returned {type(safety).__name__}, expected dict",
            }],
        }
    if not safety.get("is_safe", False):
        safety_issues = safety.get("issues") or []
        unsafe_result: Dict[str, object] = {
            "plan": None,
            "estimated_cost": None,
            "rows_to_scan": None,
            "issues": [*safety_issues, {"issue_type": "UNSAFE", "description": "Query failed safety check."}],
        }
        if dry_run_only:
            unsafe_result["dry_run_only"] = True
            unsafe_result["skipped_execution"] = True
            unsafe_result["sql_query"] = sql_query
        return unsafe_result
    if dry_run_only:
        return {
            "plan": None,
            "estimated_cost": None,
            "rows_to_scan": None,
            "issues": [],
            "dry_run_only": True,
            "skipped_execution": True,
            "sql_query": sql_query,
        }

    try:
        # Обращаемся к get_plugin через фасадный модуль (для monkeypatch).
        # _facade уже импортирован выше (для sql_safety_check) — переиспользуем.
        get_plugin = _facade.get_plugin

        plugin = get_plugin(effective_dsn)
        conn = plugin.connect(effective_dsn)
        try:
            return plugin.explain(conn, sql_query)
        finally:
            plugin.close(conn)
    except Exception as e:
        # Маскируем DSN/пароль в сообщении об ошибке — DSN не должен попадать
        # в логи/трассы/AG-UI envelope (см. EPIC 7.24, переиспользует mask_dsn
        # из EPIC 7.3 — общий helper для текстов с DSN-литералами).
        error_message = mask_dsn(str(e))
        error_result: Dict[str, object] = {
            "plan": None,
            "estimated_cost": None,
            "rows_to_scan": None,
            "issues": [{"issue_type": "EXPLAIN_ERROR", "description": error_message}],
        }
        if dry_run_only:
            error_result["dry_run_only"] = True
            error_result["skipped_execution"] = True
            error_result["sql_query"] = sql_query
        return error_result
