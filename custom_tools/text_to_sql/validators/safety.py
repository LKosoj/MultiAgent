"""
SQL Safety валидатор и метрики sqlglot.

ВНУТРЕННЯЯ СТРУКТУРА (декомпозиция god-модуля):
- ``_LiteralMasker`` — stateless маскировка строковых литералов и
  quoted-идентификаторов.
- ``_RegexValidator`` — regex-проверки (forbidden keywords/functions,
  IN-список в legacy-режиме, EXPLAIN ANALYZE, комментарии).
- ``_SqlglotValidator`` — AST-проверки через sqlglot (структура стейтмента,
  EXPLAIN ANALYZE на AST-уровне, IN-списки на AST, корневой select/CTE).
- ``SQLSafetyValidator`` — публичный фасад. Сохраняет ВЕСЬ исторический
  API (включая ``_mask_string_literals``/``_check_in_lists`` и пр.,
  на которые опираются внешние модули и тесты). Внутри делегирует в
  helper-классы.

Публичный контракт сохранён: импорт ``from ... import SQLSafetyValidator,
get_sqlglot_metrics, reset_sqlglot_metrics, record_sqlglot_metric,
SQLGLOT_AVAILABLE`` продолжает работать. См.
``tests/test_validators_public_api_preserved.py``.
"""
import concurrent.futures
import os
import re
import logging
import threading
from typing import List, Dict, Any, FrozenSet, Iterable, Tuple

from .safety_config import load_safety_profile, reload_safety_config

logger = logging.getLogger(__name__)

# Опциональный импорт sqlglot
try:
    import sqlglot
    from sqlglot import expressions as exp
    from sqlglot.errors import TokenError as _SqlglotTokenError
    SQLGLOT_AVAILABLE = True
except ImportError:
    SQLGLOT_AVAILABLE = False
    sqlglot = None
    exp = None
    _SqlglotTokenError = Exception  # noqa: N816

from ..dialects import (
    double_quote_is_string,
    get_current_dialect_name,
    get_sqlglot_dialect,
    is_sqlglot_enabled,
)

# Простые метрики для мониторинга. Мутации защищены _SQLGLOT_METRICS_LOCK,
# чтобы конкурентные validate(...) не теряли инкременты счётчиков
# (EPIC 2.7).
_SQLGLOT_METRICS = {
    "parse_attempts": 0,
    "parse_failures": 0,
    "fallback_count": 0,
    "validation_count": 0,
    "format_count": 0
}
_SQLGLOT_METRICS_LOCK = threading.Lock()


def _redact_safety_value(value: Any) -> Any:
    try:
        from backend.fastapi_app.agui.redaction import _redact_payload, redact_pii_in_payload

        if isinstance(value, BaseException):
            value = str(value)
        return redact_pii_in_payload(_redact_payload(value))
    except Exception as e:
        logger.warning("_redact_safety_value: redaction import failed: %s", e)
        return "<redacted>"


def get_sqlglot_metrics() -> dict:
    """Возвращает снэпшот текущих метрик sqlglot."""
    with _SQLGLOT_METRICS_LOCK:
        return _SQLGLOT_METRICS.copy()


def reset_sqlglot_metrics() -> None:
    """Сбрасывает метрики sqlglot."""
    with _SQLGLOT_METRICS_LOCK:
        _SQLGLOT_METRICS.clear()
        _SQLGLOT_METRICS.update({
            "parse_attempts": 0,
            "parse_failures": 0,
            "fallback_count": 0,
            "validation_count": 0,
            "format_count": 0,
        })


def record_sqlglot_metric(key: str, delta: int = 1) -> None:
    """Потокобезопасно инкрементирует метрику sqlglot.

    Используется как `safety`-маршрутом (внутри валидатора), так и внешними
    модулями (`core/_sql_generation_api.py`, `validators/schema_aware.py`),
    чтобы все мутации проходили через единый lock — иначе counters теряются
    при конкурентных вызовах.
    """
    with _SQLGLOT_METRICS_LOCK:
        _SQLGLOT_METRICS[key] = _SQLGLOT_METRICS.get(key, 0) + delta


class _ParseTimeoutError(Exception):
    """sqlglot.parse exceeded SQL_VALIDATE_PARSE_TIMEOUT_SEC wall-time budget."""


def _set_operation_classes() -> tuple:
    """Возвращает tuple sqlglot-классов для set-операций (UNION/INTERSECT/EXCEPT).

    В sqlglot 27.x ``exp.Intersect`` и ``exp.Except`` НЕ наследуются от
    ``exp.Union`` — у них общий родитель ``SetOperation``. Поэтому одной
    проверки ``isinstance(stmt, exp.Union)`` недостаточно. Этот хелпер
    переиспользуется в ``validate_statement_ast`` и ``is_valid_select_or_cte``,
    чтобы любая будущая правка списка set-операций жила в одном месте.
    """
    if exp is None:
        return ()
    return tuple(
        cls for cls in (
            getattr(exp, name, None) for name in ("Union", "Intersect", "Except")
        ) if cls is not None
    )


