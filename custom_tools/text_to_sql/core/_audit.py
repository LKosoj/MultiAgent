"""Audit / cache подмодуль core (Phase 7 декомпозиция).

Реализация: audit_logger, save_successful_sql, purge_schema_linking_rag_cache.
"""
import atexit
import hashlib
import json
import logging
import logging.handlers
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils import (
    dsn_to_sanitized_name,
    get_facade_repo_root,
    get_runtime_context_dsn,
    is_sensitive_secret_key,
    mask_dsn,
)
from ._pii import pii_mask_sync

logger = logging.getLogger(__name__)


# EPIC 7.9: RotatingFileHandler заменяет ручной цикл переименования.
# Handler кэшируется по абсолютному пути файла (под Lock'ом), чтобы:
# (1) не открывать дескриптор на каждый вызов аудита (race + leak);
# (2) тест может monkeypatch'ить core.__file__ и получать новый путь —
#     в этом случае создаём отдельный handler для нового пути.
_audit_handlers: Dict[str, logging.handlers.RotatingFileHandler] = {}
_audit_handlers_lock = threading.Lock()

# W7-T3: stale-handlers — буфер для устаревших handler'ов, которые нельзя
# закрыть синхронно в `_get_audit_handler` (см. ниже race-сценарий).
#
# Сценарий race без буфера:
#   T1: get_audit_handler() → cached → отпускает lock → handler.emit() ...
#   T2: get_audit_handler(другие max_bytes) → берёт lock → cached.close() →
#       T1 получает "I/O operation on closed file" внутри emit().
#
# Решения, рассмотренные в W7-T3:
#   1) Расширить `_audit_handlers_lock` на emit: контеншн на каждой записи
#      аудита (handler у нас один на путь — все потоки сериализуются).
#      Излишне дорого, у `RotatingFileHandler` уже есть свой `handler.lock`.
#   2) Refcount acquire/release: корректно, но добавляет API surface
#      (release нужно вызывать в finally в caller'е, легко забыть).
#   3) Отложенное закрытие: при «пересоздании» не закрываем старый handler,
#      а складываем его в `_stale_handlers`. Используем `handler.lock`,
#      встроенный в RotatingFileHandler, который уже сериализует emit
#      и shouldRollover/doRollover. atexit закрывает всё в конце процесса.
#
# Выбран вариант (3) как самый простой и корректный: переключение параметров
# через env — редкое событие, рост `_stale_handlers` ограничен числом
# переключений в рамках процесса (на практике 1-2). Каждый handler держит
# один FD до atexit; это приемлемая цена за отсутствие race на emit.
#
# L36: cap на размер _stale_handlers. При превышении STALE_HANDLERS_CAP
# закрываем и удаляем самый старый элемент. Вызывается под _audit_handlers_lock,
# поэтому thread-safe (lock уже захвачен в _get_audit_handler).
_stale_handlers: List[logging.handlers.RotatingFileHandler] = []
_STALE_HANDLERS_CAP = 16


def _sanitize_audit_text(value: str) -> str:
    """Regex-only sanitizer for audit/sqlrag text: PII plus DSN/secrets."""
    return pii_mask_sync(mask_dsn(value))


