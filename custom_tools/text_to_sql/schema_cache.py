"""
EPIC 8.3: Cache-hashing утилиты и SchemaCacheManager.

Вынесены из единого ``schema_memory.py`` (868 строк) после разбиения по
responsibility:
  * `LINKING_CACHE_ENV_PREFIXES` — префиксы env-флагов, влияющих на
    schema-linking; source of truth для autodiscovery.
  * `_collect_linking_cache_env` — собирает текущее состояние env-флагов
    по префиксам (T3.22).
  * `_truncate_salt` — BLAKE2b принимает соль <= 16 байт; сжимаем при
    превышении (T3.5).
  * `ORDER_SIGNIFICANT_KEYS` + `_normalize_for_hash` — рекурсивная
    нормализация структур для стабильного хэширования (T3.7).
  * `SchemaCacheManager` — менеджер кэширования результатов schema-linking.

В этом модуле НЕТ прямой работы с SQLite/Chroma — только хэширование и
save_memory/get_memory через публичный API.
"""
from __future__ import annotations

import os
import json
import hashlib
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .utils import dsn_to_sanitized_name, get_runtime_context_dsn, get_schema_version

logger = logging.getLogger(__name__)


# W2-T4: явный контракт между «честный miss» и «сбой кэша».
# Раньше load_from_cache / save_to_cache ловили все исключения и возвращали
# None / no-op — silent fallback, маскирующий corruption под miss. Теперь
# различаем:
#   * miss (нет записи) → load_from_cache возвращает None;
#   * IOError / JSONDecodeError / прочая поломка backend'а → raise
#     SchemaCacheCorrupted, caller сам решает (rebuild без кэша / fail).
class SchemaCacheCorrupted(RuntimeError):
    """Кэш schema-linking повреждён или backend упал.

    Отличается от cache miss: при miss возвращается None, при corruption —
    raise (см. W2-T4 в TEXT_TO_SQL_REFACTOR_PLAN.md).
    """


# Префиксы env-флагов, влияющих на schema-linking. Это и есть source of truth:
# любая переменная окружения с одним из этих префиксов автоматически попадает
# в `linking_env_hash` и инвалидирует cache_key при изменении. Раньше тут был
# захардкоженный список из 10 имён — при добавлении нового флага в другом
# модуле кэш-ключ молча "забывал" учитывать его, давая stale cache hit.
#
# Префиксы покрывают все группы флагов schema-linking pipeline:
#   - SCHEMA_LINKING_*      (стратегии связывания, LLM-фолбэки)
#   - SCHEMA_TABLE_*        (отбор кандидатов-таблиц)
#   - SCHEMA_LLM_*          (LLM-параметры для linking)
#   - SCHEMA_INCLUDE_TABLES (allowlist таблиц)
#   - SCHEMA_MAX_*          (лимиты таблиц/колонок)
#   - SCHEMA_DESC_*         (лимиты описаний)
#   - SCHEMA_AGGRESSIVE_*   (агрессивная фильтрация)
LINKING_CACHE_ENV_PREFIXES: tuple = (
    "SCHEMA_LINKING_",
    "SCHEMA_TABLE_",
    "SCHEMA_LLM_",
    "SCHEMA_INCLUDE_TABLES",
    "SCHEMA_MAX_",
    "SCHEMA_DESC_",
    "SCHEMA_AGGRESSIVE_",
)


def _collect_linking_cache_env() -> Dict[str, str]:
    """Собирает текущее состояние env-флагов schema-linking из `os.environ`.

    Source of truth — это сам `os.environ`, отфильтрованный по
    `LINKING_CACHE_ENV_PREFIXES`. Любое изменение значения (или
    появление/исчезновение переменной) меняет cache_key.

    Возвращает отсортированный по имени словарь, чтобы итоговый
    JSON-хэш был детерминированным.
    """
    collected = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(LINKING_CACHE_ENV_PREFIXES)
    }
    return dict(sorted(collected.items()))