def _parse_with_timeout(sql_query: str, dialect: str | None, timeout_sec: float):
    """Вызывает ``sqlglot.parse`` с реальным wall-time таймаутом.

    ``ThreadPoolExecutor`` НЕЛЬЗЯ создавать через ``with``: его ``__exit__``
    делает ``shutdown(wait=True)`` и блокирует возврат, пока pathological
    парсинг не закончится — таймаут срабатывал бы только в логе. Создаём
    executor вручную и при таймауте вызываем ``shutdown(wait=False,
    cancel_futures=True)`` (Python 3.9+), чтобы текущий поток не блокировался
    на висящем ``sqlglot.parse``.

    Поднимает ``_ParseTimeoutError`` при превышении таймаута; пробрасывает
    любые иные исключения парсера как есть.

    Trade-off: executor создаётся на каждый вызов вместо module-level
    singleton. Per-call вариант стоит дороже (создание/teardown потока), но
    надёжно изолирует pathological parse: shared executor с
    ``max_workers=1`` мог бы быть забит зависшим parse'ом от предыдущего
    запроса, что заблокировало бы ВСЕ последующие validate() пока
    pathological поток не доработает (cancel_futures отменяет только
    pending, но не running task для pure-Python). Под высокий rps
    оправдано вынести в pool с ``max_workers>1`` и lifetime metrics, но
    это требует отдельной задачи по нагрузочному тестированию.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(sqlglot.parse, sql_query, read=dialect)
    try:
        try:
            return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError as e:
            raise _ParseTimeoutError(
                f"sqlglot.parse exceeded {timeout_sec}s timeout"
            ) from e
    finally:
        # cancel_futures отменяет ещё не стартовавшие задачи; уже запущенный
        # parse прерывистым не сделать (sqlglot — pure-python, без I/O),
        # но executor сам по себе не блокирует caller.
        executor.shutdown(wait=False, cancel_futures=True)


# =====================================================================
# LITERAL MASKING  (internal helper, decomposition candidate)
# =====================================================================
class _LiteralMasker:
    """Stateless маскировка строковых литералов и quoted-идентификаторов.

    Не хранит state — все нужные параметры передаются аргументами.
    """

    @staticmethod
    def mask_string_literals(sql_query: str, dialect: str | None = None) -> str:
        """Маскирует строковые литералы, сохраняя длину исходной строки.

        Поддерживаемые формы:
        - Одинарные кавычки с удвоением: 'a''b'
        - Двойные кавычки в MySQL (строковый литерал): "a""b"
        - Backslash-escape внутри одинарных кавычек: 'a\\'b' (MySQL без NO_BACKSLASH_ESCAPES)
        - PostgreSQL E-strings: E'\\n', e'\\t'
        - PostgreSQL dollar-quoted strings: $$...$$, $tag$...$tag$

        Все литералы заменяются на пробелами той же длины, чтобы сохранить
        офсеты для последующих regex-проверок.

        Двойные кавычки в стандартных диалектах (Postgres/SQLite/DuckDB/ANSI и пр.)
        — это quoted identifier, а не строка, поэтому их содержимое НЕ маскируется,
        чтобы pre-parse regex видел подозрительные слова внутри identifier
        (например ``"DROP"``). Эскейп удвоенной кавычки ``""`` всё равно корректно
        пропускается, чтобы сохранить офсеты.
        """
        dq_is_string = double_quote_is_string(dialect)
        result = []
        i = 0
        n = len(sql_query)
        while i < n:
            ch = sql_query[i]

            # Dollar-quoted: $tag$...$tag$ или $$...$$
            if ch == "$":
                tag_match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql_query[i:])
                if tag_match:
                    tag = tag_match.group(0)  # включая обрамляющие $
                    start = i
                    i += len(tag)
                    end_idx = sql_query.find(tag, i)
                    if end_idx == -1:
                        # Незакрытый dollar-quoted — маскируем до конца
                        result.append(" " * (n - start))
                        i = n
                    else:
                        # Маскируем весь блок включая закрывающий тэг
                        literal_len = end_idx + len(tag) - start
                        result.append(" " * literal_len)
                        i = end_idx + len(tag)
                    continue

            # E-strings: E'...' или e'...' с backslash-escape
            if ch in ("E", "e") and i + 1 < n and sql_query[i + 1] == "'":
                start = i
                i += 2  # пропускаем E'
                while i < n:
                    c = sql_query[i]
                    if c == "\\" and i + 1 < n:
                        # Backslash экранирует следующий символ
                        i += 2
                        continue
                    if c == "'":
                        if i + 1 < n and sql_query[i + 1] == "'":
                            # Удвоенная кавычка
                            i += 2
                            continue
                        # Закрывающая кавычка
                        i += 1
                        break
                    i += 1
                result.append(" " * (i - start))
                continue

            # Обычные одинарные кавычки с учётом backslash-escape и удвоения
            if ch == "'":
                start = i
                i += 1
                while i < n:
                    c = sql_query[i]
                    if c == "\\" and i + 1 < n:
                        # Backslash экранирует следующий символ (MySQL стиль)
                        i += 2
                        continue
                    if c == "'":
                        if i + 1 < n and sql_query[i + 1] == "'":
                            i += 2
                            continue
                        i += 1
                        break
                    i += 1
                result.append(" " * (i - start))
                continue

            # Двойные кавычки.
            # В MySQL "..." — это строковый литерал (маскируем), в остальных
            # стандартных диалектах — quoted identifier (НЕ маскируем содержимое,
            # но корректно пропускаем эскейп "" чтобы не сломать офсеты).
            if ch == '"':
                if dq_is_string:
                    start = i
                    i += 1
                    while i < n:
                        c = sql_query[i]
                        if c == '"':
                            if i + 1 < n and sql_query[i + 1] == '"':
                                i += 2
                                continue
                            i += 1
                            break
                        i += 1
                    result.append(" " * (i - start))
                    continue
                else:
                    # Идентификатор: оставляем содержимое как есть, только
                    # корректно учитываем удвоение "" внутри.
                    result.append(ch)
                    i += 1
                    while i < n:
                        c = sql_query[i]
                        if c == '"':
                            if i + 1 < n and sql_query[i + 1] == '"':
                                # эскейпнутая кавычка: эмитим оба символа и идём дальше
                                result.append('""')
                                i += 2
                                continue
                            # закрывающая кавычка
                            result.append(c)
                            i += 1
                            break
                        result.append(c)
                        i += 1
                    continue

            result.append(ch)
            i += 1

        return "".join(result)

    @staticmethod
    def mask_identifiers_via_lex(
        sql_query: str,
        dialect: str | None,
        forbidden_keywords: Iterable[str],
    ) -> str:
        """Маскирует quoted-идентификаторы пробелами через диалект-aware лексер sqlglot.

        Запрос предполагается уже без строковых литералов (после
        :meth:`mask_string_literals`). Назначение — убрать ложные срабатывания
        FORBIDDEN_KEYWORDS regex на словах внутри идентификаторов (например
        ``"merge"`` в Postgres, `` `union_col` `` в MySQL, ``AS column_with_union``).

        Замаскированы:
          * ``TokenType.IDENTIFIER`` — всегда quoted (``"a"`` / `` `a` ``).
          * ``TokenType.VAR`` — голые имена/алиасы; для них не имеет смысла,
            если это keyword (``select``/``from``), но безопасно нейтрализуем,
            чтобы пользовательские алиасы вида ``column_with_union`` не ловились.

        Офсеты сохраняются: символы заменяются пробелами в исходных позициях.

        Поведение при ошибке лексера (``_SqlglotTokenError``): функция логирует
        warning и возвращает ИСХОДНУЮ строку без маскировки идентификаторов.
        Это намеренный fail-graceful: вызывающий код (regex-проверки
        FORBIDDEN_KEYWORDS) продолжит работу по сырому SQL, а финальный
        ``sqlglot.parse`` в ``_validate_with_sqlglot`` всё равно поднимет
        SQL_PARSE_ERROR на действительно невалидном входе. Без такого
        поведения единичная ошибка лексера полностью блокировала бы
        regex-слой защиты.
        """
        if sqlglot is None:
            # USE_SQLGLOT=0 в legacy не вызывает эту функцию. Здесь это
            # означает, что sqlglot недоступен — отдаём исходную строку
            # как есть, чтобы regex по крайней мере работал.
            return sql_query
        from sqlglot.dialects.dialect import Dialect
        from sqlglot.tokens import TokenType

        dialect_key = (dialect or "").lower() or None
        # Маппинг внутреннего имени диалекта на sqlglot-диалект — используем
        # тот же словарь, что и для парсера, чтобы lex/parse шли в синке.
        from ..dialects import SQLGLOT_DIALECT_MAPPING

        sqlglot_dialect = SQLGLOT_DIALECT_MAPPING.get(dialect_key, "ansi")
        # У sqlglot нет диалекта 'ansi' — используем дефолтный токенайзер.
        get_or_raise_arg = "" if sqlglot_dialect == "ansi" else sqlglot_dialect
        tokenizer_cls = Dialect.get_or_raise(get_or_raise_arg).tokenizer_class
        try:
            tokens = tokenizer_cls().tokenize(sql_query)
        except _SqlglotTokenError as e:
            # При ошибке лексера не маскируем — regex-проверка по сырому SQL
            # остаётся активной (вызывающий код продолжит работу).
            logger.warning(
                "sqlglot tokenize failed in mask_identifiers_via_lex: %s; sql=%r",
                _redact_safety_value(e),
                _redact_safety_value(sql_query[:200]),
            )
            return sql_query

        # Часть запрещённых ключевых слов sqlglot не имеет в keyword-словаре
        # (например ATTACH/DETACH/REVOKE/EXEC у дефолтного токенайзера) и
        # возвращает их как TokenType.VAR. Их маскировать нельзя — иначе
        # pre-parse FORBIDDEN_KEYWORDS regex их не увидит. Поэтому VAR-токены,
        # текст которых в верхнем регистре совпадает с forbidden_keyword,
        # оставляем как есть.
        forbidden_upper = {kw.upper() for kw in forbidden_keywords}

        buf = list(sql_query)
        n = len(sql_query)
        for tok in tokens:
            if tok.token_type not in (TokenType.IDENTIFIER, TokenType.VAR):
                continue
            if (
                tok.token_type == TokenType.VAR
                and (tok.text or "").upper() in forbidden_upper
            ):
                continue
            # tok.start/tok.end в sqlglot — индексы первого и ПОСЛЕДНЕГО
            # символа токена в исходной строке (включительно).
            start = max(0, int(tok.start))
            end = min(n - 1, int(tok.end))
            for idx in range(start, end + 1):
                buf[idx] = " "
        return "".join(buf)


# =====================================================================
# REGEX VALIDATION  (internal helper, decomposition candidate)
# =====================================================================
class _RegexValidator:
    """Regex-based проверки безопасности.

    Хранит конфиг (forbidden_keywords/functions, max_in_list_size) и применяет
    его к masked SQL. Не вызывает sqlglot напрямую (за исключением
    ``contains_comments``, где AST используется как опциональный быстрый путь).
    """

    def __init__(
        self,
        forbidden_keywords: Iterable[str],
        forbidden_functions: Iterable[str],
        max_in_list_size: int,
    ):
        # Нормализуем входы в tuple: позволяет фасаду передавать list/frozenset/...
        # без рассогласования сигнатур (см. фикс review-нота про List vs frozenset).
        self.forbidden_keywords: Tuple[str, ...] = tuple(forbidden_keywords)
        self.forbidden_functions: Tuple[str, ...] = tuple(forbidden_functions)
        self.max_in_list_size = max_in_list_size

    @staticmethod
    def has_explain_analyze(upper_sql: str) -> bool:
        """Детектор EXPLAIN ANALYZE для regex-форм: prefix и PG-скобочной.

        Покрывает:
        - EXPLAIN ANALYZE SELECT ...
        - EXPLAIN (ANALYZE) SELECT ...
        - EXPLAIN (ANALYZE, BUFFERS) SELECT ...
        - EXPLAIN (FORMAT JSON, ANALYZE TRUE) SELECT ...
        """
        return bool(
            re.match(
                r"^\s*EXPLAIN\s*(?:\(.*?\bANALYZE\b.*?\)|ANALYZE\b)",
                upper_sql,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )

    def check_forbidden_keywords(
        self, upper_sql: str, issues: List[Dict[str, Any]]
    ) -> bool:
        """Проверяет forbidden_keywords. Возвращает True, если хоть один найден.

        EPIC 2.8: накапливаем все запрещённые ключевые слова, без early break.
        """
        found = False
        for forbidden_keyword in self.forbidden_keywords:
            if re.search(fr"\b{forbidden_keyword}\b", upper_sql):
                issues.append({
                    "issue_type": "FORBIDDEN_STATEMENT",
                    "description": f"Forbidden SQL keyword '{forbidden_keyword}' detected."
                })
                found = True
        return found

    def check_forbidden_functions(
        self, upper_sql: str, issues: List[Dict[str, Any]]
    ) -> bool:
        """Проверяет forbidden_functions (multi-word patterns). Возвращает True, если найдено."""
        found = False
        for forbidden_function in self.forbidden_functions:
            # multi-word конструкции (например "into outfile") матчим как
            # последовательность слов с произвольным whitespace между ними.
            tokens = forbidden_function.strip().upper().split()
            if not tokens:
                continue
            pattern = r"\b" + r"\s+".join(re.escape(t) for t in tokens) + r"\b"
            if re.search(pattern, upper_sql):
                issues.append({
                    "issue_type": "FORBIDDEN_FUNCTION",
                    "description": (
                        f"Forbidden SQL function/construct '{forbidden_function}' detected."
                    ),
                })
                found = True
        return found

    def check_in_lists(self, masked_query: str, issues: List[Dict[str, Any]]) -> None:
        """Проверяет размер IN-списков regex-эвристикой (legacy USE_SQLGLOT=0).

        ВНИМАНИЕ: legacy regex-режим небезопасен с nested parens внутри IN
        (например ``IN ((SELECT ...))`` или ``IN (func(a, b), c)``): regex
        ``([^)]*)`` обрывается на первой закрывающей скобке. Этот путь
        должен быть удалён в будущем; в production он по умолчанию заблокирован
        в :meth:`SQLSafetyValidator.validate` (см. SQL_SAFETY_ALLOW_LEGACY).

        Используется только в legacy-пути; sqlglot-маршрут считает IN-списки
        через AST в :meth:`_SqlglotValidator.validate_statement_ast` (EPIC 2.6).

        EPIC 2.8: накапливаем все нарушения IN-list, без early break.
        Fail-fast: ошибки regex не глушим — логируем warning и пробрасываем
        (нет silent fallback).
        """
        try:
            matches = list(
                re.finditer(
                    r"\bIN\s*\(([^)]*)\)",
                    masked_query,
                    flags=re.IGNORECASE | re.DOTALL,
                )
            )
        except re.error as e:
            logger.warning(
                "Legacy IN-list regex failed on masked query: %s", e
            )
            raise

        for m in matches:
            content = m.group(1)
            if re.search(r"\bSELECT\b", content, flags=re.IGNORECASE):
                continue
            # Грубое разбиение по запятым (legacy-фолбэк без AST).
            items = [x for x in [p.strip() for p in content.split(",")] if x]
            if len(items) > self.max_in_list_size:
                issues.append({
                    "issue_type": "IN_LIST_TOO_LARGE",
                    "description": f"IN list has {len(items)} items (>{self.max_in_list_size})."
                })

    @staticmethod
    def contains_comments(
        masked_query: str,
        original_query: str | None = None,
        dialect: str | None = None,
    ) -> bool:
        """Детектор SQL-комментариев.

        Основной путь (sqlglot доступен): диалект-aware Tokenizer возвращает
        для каждого токена список ``comments`` — наличие хотя бы одного
        указывает на наличие комментария в исходном SQL. Используем
        ``original_query`` если передан, иначе ``masked_query``: токенайзер
        должен видеть оригинальный текст (после маскировки строк, но не
        после lex-маскировки идентификаторов).

        Fallback (USE_SQLGLOT=0 или sqlglot недоступен): word-boundary regex.
        ``r'(^|\\s|;|\\))--'`` — чтобы выражения вида ``-2 - -1`` не
        считались комментарием (там после ``-`` идёт число, а перед — пробел и
        ``-``, но не word-граница), плюс ``r'/\\*'`` для блочных.
        Fail-fast: ``try/except: pass`` запрещён.
        """
        # AST-путь
        if sqlglot is not None:
            from sqlglot.dialects.dialect import Dialect
            from sqlglot.tokens import TokenType  # noqa: F401  (kept for parity)
            from ..dialects import SQLGLOT_DIALECT_MAPPING

            dialect_key = (dialect or "").lower() or None
            sqlglot_dialect = SQLGLOT_DIALECT_MAPPING.get(dialect_key, "ansi")
            get_or_raise_arg = "" if sqlglot_dialect == "ansi" else sqlglot_dialect
            tokenizer_cls = Dialect.get_or_raise(get_or_raise_arg).tokenizer_class

            source = original_query if original_query is not None else masked_query
            try:
                tokens = tokenizer_cls().tokenize(source)
            except _SqlglotTokenError as e:
                # Лексер не разобрал SQL — наличие комментариев не подтверждено,
                # но и не опровергнуто. Возвращаем True: дальнейший AST-парсинг
                # всё равно поднимет SQL_PARSE_ERROR, а consumer должен отвергнуть
                # запрос как невалидный.
                logger.warning(
                    "sqlglot tokenize failed in contains_comments: %s; sql=%r",
                    e,
                    source[:200],
                )
                return True
            for tok in tokens:
                if getattr(tok, "comments", None):
                    return True
            # На случай trailing-комментариев в конце запроса, не привязанных
            # к токену, дополнительно проверим word-boundary regex по masked.
            # (sqlglot обычно их всё равно прикрепляет, но подстрахуемся.)
            if re.search(r'(^|\s|;|\))--', masked_query):
                return True
            if re.search(r'/\*', masked_query):
                return True
            return False

        # Legacy fallback (USE_SQLGLOT=0 и sqlglot отсутствует одновременно
        # маловероятно, но явный путь предусмотрим).
        if re.search(r'(^|\s|;|\))--', masked_query):
            return True
        if re.search(r'/\*', masked_query):
            return True
        return False


# =====================================================================
# SQLGLOT / AST VALIDATION  (internal helper, decomposition candidate)
# =====================================================================
class _SqlglotValidator:
    """AST-проверки через sqlglot: structure, EXPLAIN ANALYZE, IN-list, root SELECT/CTE.

    Использует ``_RegexValidator.has_explain_analyze`` как fallback на regex.
    """

    def __init__(
        self,
        ast_forbidden_stmt_classes: Iterable[str],
        ast_forbidden_command_words: Iterable[str],
        max_in_list_size: int,
        forbidden_keywords: Iterable[str],
        forbidden_functions: Iterable[str] = (),
    ):
        # Нормализуем: stmt_classes итерируется (tuple для стабильного порядка),
        # command_words проверяется по `in` — frozenset даёт O(1).
        self.ast_forbidden_stmt_classes: Tuple[str, ...] = tuple(ast_forbidden_stmt_classes)
        self.ast_forbidden_command_words: FrozenSet[str] = frozenset(ast_forbidden_command_words)
        self.max_in_list_size = max_in_list_size
        # W7-T4/T5: legacy-путь `is_valid_select_or_cte` (regex fallback при
        # USE_SQLGLOT=0 или ImportError sqlglot) использует forbidden_keywords,
        # чтобы блокировать DML/DDL внутри `WITH ... <statement>`. Раньше
        # список был захардкожен прямо в методе — нарушение AGENTS.md (хардкод
        # бизнес-логики в QA-слое). Теперь читаем из safety.yaml-профиля.
        # safety.yaml хранит ключевые слова в верхнем регистре (см.
        # config/text_to_sql/safety.yaml profiles.*.forbidden_keywords),
        # что совпадает с форматом, ожидаемым regex `\b{kw}\b` по masked_upper.
        # Принудительный upper() сохраняет инвариант, даже если профиль будет
        # задан в смешанном регистре в кастомном конфиге.
        self.forbidden_keywords: FrozenSet[str] = frozenset(
            kw.upper() for kw in forbidden_keywords
        )
        # Фикс #1/#9: AST-проверка запрещённых функций. Нормализуем в lower —
        # имена функций в sqlglot AST отдаются в нижнем регистре (Anonymous.name).
        self.forbidden_functions: FrozenSet[str] = frozenset(
            fn.lower() for fn in forbidden_functions
        )

    @staticmethod
    def ast_has_explain_analyze(stmt) -> bool:
        """AST-уровневая проверка EXPLAIN ANALYZE.

        sqlglot обычно представляет EXPLAIN как exp.Command с name='EXPLAIN'
        и опциями в expression/args. Если AST не даёт прямого сигнала —
        возвращаем False, fallback на regex (вызывающая сторона уже
        использует _RegexValidator.has_explain_analyze)."""
        if not isinstance(stmt, exp.Command):
            return False
        try:
            name = (getattr(stmt, "name", "") or "").upper()
        except Exception:
            name = ""
        if name != "EXPLAIN":
            return False
        # Берём «полезную нагрузку» команды и ищем ANALYZE в верхнем регистре.
        try:
            expression = stmt.args.get("expression") if hasattr(stmt, "args") else None
        except Exception:
            expression = None
        payload = str(expression) if expression is not None else str(stmt)
        return bool(
            re.search(
                r"^\s*\(.*?\bANALYZE\b.*?\)|^\s*ANALYZE\b",
                payload.upper(),
                flags=re.IGNORECASE | re.DOTALL,
            )
        )

    def check_forbidden_functions_ast(
        self, stmt, issues: List[Dict[str, Any]]
    ) -> bool:
        """AST-обход запрещённых функций — фикс #1/#9.

        Проверяет stmt на наличие:
        1. exp.Dot с child Anonymous (system.shutdown, system.kill) — «qualifier.func».
        2. exp.Anonymous (pg_sleep, dblink, url, s3, load_file и пр.) — .name.lower().
        3. exp.Table с .db (information_schema, pg_catalog, mysql.user) — схема и
           составное имя «schema.table».

        Возвращает True, если найдено хотя бы одно нарушение.

        Этот метод — независимая (вторая) линия защиты от forbidden_functions:
        работает по AST, не зависит от regex или маскировки идентификаторов,
        поэтому правильно работает при USE_SQLGLOT=1 (прод-режим).
        """
        if not self.forbidden_functions:
            return False
        if exp is None:
            return False

        found = False

        # 1. exp.Dot с child-Anonymous: «qualifier.func()» → «qualifier.func»
        # Обходим ПЕРВЫМ, чтобы не дублировать нарушение с шагом 2 (Anonymous).
        dot_anonymous_ids: set = set()
        for dot in stmt.find_all(exp.Dot):
            qualifier = getattr(dot.this, "name", "") or ""
            child = dot.expression
            child_name = getattr(child, "name", "") or ""
            if qualifier and child_name:
                qualified = f"{qualifier.lower()}.{child_name.lower()}"
                if qualified in self.forbidden_functions:
                    issues.append({
                        "issue_type": "FORBIDDEN_FUNCTION",
                        "description": (
                            f"Forbidden SQL function/construct '{qualified}' detected."
                        ),
                    })
                    found = True
                    # Запоминаем id child-узла, чтобы не дублировать его в шаге 2
                    dot_anonymous_ids.add(id(child))

        # 2. exp.Anonymous: имя функции в нижнем регистре
        for anon in stmt.find_all(exp.Anonymous):
            if id(anon) in dot_anonymous_ids:
                continue
            fn_name = (getattr(anon, "name", "") or "").lower()
            if fn_name and fn_name in self.forbidden_functions:
                issues.append({
                    "issue_type": "FORBIDDEN_FUNCTION",
                    "description": (
                        f"Forbidden SQL function/construct '{fn_name}' detected."
                    ),
                })
                found = True

        # 3. Именованные function-классы sqlglot (НЕ Anonymous): current_user,
        #    session_user, current_setting и пр. парсятся не как exp.Anonymous,
        #    а как выделенные подклассы exp.Func (например exp.CurrentUser),
        #    у которых .name пустой — поэтому шаги 1-2 их НЕ ловят, и deny-list
        #    обходится (например `SELECT current_user` при USE_SQLGLOT=1).
        #    Сверяем канонические SQL-имена класса (sql_names()) с deny-list.
        #    Это НЕ хардкод-список: имена берутся из самой sqlglot-модели
        #    функции, а запрет — из forbidden_functions профиля (source of truth).
        for func in stmt.find_all(exp.Func):
            if isinstance(func, exp.Anonymous) or id(func) in dot_anonymous_ids:
                continue
            try:
                candidate_names = {n.lower() for n in type(func).sql_names()}
            except Exception as e:
                # НЕ молчим: sql_names() может бросить (например NotImplementedError
                # у базового exp.Func при апгрейде sqlglot) — тогда потенциальный
                # нарушитель пропускается. Логируем, чтобы обход deny-list был виден.
                logger.warning(
                    "check_forbidden_functions_ast: sql_names() failed for %s: %s",
                    type(func).__name__, e,
                )
                continue
            hit = candidate_names.intersection(self.forbidden_functions)
            if hit:
                fn_name = sorted(hit)[0]
                issues.append({
                    "issue_type": "FORBIDDEN_FUNCTION",
                    "description": (
                        f"Forbidden SQL function/construct '{fn_name}' detected."
                    ),
                })
                found = True

        # 4. exp.Table с .db: покрывает information_schema.*, pg_catalog.*, mysql.user
        for table in stmt.find_all(exp.Table):
            db = (table.db or "").lower()
            tname = (table.name or "").lower()
            # Проверяем сам «schema» как запрещённое имя
            if db and db in self.forbidden_functions:
                issues.append({
                    "issue_type": "FORBIDDEN_FUNCTION",
                    "description": (
                        f"Forbidden SQL function/construct '{db}' detected."
                    ),
                })
                found = True
                continue
            # Проверяем «schema.table» (например mysql.user)
            if db and tname:
                qualified = f"{db}.{tname}"
                if qualified in self.forbidden_functions:
                    issues.append({
                        "issue_type": "FORBIDDEN_FUNCTION",
                        "description": (
                            f"Forbidden SQL function/construct '{qualified}' detected."
                        ),
                    })
                    found = True

        return found

    def validate_statement_ast(self, stmt, issues: List[Dict[str, Any]]) -> None:
        """Валидация отдельного стейтмента через AST."""
        # AST-уровневый guard для модифицирующих стейтментов. Дополняет
        # forbidden_keywords (regex), чтобы поймать DML/DDL даже там, где
        # regex может промахнуться из-за необычного форматирования.
        ast_forbidden_classes = tuple(
            cls for cls in (
                getattr(exp, name, None) for name in self.ast_forbidden_stmt_classes
            ) if cls is not None
        )
        if ast_forbidden_classes and isinstance(stmt, ast_forbidden_classes):
            issues.append({
                "issue_type": "FORBIDDEN_STATEMENT",
                "description": f"Statement type {type(stmt).__name__} is not allowed.",
            })
            return

        set_op_classes = _set_operation_classes()
        # Проверяем, что это разрешенный тип стейтмента
        if isinstance(stmt, exp.Select):
            # Фикс #2: SELECT ... INTO newtable создаёт таблицу (PG/MSSQL).
            # stmt.args.get('into') непуст при наличии INTO-клаузы.
            into_node = stmt.args.get("into")
            if into_node is not None:
                issues.append({
                    "issue_type": "FORBIDDEN_STATEMENT",
                    "description": "SELECT ... INTO is not allowed (creates table/file).",
                })
            # Простой SELECT без INTO - разрешен; продолжаем проверку IN-списков и функций
        elif set_op_classes and isinstance(stmt, set_op_classes):
            # Set-операции (UNION/UNION ALL/INTERSECT/EXCEPT) — рекурсивно
            # валидируем обе ветки. Если дочерний узел не SELECT/With/Union —
            # отмечаем NOT_SELECT с конкретным типом (EPIC 2.4).
            for child_attr in ("this", "expression"):
                child = getattr(stmt, child_attr, None)
                if child is None:
                    continue
                if isinstance(child, exp.Select):
                    continue
                if isinstance(child, exp.With):
                    inner = getattr(child, "this", None)
                    if isinstance(inner, exp.Select):
                        continue
                    issues.append({
                        "issue_type": "NOT_SELECT",
                        "description": (
                            f"Set-operation branch contains unsupported "
                            f"child type {type(inner).__name__ if inner is not None else 'None'}."
                        ),
                    })
                    continue
                # Рекурсивная валидация для вложенных set-операций.
                if isinstance(child, set_op_classes):
                    self.validate_statement_ast(child, issues)
                    continue
                issues.append({
                    "issue_type": "NOT_SELECT",
                    "description": (
                        f"Set-operation branch contains unsupported "
                        f"child type {type(child).__name__}."
                    ),
                })
        elif isinstance(stmt, exp.With):
                # CTE - проверяем, что финальный стейтмент это SELECT либо
                # set-операция (UNION/INTERSECT/EXCEPT) — EPIC 2.4.
                inner = getattr(stmt, "this", None)
                if isinstance(inner, exp.Select):
                    pass
                elif set_op_classes and isinstance(inner, set_op_classes):
                    self.validate_statement_ast(inner, issues)
                else:
                    issues.append({
                        "issue_type": "NOT_SELECT",
                        "description": (
                            f"CTE must end with SELECT statement, got "
                            f"{type(inner).__name__ if inner is not None else 'None'}."
                        )
                    })
        elif isinstance(stmt, getattr(exp, "Describe", ())):
            pass
        elif isinstance(stmt, exp.Command):
            # Проверяем команды - разрешаем DESCRIBE и EXPLAIN, явно блокируем
            # расширенный список опасных команд (LOAD/BACKUP/RESTORE/...).
            stmt_sql = str(stmt).upper().strip()
            command_name = (getattr(stmt, "name", "") or "").upper()

            if command_name in self.ast_forbidden_command_words or any(
                stmt_sql.startswith(word + " ") or stmt_sql == word
                for word in self.ast_forbidden_command_words
            ):
                issues.append({
                    "issue_type": "FORBIDDEN_STATEMENT",
                    "description": f"Command '{command_name or stmt_sql}' is not allowed.",
                })
                return

            if stmt_sql.startswith(("DESCRIBE ", "DESC ", "EXPLAIN ", "EXPLAIN(")) or stmt_sql in ("DESCRIBE", "DESC", "EXPLAIN"):
                # AST-проверка EXPLAIN ANALYZE с fallback на расширенный regex,
                # ловящий PG-форму EXPLAIN (ANALYZE, BUFFERS) ...
                if self.ast_has_explain_analyze(stmt) or _RegexValidator.has_explain_analyze(stmt_sql):
                    issues.append({
                        "issue_type": "FORBIDDEN_EXPLAIN_ANALYZE",
                        "description": "EXPLAIN ANALYZE executes the query and is not allowed."
                    })
                # DESCRIBE/DESC/EXPLAIN разрешены для интроспекции
                pass
            else:
                issues.append({
                    "issue_type": "FORBIDDEN_COMMAND",
                    "description": f"Command '{stmt_sql}' is not allowed. Only DESCRIBE/DESC/EXPLAIN commands are permitted."
                })
        else:
            # Все остальные типы запрещены
            stmt_type = type(stmt).__name__
            issues.append({
                "issue_type": "NOT_SELECT",
                "description": f"Only SELECT queries, CTEs, and DESCRIBE/EXPLAIN commands are allowed, got {stmt_type}."
            })

        # Проверка размера IN-списков через AST (EPIC 2.6).
        # Считаем только литеральные списки (args["expressions"]); подзапросы
        # (args["query"] != None) пропускаем — они не "длинный IN".
        for in_expr in stmt.find_all(exp.In):
            expressions = in_expr.args.get("expressions")
            if expressions and in_expr.args.get("query") is None:
                count = len(expressions)
                if count > self.max_in_list_size:
                    issues.append({
                        "issue_type": "IN_LIST_TOO_LARGE",
                        "description": f"IN list has {count} items (>{self.max_in_list_size})."
                    })

        # Фикс #1/#9: AST-проверка запрещённых функций — независимая линия защиты.
        # Работает после маскировки строк, по AST (не regex), поэтому
        # не зависит от _mask_identifiers_via_lex и правильно работает при
        # USE_SQLGLOT=1.
        self.check_forbidden_functions_ast(stmt, issues)

    def is_valid_select_or_cte(
        self,
        masked_query: str,
        original_query: str | None = None,
        dsn: str | None = None,
    ) -> bool:
        """Проверяет, что запрос является SELECT, CTE или разрешенной командой (DESCRIBE/EXPLAIN).

        Использует sqlglot.parse, чтобы корректно отличать `WITH ... SELECT`
        от `WITH ... DELETE/UPDATE/INSERT` и не ломаться на `;` внутри
        строковых литералов. Для парсинга используется `original_query` (если
        передан), потому что замаскированные литералы могут стать
        синтаксически невалидными. Для legacy-режима (USE_SQLGLOT=0), когда
        sqlglot недоступен, остаётся консервативная regex-эвристика по
        masked_query — список запрещённых ключевых слов берётся из
        ``self.forbidden_keywords`` (safety.yaml профиль), без хардкода
        (W7-T4/T5, AGENTS.md).
        """
        masked_upper = masked_query.upper().strip()

        # DESCRIBE/DESC/EXPLAIN команды распознаём по префиксу, чтобы не
        # требовать от sqlglot успешного парсинга специфичных диалектных форм.
        if re.match(r"^\s*(DESCRIBE|DESC)\b", masked_upper):
            return True
        if re.match(r"^\s*EXPLAIN\b", masked_upper):
            return True

        if SQLGLOT_AVAILABLE:
            try:
                strict_dsn = bool(dsn and str(dsn).strip())
                dialect = get_sqlglot_dialect(dsn, strict=strict_dsn)
                # Парсим ИСХОДНЫЙ запрос — masked-вариант может быть невалиден
                # из-за замаскированных кавычек.
                source = original_query if original_query is not None else masked_query
                try:
                    parse_timeout = float(
                        os.getenv("SQL_VALIDATE_PARSE_TIMEOUT_SEC", "5")
                    )
                except ValueError:
                    parse_timeout = 5.0
                # Legacy-путь _validate_legacy → _is_valid_select_or_cte
                # тоже должен быть защищён от pathological SQL: переиспользуем
                # общий helper с реальным wall-time таймаутом.
                stmts = _parse_with_timeout(
                    source,
                    None if dialect == "ansi" else dialect,
                    parse_timeout,
                )
            except _ParseTimeoutError:
                return False
            except Exception:
                return False

            stmts = [s for s in stmts if s is not None]
            if len(stmts) != 1:
                # multi-statement или пустой парс — не пропускаем
                return False

            stmt = stmts[0]
            set_op_classes = _set_operation_classes()
            # WITH ... SELECT обычно парсится как exp.Select с args['with'];
            # exp.With в корне возможен для отдельных диалектов/форм.
            if isinstance(stmt, exp.Select):
                # Фикс #2: SELECT ... INTO newtable создаёт таблицу/файл
                # (PG/MSSQL) — это DDL side-effect, а не read-only SELECT.
                # validate_statement_ast ловит это на AST-пути forbidden-проверок
                # (USE_SQLGLOT=1). Здесь — независимая проверка внутри
                # is_valid_select_or_cte: она гейтится на SQLGLOT_AVAILABLE (см.
                # выше), поэтому закрывает тот же провал и при USE_SQLGLOT=0, когда
                # AST-проверка forbidden-конструкций не запускается, лишь бы
                # sqlglot был установлен для парсинга.
                if stmt.args.get("into") is not None:
                    return False
                return True
            if set_op_classes and isinstance(stmt, set_op_classes):
                # UNION/UNION ALL/INTERSECT/EXCEPT — это set-операции над SELECT,
                # допускаем как читающие. В sqlglot 27.x Intersect/Except не
                # наследуются от Union, поэтому проверяем явный tuple классов.
                return True
            if isinstance(stmt, exp.With):
                inner = getattr(stmt, "this", None)
                if isinstance(inner, exp.Select):
                    if inner.args.get("into") is not None:
                        return False
                    return True
                if set_op_classes and isinstance(inner, set_op_classes):
                    return True
                return False
            return False

        # Fallback без sqlglot: консервативный regex.
        if re.match(r"^\s*SELECT\b", masked_upper):
            return True
        if re.match(r"^\s*WITH\b", masked_upper):
            # Без AST не можем гарантировать, что финал — SELECT; принимаем,
            # только если в строке нет запрещённых модифицирующих ключевых слов
            # (они отдельно блокируются FORBIDDEN_KEYWORDS на уровне выше).
            # W7-T4/T5: список приходит из safety.yaml профиля через
            # `self.forbidden_keywords`. Если профиль не загрузился, инстанс
            # `_SqlglotValidator` не будет создан (fail-fast в
            # `load_safety_profile`), так что здесь никогда не дойдёт до
            # пустого set'а.
            if not self.forbidden_keywords:
                # Защита-инвариант: безопаснее отказать, чем пропустить.
                # Триггерится только при программной ошибке (валидатор создан
                # вручную без forbidden_keywords). Silent-fallback на хардкод
                # запрещён AGENTS.md.
                raise RuntimeError(
                    "_SqlglotValidator.forbidden_keywords is empty: "
                    "safety profile not loaded correctly"
                )
            for kw in self.forbidden_keywords:
                if re.search(fr"\b{kw}\b", masked_upper):
                    return False
            return "SELECT" in masked_upper
        return False


# =====================================================================
# PUBLIC FACADE
# =====================================================================
class SQLSafetyValidator:
    """Статическая проверка безопасности SQL запросов.

    Списки запрещённых ключевых слов / AST-классов / командных слов и числовые
    лимиты загружаются из ``config/text_to_sql/safety.yaml`` (см. EPIC 2.1).
    Никакого хардкода в этом классе быть не должно.

    Реализация декомпозирована на три internal-хелпера:
    ``_LiteralMasker``, ``_RegexValidator``, ``_SqlglotValidator``. Все
    protected методы (``_mask_string_literals``, ``_check_in_lists`` и пр.)
    сохранены как тонкие делегаторы — на них опираются внешние модули
    (``core/_sql_generation_api.py``) и тесты.
    """

    def __init__(self):
        self._profile = load_safety_profile()
        self.forbidden_keywords: List[str] = list(self._profile.forbidden_keywords)
        self.forbidden_functions: List[str] = list(
            getattr(self._profile, "forbidden_functions", []) or []
        )
        self.ast_forbidden_stmt_classes = self._profile.ast_forbidden_stmt_classes
        self.ast_forbidden_command_words = self._profile.ast_forbidden_command_words
        self.max_query_length = self._profile.max_query_length
        self.max_in_list_size = self._profile.max_in_list_size
        # NB: _sqlglot_available больше не кешируется в __init__. Чтение
        # происходит через property ниже, чтобы тесты могли monkeypatch'нуть
        # модульный SQLGLOT_AVAILABLE ПОСЛЕ создания валидатора без stale-флага.

        # Internal helpers (decomposed responsibility).
        self._masker = _LiteralMasker()
        self._regex = _RegexValidator(
            forbidden_keywords=self.forbidden_keywords,
            forbidden_functions=self.forbidden_functions,
            max_in_list_size=self.max_in_list_size,
        )
        self._sqlglot = _SqlglotValidator(
            ast_forbidden_stmt_classes=self.ast_forbidden_stmt_classes,
            ast_forbidden_command_words=self.ast_forbidden_command_words,
            max_in_list_size=self.max_in_list_size,
            # W7-T4/T5: legacy fallback в `is_valid_select_or_cte` использует
            # forbidden_keywords из того же safety.yaml профиля, что и
            # regex-валидатор. Никакого хардкода в QA-слое.
            forbidden_keywords=self.forbidden_keywords,
            # Фикс #1/#9: AST-проверка запрещённых функций.
            forbidden_functions=self.forbidden_functions,
        )
        # Фикс LOW: lock для защиты swap-операций в reload().
        self._reload_lock = threading.Lock()

    @property
    def _sqlglot_available(self) -> bool:
        """Читает ``SQLGLOT_AVAILABLE`` через фасад в момент вызова.

        Тесты monkeypatch'ат флаг на фасадном модуле
        ``custom_tools.text_to_sql.validators``; читаем оттуда, чтобы
        патч был виден без кеша в ``__init__``. Локальный
        ``SQLGLOT_AVAILABLE`` используется только если фасад не отдаёт
        атрибут (например, частичная инициализация при импорте).
        """
        try:
            import custom_tools.text_to_sql.validators as _facade
            return bool(getattr(_facade, "SQLGLOT_AVAILABLE", SQLGLOT_AVAILABLE))
        except Exception:
            return bool(SQLGLOT_AVAILABLE)

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------
    def reload(self) -> None:
        """Пере-инициализирует валидатор из свежего safety.yaml.

        W8-T5: сбрасывает кеш ``safety_config`` и заново собирает
        ``_RegexValidator``/``_SqlglotValidator`` с актуальным набором
        правил. Полезно, если изменили ``TEXT_TO_SQL_SAFETY_PROFILE``
        или содержимое ``config/text_to_sql/safety.yaml`` без рестарта
        процесса.

        Контракт:
          * **Вызывать только из admin endpoint или тестов**. В production
            runtime безопаснее restart процесса — это гарантирует, что все
            рабочие потоки видят новый профиль одновременно.
          * Метод не предотвращает гонки с конкурентными ``validate(...)``:
            если другой поток уже зашёл в ``validate`` со старыми
            ``_regex``/``_sqlglot``, он отработает старым профилем.
          * Никаких автоматических watcher-ов mtime — только explicit API.
            См. ``reload_safety_config`` в ``safety_config.py``.

        Реализация: вызывает ``reload_safety_config()`` (сброс кеша
        модульного загрузчика), затем повторяет логику ``__init__``,
        перезаполняя поля и пере-создавая internal helpers.
        """
        reload_safety_config()
        profile = load_safety_profile()
        forbidden_keywords = list(profile.forbidden_keywords)
        forbidden_functions = list(
            getattr(profile, "forbidden_functions", []) or []
        )
        new_regex = _RegexValidator(
            forbidden_keywords=forbidden_keywords,
            forbidden_functions=forbidden_functions,
            max_in_list_size=profile.max_in_list_size,
        )
        new_sqlglot = _SqlglotValidator(
            ast_forbidden_stmt_classes=profile.ast_forbidden_stmt_classes,
            ast_forbidden_command_words=profile.ast_forbidden_command_words,
            max_in_list_size=profile.max_in_list_size,
            forbidden_keywords=forbidden_keywords,
            # Фикс #1/#9: передаём forbidden_functions для AST-проверки функций.
            forbidden_functions=forbidden_functions,
        )
        # Lock сериализует ОДНОВРЕМЕННЫЕ reload() между собой (чтобы записи
        # полей двух параллельных reload не переплелись). Он НЕ защищает
        # конкурентные validate(): читатели не берут этот lock и могут
        # увидеть старый профиль (см. контракт в docstring: reload — только
        # admin/тесты; в production — restart процесса).
        with self._reload_lock:
            self._profile = profile
            self.forbidden_keywords = forbidden_keywords
            self.forbidden_functions = forbidden_functions
            self.ast_forbidden_stmt_classes = profile.ast_forbidden_stmt_classes
            self.ast_forbidden_command_words = profile.ast_forbidden_command_words
            self.max_query_length = profile.max_query_length
            self.max_in_list_size = profile.max_in_list_size
            self._regex = new_regex
            self._sqlglot = new_sqlglot

    def validate(self, sql_query: str, dsn: str | None = None) -> Dict[str, Any]:
        """Проверяет SQL запрос на безопасность (СТАТИЧЕСКИЙ слой).

        W9-A10: это **static layer** — regex + sqlglot AST, БЕЗ обращений к
        LLM. Результат содержит ``layer="static"``, чтобы caller мог
        отделить статический результат от LLM-advisory (см.
        ``SQLLLMAdvisor.audit`` и ``core._sql_generation_api.sql_safety_check``).
        QA-слой не должен зависеть от LLM-доступности — даже если LLM
        упал/недоступен, статическая проверка обязана давать ответ.
        """
        result = self._validate_inner(sql_query, dsn=dsn)
        # Унифицируем layer-маркер у всех ветвей.
        result.setdefault("layer", "static")
        return result

    def _validate_inner(self, sql_query: str, dsn: str | None = None) -> Dict[str, Any]:
        q = sql_query.strip()

        safety_level = os.getenv("TEXT_TO_SQL_SAFETY_LEVEL", "strict").strip().lower()
        if safety_level != "strict":
            return {
                "is_safe": False,
                "issues": [{
                    "issue_type": "UNSUPPORTED_SAFETY_LEVEL",
                    "description": f"Unsupported SQL safety level '{safety_level}'. Only 'strict' is currently supported."
                }]
            }

        # USE_SQLGLOT=0 - явный legacy mode, не silent fallback.
        if not is_sqlglot_enabled():
            # Legacy regex-режим небезопасен с nested parens в IN-списках и
            # должен быть удалён в будущем. В production по умолчанию режим
            # отключён hard-fail'ом; поднять флаг SQL_SAFETY_ALLOW_LEGACY=1
            # можно только осознанно.
            env_name = (
                os.getenv("ENV") or os.getenv("APP_ENV") or ""
            ).strip().lower()
            if env_name == "production":
                allow_legacy = os.getenv("SQL_SAFETY_ALLOW_LEGACY", "0").strip() == "1"
                if not allow_legacy:
                    return {
                        "is_safe": False,
                        "issues": [{
                            "issue_type": "LEGACY_VALIDATION_DISABLED",
                            "description": (
                                "Legacy SQL safety validation (USE_SQLGLOT=0) is disabled "
                                "in production. Set SQL_SAFETY_ALLOW_LEGACY=1 to override."
                            ),
                        }],
                    }
                logger.warning(
                    "SQL safety: legacy USE_SQLGLOT=0 mode allowed in production via "
                    "SQL_SAFETY_ALLOW_LEGACY=1; regex-only validation is unsafe."
                )
            return self._validate_legacy(q, dsn=dsn)

        if self._sqlglot_available:
            return self._validate_with_sqlglot(q, dsn=dsn)

        return {
            "is_safe": False,
            "issues": [{
                "issue_type": "SQLGLOT_UNAVAILABLE",
                "description": "SQLglot is not available. Strict SQL validation cannot run."
            }]
        }

    # ------------------------------------------------------------------
    # ORCHESTRATION (legacy + sqlglot paths)
    # ------------------------------------------------------------------
    def _validate_legacy(self, sql_query: str, dsn: str | None = None) -> Dict[str, Any]:
        """Legacy read-only validation used only when sqlglot is explicitly disabled."""
        with _SQLGLOT_METRICS_LOCK:
            _SQLGLOT_METRICS["fallback_count"] += 1

        issues: List[Dict[str, Any]] = []
        strict_dsn = bool(dsn and str(dsn).strip())
        dialect_name = get_current_dialect_name(dsn, strict=strict_dsn)
        masked_sql = self._mask_string_literals(sql_query, dialect_name)
        upper_sql = masked_sql.upper().strip()

        if self._contains_comments(masked_sql, original_query=sql_query, dialect=dialect_name):
            issues.append({
                "issue_type": "COMMENTS_NOT_ALLOWED",
                "description": "SQL comments are not allowed."
            })

        # EPIC 2.8: накапливаем все запрещённые ключевые слова, без early break,
        # чтобы дать пользователю полную картину нарушений за один проход.
        self._regex.check_forbidden_keywords(upper_sql, issues)
        self._regex.check_forbidden_functions(upper_sql, issues)

        if len([s for s in masked_sql.split(";") if s.strip()]) > 1:
            issues.append({
                "issue_type": "MULTI_STATEMENT",
                "description": "Multiple statements are not allowed."
            })

        if self._has_explain_analyze(upper_sql):
            issues.append({
                "issue_type": "FORBIDDEN_EXPLAIN_ANALYZE",
                "description": "EXPLAIN ANALYZE executes the query and is not allowed."
            })

        if not self._is_valid_select_or_cte(masked_sql, sql_query, dsn=dsn):
            issues.append({
                "issue_type": "NOT_SELECT",
                "description": "Only SELECT queries, CTEs, and safe DESCRIBE/EXPLAIN commands are allowed."
            })

        self._check_in_lists(masked_sql, issues)
        if len(sql_query) > self.max_query_length:
            issues.append({
                "issue_type": "QUERY_TOO_LARGE",
                "description": "Query is too long."
            })

        return {"is_safe": len(issues) == 0, "issues": issues}

    def _validate_with_sqlglot(self, sql_query: str, dsn: str | None = None) -> Dict[str, Any]:
        """Валидация с использованием sqlglot AST."""
        with _SQLGLOT_METRICS_LOCK:
            _SQLGLOT_METRICS["parse_attempts"] += 1
            _SQLGLOT_METRICS["validation_count"] += 1

        issues: List[Dict[str, Any]] = []

        strict_dsn = bool(dsn and str(dsn).strip())
        dialect_name = get_current_dialect_name(dsn, strict=strict_dsn)
        masked_sql = self._mask_string_literals(sql_query, dialect_name)
        if self._contains_comments(masked_sql, original_query=sql_query, dialect=dialect_name):
            issues.append({
                "issue_type": "COMMENTS_NOT_ALLOWED",
                "description": "SQL comments are not allowed."
            })

        # СНАЧАЛА проверяем на запрещенные ключевые слова ДО парсинга sqlglot.
        # 1. Строковые литералы уже замаскированы — SELECT 'DROP' не DDL.
        # 2. Дополнительно маскируем quoted identifiers через диалект-aware
        #    лексер sqlglot, чтобы FORBIDDEN_KEYWORDS не срабатывал на
        #    "merge", `union_col`, AS column_with_union и т.п. (EPIC 2.3).
        # AST-маршрут ниже использует ОРИГИНАЛЬНЫЙ sql_query, чтобы парсер
        # видел реальные идентификаторы.
        # EPIC 2.8: накапливаем все нарушения; AST-парсинг пропускаем, если
        # найден forbidden keyword — sqlglot всё равно не сможет надёжно
        # распарсить DML/DDL, но все накопленные issues остаются в ответе.
        masked_for_regex = self._mask_identifiers_via_lex(masked_sql, dialect_name)
        upper_sql = masked_for_regex.upper().strip()
        has_forbidden_keyword = self._regex.check_forbidden_keywords(upper_sql, issues)
        if self._regex.check_forbidden_functions(upper_sql, issues):
            has_forbidden_keyword = True
        if self._has_explain_analyze(upper_sql):
            issues.append({
                "issue_type": "FORBIDDEN_EXPLAIN_ANALYZE",
                "description": "EXPLAIN ANALYZE executes the query and is not allowed."
            })

        if not has_forbidden_keyword:
            try:
                # Парсинг SQL (только для разрешенных команд).
                # Защищаем sqlglot.parse от pathological SQL реальным
                # wall-time таймаутом — см. ``_parse_with_timeout``.
                dialect = get_sqlglot_dialect(dsn, strict=strict_dsn)
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
                    with _SQLGLOT_METRICS_LOCK:
                        _SQLGLOT_METRICS["parse_failures"] += 1
                    return {
                        "is_safe": False,
                        "issues": [{
                            "issue_type": "SQL_PARSE_TIMEOUT",
                            "description": (
                                f"SQL parse exceeded timeout of {parse_timeout}s"
                            ),
                        }],
                    }

                if not statements:
                    with _SQLGLOT_METRICS_LOCK:
                        _SQLGLOT_METRICS["parse_failures"] += 1
                    issues.append({
                        "issue_type": "PARSE_ERROR",
                        "description": "Failed to parse SQL query."
                    })
                else:
                    # Проверка на множественные стейтменты
                    if len(statements) > 1:
                        issues.append({
                            "issue_type": "MULTI_STATEMENT",
                            "description": "Multiple statements are not allowed."
                        })

                    # Проверка каждого стейтмента
                    for stmt in statements:
                        self._validate_statement_ast(stmt, issues)

            except Exception as e:
                with _SQLGLOT_METRICS_LOCK:
                    _SQLGLOT_METRICS["parse_failures"] += 1
                safe_error = _redact_safety_value(e)
                logger.error("sqlglot parsing failed: %s", safe_error)
                issues.append({
                    "issue_type": "SQL_PARSE_ERROR",
                    "description": f"Failed to parse SQL: {safe_error}"
                })

        # Проверка длины запроса — выполняется всегда, чтобы пользователь видел
        # все нарушения за один проход (EPIC 2.8).
        if len(sql_query) > self.max_query_length:
            issues.append({
                "issue_type": "QUERY_TOO_LARGE",
                "description": "Query is too long."
            })

        return {
            "is_safe": len(issues) == 0,
            "issues": issues
        }

    # ------------------------------------------------------------------
    # BACKWARD-COMPATIBLE PROTECTED API
    # Тонкие делегаторы — сохранены для:
    #   * внешнего вызова из core/_sql_generation_api.py
    #     (см. ``sql_validator._mask_string_literals(...)``);
    #   * тестов, которые либо вызывают эти методы напрямую, либо
    #     переопределяют их в субклассах (template method pattern).
    # Реальная реализация — в helper-классах выше.
    # ------------------------------------------------------------------
    def _mask_string_literals(self, sql_query: str, dialect: str | None = None) -> str:
        return self._masker.mask_string_literals(sql_query, dialect)

    def _mask_identifiers_via_lex(self, sql_query: str, dialect: str | None = None) -> str:
        return self._masker.mask_identifiers_via_lex(
            sql_query, dialect, self.forbidden_keywords
        )

    def _has_explain_analyze(self, upper_sql: str) -> bool:
        return self._regex.has_explain_analyze(upper_sql)

    def _ast_has_explain_analyze(self, stmt) -> bool:
        return self._sqlglot.ast_has_explain_analyze(stmt)

    def _validate_statement_ast(self, stmt, issues: List[Dict[str, Any]]) -> None:
        self._sqlglot.validate_statement_ast(stmt, issues)

    def _check_in_lists(self, masked_query: str, issues: List[Dict[str, Any]]) -> None:
        self._regex.check_in_lists(masked_query, issues)

    def _is_valid_select_or_cte(
        self, masked_query: str, original_query: str | None = None, dsn: str | None = None
    ) -> bool:
        return self._sqlglot.is_valid_select_or_cte(masked_query, original_query, dsn=dsn)

    def _contains_comments(
        self,
        masked_query: str,
        original_query: str | None = None,
        dialect: str | None = None,
    ) -> bool:
        return self._regex.contains_comments(masked_query, original_query, dialect)


# === W9-A10: явное разделение static_safety и llm_advisory ===================
#
# По AGENTS.md QA-слой не должен зависеть от LLM-доступности. Поэтому:
#
# * ``SQLStaticSafetyValidator`` — alias для ``SQLSafetyValidator``. Имя
#   подчёркивает, что validate() — это статический слой (regex + sqlglot AST,
#   БЕЗ LLM). Используется в новых call-sites; существующие импорты
#   ``SQLSafetyValidator`` продолжают работать без изменений.
# * ``SQLLLMAdvisor`` — отдельный класс для LLM-проверки. Возвращает
#   ``{advisory, layer: "llm_advisory", blocking: False}``. По дизайну
#   advisory НЕ блокирующий — это «второе мнение», не отменяющее результат
#   static слоя.
#
# Orchestrator (комбинированная проверка) живёт в
# ``core._sql_generation_api.sql_safety_check``: он запускает static, и
# если is_safe=False — early-return, НЕ зовёт LLM (зачем тратить токены и
# время на запрос, который и так отклонён по статическим правилам).
SQLStaticSafetyValidator = SQLSafetyValidator
"""W9-A10: явный alias для статического слоя.