def _sanitize_audit_obj(value: Any, _memo: Optional[dict[int, Any]] = None) -> Any:
    """Recursively sanitize strings while preserving non-string JSON values."""
    if isinstance(value, str):
        return _sanitize_audit_text(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    if _memo is None:
        _memo = {}
    obj_id = id(value)
    if obj_id in _memo:
        return _memo[obj_id]
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        _memo[obj_id] = sanitized
        for key, item in value.items():
            safe_key = _sanitize_audit_text(key) if isinstance(key, str) else key
            sanitized[safe_key] = (
                "<redacted>"
                if is_sensitive_secret_key(key)
                else _sanitize_audit_obj(item, _memo)
            )
        return sanitized
    if isinstance(value, list):
        sanitized_list: list[Any] = []
        _memo[obj_id] = sanitized_list
        sanitized_list.extend(_sanitize_audit_obj(item, _memo) for item in value)
        return sanitized_list
    _memo[obj_id] = ()
    sanitized_tuple = tuple(_sanitize_audit_obj(item, _memo) for item in value)
    _memo[obj_id] = sanitized_tuple
    return sanitized_tuple


def _close_all_audit_handlers() -> None:
    """atexit-hook: закрываем все audit-handler'ы (живые + stale).

    Best-effort: исключения логируем, но не пробрасываем — atexit-hook'и
    не должны валить shutdown процесса.
    """
    with _audit_handlers_lock:
        for handler in list(_audit_handlers.values()):
            try:
                handler.close()
            except Exception as exc:  # pragma: no cover - shutdown best-effort
                logger.debug("audit handler close failed at exit: %s", exc)
        _audit_handlers.clear()
        for handler in _stale_handlers:
            try:
                handler.close()
            except Exception as exc:  # pragma: no cover - shutdown best-effort
                logger.debug("stale audit handler close failed at exit: %s", exc)
        _stale_handlers.clear()


atexit.register(_close_all_audit_handlers)


def _ensure_audit_log_secure(log_path: Path) -> None:
    """Атомарно создаёт audit-log файл с правами 0o600.

    Контракт:
        * Если файла нет — создаём через ``os.open`` с флагами
          ``O_WRONLY|O_CREAT|O_APPEND`` и mode=0o600. ``O_CREAT`` с mode
          применяется в одном syscall — между созданием и применением
          прав нет race-окна (в отличие от ``open()+chmod()``).
        * Если файл уже существует — проверяем фактический mode и при
          расхождении (бит для group/world) делаем ``os.chmod(0o600)``.
        * Windows и POSIX-системы без chmod-поддержки: исключение
          логируется через ``logger.warning``, не пробрасывается
          (аудит важнее жёсткого fail при отсутствии POSIX-прав).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    try:
        fd = os.open(str(log_path), flags, 0o600)
        os.close(fd)
    except OSError as open_err:
        logger.warning(
            "Could not pre-create audit log %s with 0o600: %s",
            log_path, open_err,
        )
        return
    try:
        current_mode = log_path.stat().st_mode & 0o777
        if current_mode != 0o600:
            os.chmod(log_path, 0o600)
    except OSError as chmod_err:
        logger.warning(
            "Could not chmod 0o600 on %s: %s", log_path, chmod_err,
        )


def _get_audit_handler(log_path: Path, max_bytes: int, backups: int) -> logging.handlers.RotatingFileHandler:
    """Возвращает RotatingFileHandler для указанного пути (кэшируется).

    Если для пути уже есть handler, но его параметры (max_bytes/backups)
    изменились через env, пересоздаём (старый перемещаем в `_stale_handlers`,
    открываем новый), чтобы не было silent-расхождения с конфигом и race
    с пишущим потоком (W7-T3): пишущий поток держит ссылку на старый handler
    и `handler.lock` сериализует его emit без участия `_audit_handlers_lock`.
    """
    key = str(log_path)
    oldest_to_close = None
    with _audit_handlers_lock:
        cached = _audit_handlers.get(key)
        if cached is not None:
            if cached.maxBytes == max_bytes and cached.backupCount == backups:
                return cached
            # Параметры изменились — НЕ закрываем cached синхронно.
            # Другой поток мог только что получить эту ссылку и сейчас вызывает
            # `cached.emit()`; синхронный `cached.close()` дал бы ему
            # «I/O operation on closed file». Откладываем close до atexit.
            # L36: перед добавлением в _stale_handlers — проверяем cap.
            # При превышении сохраняем самый старый элемент для закрытия ПОСЛЕ
            # выхода из lock. ВНИМАНИЕ (W7-T3): этот ранний close() нарушал бы
            # инвариант «не закрывать stale-handler синхронно», т.к. поток,
            # получивший ссылку ещё когда handler был cached, может быть в
            # середине emit() и поймать «I/O operation on closed file».
            # Поэтому путь emit в audit_logger обёрнут в try/except
            # (ValueError, OSError) — именно это делает ранний close()
            # race-safe. close() выполняем вне lock, чтобы не нарушать
            # «откладываем close до освобождения lock» (строки 179-182).
            if len(_stale_handlers) >= _STALE_HANDLERS_CAP:
                oldest_to_close = _stale_handlers.pop(0)
            _stale_handlers.append(cached)
            _audit_handlers.pop(key, None)
        _ensure_audit_log_secure(log_path)
        handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
            delay=True,  # не открывать файл до первой записи
        )
        # Хендлер сам форматирует только message (мы пишем готовый JSON-line).
        handler.setFormatter(logging.Formatter("%(message)s"))
        _audit_handlers[key] = handler
    if oldest_to_close is not None:
        try:
            oldest_to_close.close()
        except Exception as exc:
            logger.debug("stale audit handler early-close failed: %s", exc)
    return handler


def audit_logger(audit_entry: Dict[str, object]) -> Dict[str, str]:
    """Запись аудита в локальный файл JSONL через RotatingFileHandler.

    Args:
        audit_entry: Словарь с данными для аудита

    Returns:
        Словарь с результатом записи аудита
    """
    logger.info("Logging audit entry")

    # EPIC 7.10: secrets.token_hex(16) вместо md5(session_id+time).
    # log_id — opaque-строка длиной 32 hex-символа, контракт сохранён.
    log_id = secrets.token_hex(16)

    # Fail-fast env-валидация ВНЕ try: RuntimeError о невалидных env-переменных
    # — программная/конфигурационная ошибка, а не runtime-сбой записи аудита.
    # Маскировать её под status="error" нельзя (silent degradation, AGENTS.md).
    _max_bytes_raw = os.getenv("AUDIT_LOG_MAX_BYTES", "5242880")
    try:
        max_bytes = int(_max_bytes_raw)
    except ValueError:
        raise RuntimeError(
            f"AUDIT_LOG_MAX_BYTES must be an integer, got {_max_bytes_raw!r}"
        )
    _backups_raw = os.getenv("AUDIT_LOG_BACKUPS", "3")
    try:
        backups = int(_backups_raw)
    except ValueError:
        raise RuntimeError(
            f"AUDIT_LOG_BACKUPS must be an integer, got {_backups_raw!r}"
        )

    try:
        # EPIC 7.14: единая helper-функция вместо двух копий repo_root-вычисления.
        repo_root = get_facade_repo_root()
        log_dir = repo_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # W1-T4: ограничиваем доступ к каталогу логов (owner-only).
        # POSIX-only; best-effort: на Windows chmod игнорируется silently.
        try:
            os.chmod(log_dir, 0o700)
        except OSError as chmod_err:
            logger.warning("Could not chmod 0o700 on %s: %s", log_dir, chmod_err)
        log_path = log_dir / "audit.log"

        # W1-T4: маскируем PII/DSN/secrets ДО сериализации.
        # _sanitize_audit_obj не трогает не-строковые типы (datetime/Decimal/Path),
        # поэтому default=str в json.dumps продолжает работать как было.
        safe_entry = _sanitize_audit_obj(dict(audit_entry))

        # EPIC 7.12: default=str — устойчивая сериализация datetime/Decimal/UUID/Path.
        # Без default=str такие типы валились в TypeError, что глушилось внешним
        # except и возвращало status="error" — молчаливая деградация (AGENTS.md).
        line = json.dumps(
            {"log_id": log_id, **safe_entry},
            ensure_ascii=False,
            default=str,
        )

        # EPIC 7.9: RotatingFileHandler через shouldRollover/doRollover.
        # Подход выбран намеренно: handler инкапсулирует stdlib-логику ротации,
        # но мы не регистрируем его в logger tree (нет интерференции с
        # configure_logging). Запись делаем напрямую через handler.emit.
        handler = _get_audit_handler(log_path, max_bytes, backups)
        record = logger.makeRecord(
            name=logger.name,
            level=logging.INFO,
            fn=__file__,
            lno=0,
            msg=line,
            args=(),
            exc_info=None,
        )
        # Защищаем shouldRollover→doRollover→emit единым handler.lock, чтобы
        # два потока не вызвали doRollover одновременно и не съели backup-tier.
        #
        # W7-T3 / L36: этот поток мог получить ссылку на handler, когда он был
        # ещё cached, а затем (после >= _STALE_HANDLERS_CAP переключений конфига)
        # тот же handler стал самым старым в _stale_handlers и был закрыт ранним
        # close() в _get_audit_handler. Тогда I/O ниже даст
        # «I/O operation on closed file» (ValueError) или OSError на FD.
        # Глотаем эти ошибки: аудит — best-effort, ронять caller'а из-за гонки
        # на закрытом stale-handler'е недопустимо. Именно эта обёртка делает
        # ранний close() в _get_audit_handler race-safe, не нарушая W7-T3.
        try:
            with handler.lock:
                if handler.shouldRollover(record):
                    handler.doRollover()
                    _ensure_audit_log_secure(log_path)
                handler.emit(record)
                handler.flush()
                _ensure_audit_log_secure(log_path)
        except (ValueError, OSError) as emit_err:
            # Различаем гонку на закрытом stale-handler (W7-T3/L36 — глотаем как
            # best-effort) от РЕАЛЬНОГО сбоя записи (disk full, EACCES, broken pipe).
            # Реальный сбой происходит при ОТКРЫТОМ потоке → пробрасываем во внешний
            # except (status="error"); глотаем только закрытый/None-поток, иначе это
            # silent degradation (запрещена в AGENTS.md).
            stream = getattr(handler, "stream", None)
            if stream is None or getattr(stream, "closed", False):
                logger.debug("audit emit on closed/stale handler skipped: %s", emit_err)
            else:
                raise

        logger.info("AUDIT LOGGED")
        return {"log_id": log_id, "status": "logged"}

    except Exception as e:
        # logger.exception сохраняет stacktrace — важно для диагностики
        # OSError при ротации, ValueError из json.dumps.
        # Env-валидация вынесена ВЫШЕ try, чтобы fail-fast не маскировался
        # status="error" (см. AGENTS.md: запрет silent degradation).
        logger.exception("Audit log error: %s", e)
        return {"log_id": log_id, "status": "error", "error": str(e)}


def save_successful_sql(
    sql_query: str,
    user_query: str = "",
    execution_result: str = "",
    dsn: str | None = None,
) -> Dict[str, str]:
    """Сохраняет успешно выполненный SQL-запрос в файл sqlrag/<sanitized>_<hash>.md.

    Args:
        sql_query: SQL-запрос для сохранения
        user_query: Исходный запрос пользователя (опционально)
        execution_result: JSON-строка с результатом выполнения запроса (опционально)

    Returns:
        Словарь с результатом сохранения
    """
    logger.info("Saving successful SQL query to sqlrag file")

    # Явный dsn важен для multi-DB запусков: sqlrag examples должны
    # индексироваться под той же session_id, что и schema/RAG cache конкретного
    # workflow. Отсутствие DSN — ошибка контракта, а не recoverable file IO.
    effective_dsn = dsn or get_runtime_context_dsn()
    if not effective_dsn:
        raise ValueError(
            "save_successful_sql requires explicit dsn or workflow runtime metadata"
        )

    try:

        # Получаем санитизированный DSN.
        session_id = dsn_to_sanitized_name(effective_dsn) or "default"

        # Создаём хэш от SQL-запроса (deduplication по содержимому).
        # W8-T2: sha256 вместо md5 (md5 криптографически слаб + collision risk).
        # Усекаем до 16 hex-символов: dedup по контенту, не security id, но
        # запас по сравнению с md5[:8] на два порядка — collision-volume растёт
        # с числом сохранённых файлов.
        # ВАЖНО: hash считаем от исходного (немаскированного) sql — иначе два
        # запроса с разными email'ами схлопнутся в один файл (одна и та же
        # маска [EMAIL]). dedup должен оставаться по контенту запроса.
        sql_clean = (sql_query or "").strip()
        sql_hash = hashlib.sha256(sql_clean.encode("utf-8")).hexdigest()[:16]

        # W1-T4: маскируем PII/DSN/secrets перед записью в RAG-обучающий датасет.
        # sqlrag/*.md впоследствии включается в LLM-промпты → утечка двойная
        # (диск + контекст модели). LLM-free regex-санитизация.
        sql_clean = _sanitize_audit_text(sql_clean)
        user_query = _sanitize_audit_text(user_query) if user_query else user_query

        # Формируем имя файла
        filename = f"{session_id}_{sql_hash}.md"

        # EPIC 7.14: единая helper-функция вместо дублирующейся 3-строчной логики.
        repo_root = get_facade_repo_root()
        sqlrag_dir = repo_root / "sqlrag"
        sqlrag_dir.mkdir(parents=True, exist_ok=True)
        file_path = sqlrag_dir / filename

        # W8-T2: проверку существования делаем атомарно ПОЗЖЕ через open(..., 'x').
        # Раньше был TOCTOU: `if not exists(): write()` — два параллельных вызова
        # могли пройти проверку одновременно и перезаписать файл друг друга.

        # Формируем содержимое файла
        content_lines = [
            "enable: true",
            "",
            f"# SQL запрос ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
            ""
        ]

        if user_query:
            content_lines.extend([
                f"**Запрос пользователя:** {user_query}",
                ""
            ])

        content_lines.extend([
            "```sql",
            sql_clean,
            "```",
            ""
        ])

        if execution_result:
            # M117: предупреждение при выключенном PII-маскировании. execution_result
            # может содержать реальные данные из БД (ФИО, адреса, зарплаты и т.п.).
            # Caller обязан применить pii_masking() ДО вызова save_successful_sql.
            # Предупреждаем только если данные реально будут записаны.
            if os.getenv("PII_MASKING_ENABLED", "1") == "0":
                logger.warning(
                    "save_successful_sql: PII_MASKING_ENABLED=0 — execution_result "
                    "may contain unmasked PII data written to sqlrag artifact %s. "
                    "Apply pii_masking() before calling save_successful_sql.",
                    filename,
                )
            # EPIC 7.13: согласованная семантика при битом JSON.
            # Раньше блок except выставлял success=True при невалидном JSON —
            # это ложная информация (AGENTS.md: запрещено молчаливое искажение).
            # Решение: пишем 'unknown' в полях и добавляем явное предупреждение.
            # File-status остаётся "saved" (артефакт реально сохранён).
            parsed_ok = False
            try:
                result_data = json.loads(execution_result) if isinstance(execution_result, str) else execution_result
                # W1-T4: рекурсивная маскировка строк внутри parsed result_data
                # (поля rows/data/columns могут содержать значения из БД).
                # _sanitize_audit_obj не трогает bool/int → success/rows_count/exec_time
                # остаются типизированными числами и сериализуются как есть.
                # Regex-санитизация покрывает правила из config/pii/categories.yaml (email, inn,
                # passport, snils, card_number, phone). Колоночные PII без regex-паттерна
                # (адрес, зарплата, произвольные ФИО) не маскируются — это known limitation.
                # Card-числа покрываются правилом card_number (#5). LLM-детекция здесь
                # недопустима (audit-path deadlock). Для полного покрытия использовать
                # pii_masking() до вызова save_successful_sql.
                result_data = _sanitize_audit_obj(result_data)
                if isinstance(result_data, dict):
                    rows_count = result_data.get("rows_affected", 0)
                    success = result_data.get("success", False)
                    exec_time = result_data.get("execution_time_ms", 0)
                    parsed_ok = True
                else:
                    rows_count = "unknown"
                    success = "unknown"
                    exec_time = "unknown"
            except (json.JSONDecodeError, AttributeError, TypeError):
                rows_count = "unknown"
                success = "unknown"
                exec_time = "unknown"

            content_lines.extend([
                "**Результат выполнения:**",
                f"- Успешно: {success}",
                f"- Строк получено: {rows_count}",
                f"- Время выполнения: {exec_time}{'ms' if parsed_ok else ''}",
                ""
            ])
            if not parsed_ok:
                content_lines.extend([
                    "**Предупреждение:** execution_result не удалось распарсить как JSON.",
                    ""
                ])

        content_lines.append(f"*Сохранено автоматически: {datetime.now().isoformat()}*")

        # Записываем файл атомарно через open(..., 'x').
        # W8-T2: 'x' = exclusive create — поднимает FileExistsError, если файл
        # уже существует. Это устраняет TOCTOU с `exists()+write_text()`.
        content = "\n".join(content_lines)
        try:
            with open(file_path, "x", encoding="utf-8") as f:
                f.write(content)
        except FileExistsError:
            logger.info(f"SQL query already saved in {filename}")
            return {"status": "exists", "filename": filename, "path": str(file_path)}

        # W1-T4: ограничиваем доступ к sqlrag-артефакту (owner-only).
        # Best-effort: на Windows/при ошибках доступа — warning, без проброса.
        try:
            os.chmod(file_path, 0o600)
        except OSError as chmod_err:
            logger.warning(
                "Could not chmod 0o600 on %s: %s", file_path, chmod_err,
            )

        logger.info(f"Successfully saved SQL to {filename}")
        return {"status": "saved", "filename": filename, "path": str(file_path)}

    except Exception as e:
        logger.error(f"Failed to save SQL query: {e}")
        logger.debug("save_successful_sql exception type: %s", type(e).__name__)
        return {"status": "error", "error": str(e)}


def purge_schema_linking_rag_cache(
    session_id: Optional[str] = None,
    cache_kind: Optional[str] = None,
    agent_name: str = "Schema-RAG-Agent",
) -> int:
    """Деактивирует (temporal-valid_to) записи авто-кэша RAG в тактической памяти.

    Args:
        session_id: Идентификатор сессии (опционально)
        cache_kind: Тип кэша для очистки (опционально)
        agent_name: Имя агента для деактивации (EPIC 7.11: параметризовано;
            default="Schema-RAG-Agent" сохраняет обратную совместимость).

    Returns:
        Количество деактивированных записей
    """
    dsn = os.getenv("DB_DSN", "")
    if session_id is None and dsn:
        logger.warning(
            "purge_schema_linking_rag_cache: использует DB_DSN из env для session_id; "
            "передайте session_id явно для детерминированного поведения"
        )
    # Opt-in gate SECURE_DB_EXECUTOR_ALLOW_ENV_DSN здесь неприменим:
    # DB_DSN используется ТОЛЬКО как ключ кэша (через dsn_to_sanitized_name),
    # а не для установки DB-соединения. Риск-профиль принципиально иной, чем
    # в secure_db_executor, где env-DSN открывает реальное соединение к БД.
    # secure_db_executor блокирует env-fallback без флага, потому что ошибочный
    # DSN может дать доступ к чужой БД; здесь ошибочный DSN лишь приведёт
    # к очистке «чужого» сегмента SQLite-кэша (reversible, не security boundary).
    if not session_id:
        session_id = dsn_to_sanitized_name(dsn) or "default"

    # memory_manager берём через фасадный модуль, чтобы tests, которые
    # делают monkeypatch.setattr("...core.memory_manager", ...), работали.
    from custom_tools.text_to_sql import core as _facade
    memory_manager = _facade.memory_manager

    # Явные предпроверки для диагностики (вместо опоры на AttributeError catch
    # внутри try): когда memory_manager или его db_handler — None, нам нужен
    # понятный лог-месседж, а не "AttributeError: 'NoneType' object has no
    # attribute 'db_handler'" внутри общего AttributeError-handler.
    if memory_manager is None:
        logger.error(
            "memory_manager is None — cannot purge schema-linking RAG cache "
            "(session=%s, agent=%s)", session_id, agent_name,
        )
        return 0
    if not hasattr(memory_manager, "db_handler") or memory_manager.db_handler is None:
        logger.error(
            "memory_manager.db_handler is None — cannot purge schema-linking RAG cache "
            "(session=%s, agent=%s)", session_id, agent_name,
        )
        return 0

    conn = None
    try:
        # W7-T2: используем публичный API `get_connection` вместо
        # `_get_connection`. Публичный alias добавлен в DatabaseHandler
        # (memory/database.py, см. T3.20) специально для таких потребителей —
        # тактической памяти и схемного кэша. Если в будущем DatabaseHandler
        # переедет на pool, контракт публичного API сохранит совместимость
        # (приватный `_get_connection` про это не обещает).
        try:
            conn = memory_manager.db_handler.get_connection()
        except AttributeError as e:
            logger.error("memory_manager.db_handler unavailable: %s", e)
            return 0
        cursor = conn.cursor()
        # Выбираем активные записи для session_id + agent_name.
        cursor.execute(
            """
            SELECT step, data FROM agent_memory
            WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
            """,
            (session_id, agent_name),
        )
        rows = cursor.fetchall()
        to_deactivate: List[int] = []
        tactical_ids: List[str] = []

        for row in rows:
            step = row[0]
            data_text = row[1] or ""
            try:
                data_obj = json.loads(data_text)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Invalid JSON in agent_memory.data for session=%s step=%s: %s",
                    session_id, step, e,
                )
                continue
            if not isinstance(data_obj, dict):
                continue
            cache_source = data_obj.get("cache_source")
            if cache_source not in {"schema_linking", "vector_db_search"}:
                continue
            if cache_kind and data_obj.get("cache_kind") != cache_kind:
                continue
            to_deactivate.append(step)
            tactical_ids.append(f"{session_id}-{agent_name}-{step}")

        if not to_deactivate:
            return 0

        current_time = datetime.now().isoformat()
        count = 0

        # EPIC 7.11: явная транзакция SQLite через try/rollback.
        # Раньше при exception между UPDATE-циклом и commit транзакция
        # оставалась открытой до неявного rollback в conn.close() — без
        # диагностики. Теперь — rollback явный, исключение пробрасывается.
        try:
            for step in to_deactivate:
                cursor.execute(
                    """
                    UPDATE agent_memory
                    SET valid_to = ?, updated_at = ?
                    WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to IS NULL
                    """,
                    (current_time, current_time, session_id, agent_name, step),
                )
                count += cursor.rowcount or 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # EPIC 7.11: compensation pattern для Chroma.
        # SQLite — source of truth, Chroma eventually consistent. По умолчанию
        # (strict=0) ошибка логируется и не ломает purge. В strict-режиме
        # (strict=1) при сбое Chroma выполняем обратный UPDATE (compensation),
        # чтобы восстановить инвариант «SQLite и Chroma согласованы», и пробрасываем
        # исходное исключение.
        strict_chroma_cleanup = os.getenv("TEXT_TO_SQL_STRICT_CHROMA_CLEANUP", "0") == "1"
        try:
            tactical = memory_manager.db_handler.tactical_collection
            if tactical:
                if tactical_ids:
                    tactical.delete(ids=tactical_ids)
                    logger.info(f"Deleted {len(tactical_ids)} documents from ChromaDB")
        except Exception as e:
            if strict_chroma_cleanup:
                # Compensation: откатываем deactivation для именно тех step,
                # которые только что деактивировали (valid_to == current_time).
                try:
                    for step in to_deactivate:
                        cursor.execute(
                            """
                            UPDATE agent_memory
                            SET valid_to = NULL, updated_at = ?
                            WHERE session_id = ? AND agent_name = ? AND step = ? AND valid_to = ?
                            """,
                            (current_time, session_id, agent_name, step, current_time),
                        )
                    conn.commit()
                except Exception as comp_err:
                    # Двойная ошибка — логируем critical, но raise исходный e.
                    logger.critical(
                        f"Chroma cleanup failed AND compensation rollback failed: "
                        f"chroma_err={e}, compensation_err={comp_err}. "
                        f"SQLite state inconsistent for session={session_id}, "
                        f"agent={agent_name}, steps={to_deactivate}"
                    )
                raise
            # Нестрогий режим: логируем подробно для последующего reconciliation.
            logger.error(
                f"Failed to delete records from ChromaDB tactical memory: {e}. "
                f"SQLite committed; orphan tactical_ids={tactical_ids}. "
                f"Set TEXT_TO_SQL_STRICT_CHROMA_CLEANUP=1 to fail-fast."
            )
        return count
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as close_err:
                logger.warning("Failed to close memory_manager connection: %s", close_err)