def _truncate_salt(salt: bytes) -> bytes:
    """BLAKE2b принимает соль максимум 16 байт.

    Если соль уже короче — отдаём как есть. Если длиннее — сжимаем через
    BLAKE2b-digest=16: банальное обрезание ``salt[:16]`` теряло энтропию
    хвоста (два session_id с общим префиксом > 16 символов давали
    одинаковую соль → одинаковые cache_key → cross-tenant leak).
    """
    if len(salt) <= 16:
        return salt
    return hashlib.blake2b(salt, digest_size=16).digest()


# T3.7: ключи, под которыми лежат списки со ЗНАЧИМЫМ порядком (joins,
# ORDER BY и т.п.). При нормализации для хэша порядок таких списков не
# сортируется, иначе кэш сольёт логически разные конфигурации
# (например, разный порядок joins → разный план выполнения).
ORDER_SIGNIFICANT_KEYS = frozenset({
    "joins",
    "join",
    "order_by",
    "orderby",
    "order",
    "sort",
    "sort_by",
})


def _normalize_for_hash(value: Any, parent_key: Optional[str] = None) -> Any:
    """Рекурсивно нормализует структуру для стабильного хеширования.

    - dict → dict с отсортированными ключами, значения тоже нормализованы.
    - list/tuple/set:
        * Если родительский ключ есть в ``ORDER_SIGNIFICANT_KEYS`` (T3.7)
          (например, ``joins`` или ``order_by``), порядок элементов
          СОХРАНЯЕТСЯ — иначе кэш сольёт логически разные конфигурации,
          где порядок несёт смысл (план выполнения JOIN'ов, ORDER BY и т.п.).
        * Иначе элементы сортируются по json-представлению, чтобы порядок
          не ломал cache hit rate. ``set``/``frozenset`` сортируются всегда
          (там порядка нет по определению).
    - примитивы возвращаются как есть.
    """
    if isinstance(value, dict):
        return {
            k: _normalize_for_hash(value[k], parent_key=k)
            for k in sorted(value.keys(), key=str)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        normalized = [_normalize_for_hash(item) for item in value]

        # set/frozenset не имеют порядка — всегда сортируем.
        is_unordered_container = isinstance(value, (set, frozenset))
        is_significant_order = (
            isinstance(parent_key, str)
            and parent_key.lower() in ORDER_SIGNIFICANT_KEYS
        )

        if is_significant_order and not is_unordered_container:
            return normalized

        try:
            return sorted(
                normalized,
                key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False),
            )
        except TypeError:
            # Несортируемые элементы — оставляем порядок (это НЕ silent
            # fallback: для гетерогенных типов сортировка по json-key
            # детерминирована, но если json не сериализуется — без выбора).
            return normalized
    return value


# W8-T1: env-fingerprint для инвалидации schema cache при смене окружения.
# Когда оператор меняет DB_DSN (другой кластер/база), профиль scoring/nlu/significance
# или путь к schema-source файлу — закэшированный linking-результат становится stale.
# Раньше cache_key учитывал SCHEMA_*-env-флаги, но НЕ TEXT_TO_SQL_*-профайлы и НЕ DSN
# (последний только через session_id; смена пароля/юзера НЕ инвалидировала кэш).
#
# Источники истины:
#   * DB_DSN — host:port:db (БЕЗ user/password! credentials не должны попадать в
#     стабильный fingerprint, иначе ротация пароля ломает cache hits без необходимости).
#   * TEXT_TO_SQL_*_PROFILE — профили yaml-конфигов, читаются модулями lazily.
#   * TEXT_TO_SQL_SCHEMA_SOURCE_PATH — путь к schema-json, если используется external loader.
_T2S_PROFILE_ENV_VARS: tuple = (
    "TEXT_TO_SQL_MAIN_TABLE_SCORING_PROFILE",
    "TEXT_TO_SQL_NLU_PROFILE",
    "TEXT_TO_SQL_SIGNIFICANCE_PROFILE",
    "TEXT_TO_SQL_SCHEMA_SOURCE_PATH",
)