Семантически идентичен ``SQLSafetyValidator``. Имя выбрано так, чтобы в
коде явно читалось «static layer, no LLM». Новые модули должны
импортировать его, а не базовое имя — это документация уровнем класса.
"""


class SQLLLMAdvisor:
    """W9-A10: LLM-advisory слой проверки SQL (NON-blocking).

    Отделён от ``SQLStaticSafetyValidator`` по AGENTS.md: QA-слой не должен
    зависеть от LLM-доступности. Результат NEVER блокирует выполнение SQL
    — это исключительно informational layer:

    * ``layer="llm_advisory"`` — маркер слоя.
    * ``blocking=False`` — явно non-blocking. Caller не должен использовать
      этот результат как gating: статический слой — единственный gate.
    * ``advisory`` — список советов от LLM (issue_type/description).

    Реализация делегирует в ``core._sql_generation_api._run_llm_safety_audit_with_timeout``
    (там кеш, пул воркеров, таймаут). Здесь — только тонкая обёртка с
    контрактом результата.

    Использование: orchestrator (``sql_safety_check``) вызывает advisor
    ТОЛЬКО если static вернул ``is_safe=True`` — для уже отклонённого
    запроса LLM-аудит бессмыслен (только тратит токены).
    """

    def audit(self, sql_query: str) -> Dict[str, Any]:
        """Запускает LLM-аудит и возвращает advisory-результат.

        Контракт результата (всегда):
          * ``advisory: List[Dict]`` — найденные риски (может быть пустым).
          * ``layer: "llm_advisory"``.
          * ``blocking: False`` — non-blocking.
          * ``status: "ok" | "timeout" | "failed"`` — статус LLM-вызова.
          * ``error: Optional[str]`` — текст ошибки при ``status != "ok"``.

        Errors handling: timeout/runtime-ошибки LLM НЕ пробрасываются —
        они трансформируются в ``status="timeout"|"failed"``. Это
        соответствует контракту «non-blocking»: ошибка LLM не должна
        ломать pipeline (статический слой уже сказал «safe»).
        """
        # Lazy-import для разрыва циклов между validators и core.
        from ..core._sql_generation_api import _run_llm_safety_audit_with_timeout

        try:
            llm_result = _run_llm_safety_audit_with_timeout(sql_query)
        except TimeoutError as exc:
            logger.error("SQLLLMAdvisor: timeout: %s", exc)
            return {
                "advisory": [],
                "layer": "llm_advisory",
                "blocking": False,
                "status": "timeout",
                "error": str(exc),
            }
        except (RuntimeError, ValueError, Exception) as exc:  # noqa: BLE001
            # Узкий по дизайну, но логически ловим всё — non-blocking слой.
            logger.error("SQLLLMAdvisor: failed: %s", exc)
            return {
                "advisory": [],
                "layer": "llm_advisory",
                "blocking": False,
                "status": "failed",
                "error": str(exc),
            }

        # llm_result ожидается dict (см. _run_llm_safety_audit). Если LLM
        # вернул неверную структуру — это ValueError, уже пойман выше.
        advisory_issues: List[Dict[str, Any]] = []
        raw_issues = llm_result.get("issues") if isinstance(llm_result, dict) else None
        if isinstance(raw_issues, list):
            for item in raw_issues:
                if (
                    isinstance(item, dict)
                    and item.get("issue_type")
                    and item.get("description")
                ):
                    advisory_issues.append({
                        "issue_type": str(item["issue_type"]),
                        "description": str(item["description"]),
                    })

        return {
            "advisory": advisory_issues,
            "layer": "llm_advisory",
            "blocking": False,
            "status": "ok",
            "error": None,
        }