def _dsn_host_port_db(dsn: str) -> str:
    """Возвращает ``host:port:db`` из DSN без user/password.

    Включаем ТОЛЬКО host/port/db: credentials не относятся к идентичности
    «какие данные лежат за этим DSN», но их ротация (типичная операция)
    не должна инвалидировать кэш. Если DSN нераспознаваем (file-based, sqlite),
    используем безопасное имя DSN: оно сохраняет структурные identity-части
    libpq/ODBC-строк и не тащит user/password в fingerprint.
    """
    if not dsn:
        return ""
    try:
        parsed = urlparse(dsn)
        host = (parsed.hostname or "").strip().lower()
        port = str(parsed.port) if parsed.port else ""
        # db — path без leading "/"; для sqlite это и есть путь к файлу.
        db = (parsed.path or "").strip("/").lower()
        scheme = (parsed.scheme or "").strip().lower()
        if not scheme:
            return dsn_to_sanitized_name(dsn)
        if host or port:
            return f"{scheme}://{host}:{port}/{db}"
        if db:
            # file-based DSN (sqlite:///path, duckdb:///path) — host пуст,
            # но db содержит путь, что и есть identity.
            return f"{scheme}:///{db}"
        return dsn_to_sanitized_name(dsn)
    except (ValueError, AttributeError):
        return dsn_to_sanitized_name(dsn)


def _compute_env_fingerprint(dsn: Optional[str] = None) -> str:
    """Возвращает sha256-хэш окружения, влияющего на schema cache.

    W8-T1: смена DB_DSN (host/port/db) или любого TEXT_TO_SQL_*_PROFILE env
    инвалидирует кэш. Реализация:
      * DSN санитизируется до host:port:db (БЕЗ credentials);
      * env-переменные собираются в отсортированный dict, чтобы порядок
        os.environ.items() не ломал fingerprint;
      * sha256 от json (sort_keys=True) — стабильно между процессами.

    Возвращает hex-строку длиной 64 (полный sha256).
    """
    raw_dsn = dsn if (isinstance(dsn, str) and dsn.strip()) else os.getenv("DB_DSN", "")
    fingerprint_input: Dict[str, str] = {
        "DB_DSN_IDENTITY": _dsn_host_port_db(raw_dsn),
    }
    for var in _T2S_PROFILE_ENV_VARS:
        value = os.getenv(var)
        # Сохраняем явное None vs "" различие через префикс ключа: отсутствие
        # переменной и пустая строка дают РАЗНЫЙ fingerprint (контракт env).
        if value is not None:
            fingerprint_input[var] = value
    serialized = json.dumps(fingerprint_input, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _compute_schema_fingerprint(schema_dict: Dict[str, Dict[str, Any]]) -> str:
    """Стабильный fingerprint схемы БЕЗ полной JSON-сериализации.

    W8-T7: full-JSON normalization+blake2b на схемах 100+ таблиц с тысячами
    колонок медленный (O(N) сериализация всей схемы + JSON-hash). Для
    cache-key-инвалидации достаточно сигнатуры формы:
      sha256(f"{tables_count}|{sorted_table_names_hash}|{max_column_count}").

    Это эвристический fingerprint: если эта тройка совпала — считаем схему
    «той же» с т.з. linking-кэша. Структурные мутации внутри колонок (тип,
    description) НЕ инвалидируют этот fingerprint — для таких изменений
    есть отдельный schema_hash (полная нормализация в prepare_cache_info,
    legacy backward-compat). См. использование в prepare_cache_info.

    Производительность: для схемы 100 таблиц fast-fingerprint считается за
    ~O(N) по списку имён (без вложенных dict/list), тогда как полный
    schema_hash требует O(N*M) — сериализации всех колонок/типов/описаний.

    Возвращает hex-строку (полный sha256, 64 символа).
    """
    if not isinstance(schema_dict, dict):
        # Не silent fallback — это контракт, который сторонним callerам
        # стоит соблюдать. Но валидацию делаем мягко (пустая строка вместо
        # raise), чтобы не ронять existing call-sites.
        return hashlib.sha256(b"").hexdigest()

    tables_count = len(schema_dict)
    table_names = sorted(schema_dict.keys(), key=str)
    # Хэш только имён — без сериализации тел таблиц.
    names_blob = "\n".join(str(n) for n in table_names).encode("utf-8")
    names_hash = hashlib.sha256(names_blob).hexdigest()

    # max_column_count — индикатор «размера» самой большой таблицы; меняется
    # при добавлении колонки в любую таблицу. Берём через safe-getter, потому
    # что схема может быть и legacy (columns прямо в корне), и new (columns
    # в ключе "columns"). Для fast-fingerprint достаточно эвристики.
    max_col = 0
    for table_body in schema_dict.values():
        if not isinstance(table_body, dict):
            continue
        cols = table_body.get("columns")
        if isinstance(cols, dict):
            col_count = len(cols)
        else:
            # legacy: ключи самой таблицы — это колонки.
            col_count = len(table_body)
        if col_count > max_col:
            max_col = col_count

    fingerprint_str = f"{tables_count}|{names_hash}|{max_col}"
    return hashlib.sha256(fingerprint_str.encode("utf-8")).hexdigest()


class SchemaCacheManager:
    """Менеджер кэширования результатов связывания схем."""

    def prepare_cache_info(
        self,
        entities: Any,
        db_schema: Dict[str, Dict[str, Dict[str, Any]]],
        dsn: Optional[str] = None,
    ) -> Dict[str, str]:
        """Подготавливает информацию для кэширования."""
        effective_dsn = (
            dsn if (isinstance(dsn, str) and dsn.strip())
            else get_runtime_context_dsn()
        )
        # Fail-fast ДО вызова dsn_to_sanitized_name: без DSN нельзя гарантировать
        # tenant isolation кэша. Молчаливый "default" приводит к cross-tenant leak.
        # dsn_to_sanitized_name никогда не возвращает пустую строку
        # (внутренний `return base or "db"`), поэтому валидировать надо именно
        # исходный dsn ДО санитизации.
        if not effective_dsn or not isinstance(effective_dsn, str) or not effective_dsn.strip():
            raise ValueError("DSN is required for schema cache namespace")
        session_id_cache = dsn_to_sanitized_name(effective_dsn)
        cache_kind = "schema_linking"
        schema_version = get_schema_version(db_schema)
        linking_env = _collect_linking_cache_env()
        # BLAKE2b с session_id-солью изолирует cache_key между сессиями:
        # одни и те же entities/schema/env у разных tenant'ов дают РАЗНЫЕ
        # ключи, что закрывает риск cross-tenant cache hit при коллизии
        # верхнеуровневых компонентов. Хэш-функция также безопаснее MD5.
        # Старые ключи (на MD5 без соли) автоматически инвалидируются —
        # это OK: кэш регенерируется из источника при первом запросе.
        session_salt = session_id_cache.encode("utf-8")
        linking_env_hash = hashlib.blake2b(
            json.dumps(linking_env, sort_keys=True, ensure_ascii=False).encode("utf-8"),
            digest_size=8,
            salt=_truncate_salt(session_salt),
        ).hexdigest()

        # Нормализуем entities, чтобы порядок элементов не ломал cache hit rate.
        normalized_entities = _normalize_for_hash(entities)
        entities_str = json.dumps(normalized_entities, sort_keys=True, ensure_ascii=False)
        entities_hash = hashlib.blake2b(
            entities_str.encode("utf-8"),
            digest_size=8,
            salt=_truncate_salt(session_salt),
        ).hexdigest()

        # Включаем hash самой схемы в cache_key: schema_version может совпадать
        # у разных снимков, а данные различаться — иначе получим stale cache hit.
        # Legacy: полная JSON-сериализация + blake2b. Сохраняем для backward-compat
        # (тесты T3.5 проверяют, что schema_hash имеет формат blake2b digest_size=8).
        schema_str = json.dumps(_normalize_for_hash(db_schema), sort_keys=True, ensure_ascii=False)
        schema_hash = hashlib.blake2b(
            schema_str.encode("utf-8"),
            digest_size=8,
            salt=_truncate_salt(session_salt),
        ).hexdigest()

        # W8-T7: дополнительный fast-fingerprint без полной сериализации. На
        # больших схемах (100+ таблиц) полный schema_hash дорог; fast-fingerprint
        # быстрее, но грубее (см. _compute_schema_fingerprint).
        # Поле НЕ заменяет schema_hash в cache_key — оба сосуществуют для
        # обратной совместимости с уже записанными cache-entries.
        schema_fingerprint = _compute_schema_fingerprint(db_schema)

        # W8-T1: env-fingerprint включает DB_DSN identity (host:port:db) и
        # TEXT_TO_SQL_*_PROFILE env-vars. Меняем профиль scoring/nlu/significance
        # → cache_key меняется → load_from_cache возвращает None (miss, не corruption).
        env_fingerprint = _compute_env_fingerprint(effective_dsn)

        # cache_key включает env_fingerprint, чтобы смена профиля/DSN
        # инвалидировала закэшированный linking-результат как miss.
        cache_key = (
            f"{cache_kind}_{schema_version}_{schema_hash}"
            f"_{linking_env_hash}_{entities_hash}_{env_fingerprint[:16]}"
        )

        return {
            "session_id": session_id_cache,
            "cache_kind": cache_kind,
            "cache_key": cache_key,
            "schema_version": schema_version,
            "schema_hash": schema_hash,
            "schema_fingerprint": schema_fingerprint,
            "entities_hash": entities_hash,
            "linking_env_hash": linking_env_hash,
            "env_fingerprint": env_fingerprint,
        }

    def load_from_cache(self, cache_info: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Загружает результат из кэша.

        Возвращает None ТОЛЬКО при честном cache miss (нет записи с нужным
        cache_key/schema_version). При IOError / JSONDecodeError / любых
        других сбоях backend'а пробрасывает SchemaCacheCorrupted — caller
        сам решает: пересоберёт без кэша или фейлится.

        W2-T4: раньше broad except → return None маскировал corruption
        под miss; теперь это явный контракт.
        """
        from memory.tools import get_memory
        from memory.manager import memory_manager

        if not (get_memory and memory_manager):
            # Backend-стек не инициализирован — это не corruption и не miss,
            # а отсутствие зависимости. Возвращаем None, чтобы caller
            # выполнил linking без кэша; это структурный edge case, не
            # silent fallback (стек явно не собран).
            return None

        # Ищем записи в кэше. IOError/JSONDecodeError/прочие сбои бэкенда
        # — это corruption, не miss; пробрасываем как SchemaCacheCorrupted.
        try:
            results = get_memory(
                session_id=cache_info["session_id"],
                agent_name="Schema-RAG-Agent",
                cache_kind=cache_info["cache_kind"],
                include_historical=False,
            )
        except (IOError, OSError, json.JSONDecodeError) as exc:
            raise SchemaCacheCorrupted(
                f"Failed to read schema-linking cache: {exc!r}"
            ) from exc

        # Ищем подходящий результат. Обход самих записей не должен ронять
        # процесс: если конкретный элемент битый — пропускаем (это уже
        # «частичный miss»). KeyError/TypeError/AttributeError здесь —
        # пропуск элемента, но не весь cache_corrupted.
        for result in results or []:
            if not isinstance(result, dict):
                continue
            data = result.get("data", {})
            if not isinstance(data, dict):
                continue
            if (data.get("cache_key") == cache_info["cache_key"]
                    and data.get("schema_version") == cache_info["schema_version"]):
                # W8-T1: явная проверка env_fingerprint. Запись со старого
                # cache_key (формат до W8-T1, без env_fingerprint в data)
                # автоматически попадёт под mismatch выше (cache_key теперь
                # содержит env_fingerprint suffix). Но если у записи есть
                # поле env_fingerprint и оно отличается — это тоже miss,
                # а не silent stale hit.
                stored_env_fp = data.get("env_fingerprint")
                if stored_env_fp is not None and stored_env_fp != cache_info.get("env_fingerprint"):
                    # Env-fingerprint поменялся — записи stale. Это miss,
                    # caller перегенерирует linking-результат.
                    logger.info(
                        "Schema cache miss: env_fingerprint mismatch (%s != %s)",
                        stored_env_fp[:8] if isinstance(stored_env_fp, str) else stored_env_fp,
                        cache_info.get("env_fingerprint", "")[:8],
                    )
                    continue
                cached_result = data.get("linking_result")
                if cached_result:
                    logger.info(f"Cache hit for schema linking: {cache_info['cache_key']}")
                    return cached_result

        return None

    def save_to_cache(self, cache_info: Dict[str, str], result: Dict[str, Any]) -> None:
        """Сохраняет результат в кэш.

        При сбое backend'а raise SchemaCacheCorrupted — caller решает
        (можно «не падать на запись», но это уже его осознанный выбор,
        а не silent no-op в библиотеке).

        W2-T4: раньше broad except → no-op маскировал ошибки записи.
        #18: перед сохранением деактивируем предыдущие активные записи
        schema_linking с тем же cache_key, чтобы кэш не рос неограниченно.
        """
        from memory.tools import save_memory
        from memory.manager import memory_manager, build_json_data_like_predicate

        if not (save_memory and memory_manager):
            return

        # Контракт cache_info валидируем ДО try-блока: отсутствие обязательных
        # ключей — программерская ошибка вызывающего кода, её нельзя маскировать
        # под операционное «не смогли деактивировать» (outer except ниже).
        # Перечисляем ВСЕ ключи, к которым ниже обращаемся как cache_info[k]
        # (без .get()): иначе сырой KeyError выскочит в середине save_to_cache
        # (строки cache_data / save_memory) с неинформативным сообщением.
        missing = [
            k
            for k in (
                "session_id",
                "cache_key",
                "cache_kind",
                "schema_version",
                "entities_hash",
            )
            if k not in cache_info
        ]
        if missing:
            raise KeyError(
                f"save_to_cache: cache_info missing required keys {missing}"
            )

        # #18: деактивируем старые активные schema_linking-записи с тем же cache_key
        try:
            conn = memory_manager.get_sqlite_connection()
            try:
                cursor = conn.cursor()
                cache_kind_pred, cache_kind_params = build_json_data_like_predicate(
                    "cache_kind", "schema_linking"
                )
                cache_key_pred, cache_key_params = build_json_data_like_predicate(
                    "cache_key", cache_info["cache_key"]
                )
                cursor.execute(
                    f"""
                    SELECT step FROM agent_memory
                    WHERE session_id = ? AND agent_name = ? AND valid_to IS NULL
                    AND {cache_kind_pred}
                    AND {cache_key_pred}
                    """,
                    [
                        cache_info["session_id"],
                        "Schema-RAG-Agent",
                        *cache_kind_params,
                        *cache_key_params,
                    ],
                )
                old_steps = [row[0] for row in cursor.fetchall()]
            finally:
                conn.close()

            if old_steps:
                conflicts = [
                    (cache_info["session_id"], "Schema-RAG-Agent", step)
                    for step in old_steps
                ]
                memory_manager._deactivate_conflicting_records(conflicts)
                logger.info(
                    "Деактивировано %d старых schema_linking-записей для cache_key=%s",
                    len(old_steps),
                    cache_info["cache_key"],
                )
        except Exception as exc:
            logger.warning(
                "Не удалось деактивировать старые schema_linking-записи (cache_key=%s): %r",
                cache_info.get("cache_key"),
                exc,
            )

        # Подготавливаем данные для сохранения
        cache_data = {
            "cache_source": "schema_linking",
            "cache_kind": cache_info["cache_kind"],
            "cache_key": cache_info["cache_key"],
            "schema_version": cache_info["schema_version"],
            "schema_hash": cache_info.get("schema_hash"),
            # W8-T7: fast schema fingerprint (только структурная сигнатура).
            "schema_fingerprint": cache_info.get("schema_fingerprint"),
            "entities_hash": cache_info["entities_hash"],
            "linking_env_hash": cache_info.get("linking_env_hash"),
            # W8-T1: env-fingerprint для явной проверки при load.
            "env_fingerprint": cache_info.get("env_fingerprint"),
            "linking_result": result,
        }

        try:
            save_memory(
                session_id=cache_info["session_id"],
                agent_name="Schema-RAG-Agent",
                data=cache_data,
            )
        except (IOError, OSError, json.JSONDecodeError, TypeError) as exc:
            # TypeError — попытка засериализовать не-JSON-совместимый
            # объект; это тоже сбой кэша (а не «всё хорошо, ничего не
            # сохранили»). Caller разберётся.
            raise SchemaCacheCorrupted(
                f"Failed to save schema-linking cache: {exc!r}"
            ) from exc

        logger.info(f"Schema linking result cached: {cache_info['cache_key']}")
