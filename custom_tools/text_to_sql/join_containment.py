"""Containment-валидатор джойнов для text2sql (T3, дизайн раздел 4).

Метрика containment(child→parent) = |distinct(col_a) ∩ distinct(col_b)| / |distinct(col_a)|
на сэмпле. У валидного FK-ребра дочерние значения ⊆ родительских ключей →
containment ≈ 1; у спурьёзного совпадения по `_id` → ≈ 0.

Подход: два ОТДЕЛЬНЫХ DISTINCT-сэмпла через плагин
(`build_distinct_values_query` + `execute_select`), пересечение множеств в
Python — без кросс-табличного подзапроса и без полного скана.

fail-open: любая ошибка/таймаут/недоступность плагина → `None` (ребро НЕ
отбрасывается, это refinement, не жёсткий gate), warning в лог. DSN в логах
ТОЛЬКО маскированный; в текст лога — `type(e).__name__`, не str(e).

In-process TTL-кэш (по образцу negative-TTL кэша в `_sql_generation_api.py`):
кэшируется и `None` (negative-cache), чтобы не дёргать БД повторно на заведомо
неопределимых рёбрах в пределах TTL.
"""
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

from .utils import mask_dsn_value

logger = logging.getLogger(__name__)


# --- Конфигурация через env (fail-safe геттеры) ----------------------------

_CONTAINMENT_SAMPLE_DEFAULT = 1000
_CONTAINMENT_CACHE_TTL_DEFAULT_S = 300.0
_CONTAINMENT_CACHE_MAX_DEFAULT = 512


def _get_containment_sample() -> int:
    """Размер DISTINCT-сэмпла. Fail-safe: при некорректном env → дефолт.

    Вызывается внутри fail-open try, но держим его устойчивым самостоятельно,
    чтобы дефолтное поведение не зависело от опечатки в env.
    """
    raw = os.getenv("TEXT_TO_SQL_CONTAINMENT_SAMPLE", str(_CONTAINMENT_SAMPLE_DEFAULT))
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be positive")
    except (TypeError, ValueError):
        logger.warning(
            "Invalid TEXT_TO_SQL_CONTAINMENT_SAMPLE=%r; using default %s",
            raw,
            _CONTAINMENT_SAMPLE_DEFAULT,
        )
        return _CONTAINMENT_SAMPLE_DEFAULT
    return value


def _get_containment_cache_ttl_s() -> float:
    raw = os.getenv(
        "TEXT_TO_SQL_CONTAINMENT_CACHE_TTL_S", str(_CONTAINMENT_CACHE_TTL_DEFAULT_S)
    )
    try:
        value = float(raw)
        if value < 0:
            raise ValueError("must be non-negative")
    except (TypeError, ValueError):
        logger.warning(
            "Invalid TEXT_TO_SQL_CONTAINMENT_CACHE_TTL_S=%r; using default %ss",
            raw,
            _CONTAINMENT_CACHE_TTL_DEFAULT_S,
        )
        return _CONTAINMENT_CACHE_TTL_DEFAULT_S
    return value


def _get_containment_cache_max() -> int:
    raw = os.getenv(
        "TEXT_TO_SQL_CONTAINMENT_CACHE_MAX", str(_CONTAINMENT_CACHE_MAX_DEFAULT)
    )
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("must be positive")
    except (TypeError, ValueError):
        logger.warning(
            "Invalid TEXT_TO_SQL_CONTAINMENT_CACHE_MAX=%r; using default %s",
            raw,
            _CONTAINMENT_CACHE_MAX_DEFAULT,
        )
        return _CONTAINMENT_CACHE_MAX_DEFAULT
    return value


# --- TTL-кэш (negative-cache: храним и None) -------------------------------

# Ключ — (dsn, table_a, col_a, table_b, col_b); значение — (timestamp, float|None).
_CONTAINMENT_CACHE: "OrderedDict[Tuple[str, str, str, str, str], Tuple[float, Optional[float]]]" = (
    OrderedDict()
)
_CONTAINMENT_CACHE_LOCK = threading.RLock()

# Sentinel: отличает cache-miss от валидного None-hit (negative-cache).
_CACHE_MISS = object()


def _clear_containment_cache() -> None:
    """Полная очистка TTL-кэша containment (используется в тестах)."""
    with _CONTAINMENT_CACHE_LOCK:
        _CONTAINMENT_CACHE.clear()


def _cache_check(key):
    """Возвращает закэшированное значение (float|None) или _CACHE_MISS."""
    ttl = _get_containment_cache_ttl_s()
    if ttl == 0:
        return _CACHE_MISS
    now = time.time()
    with _CONTAINMENT_CACHE_LOCK:
        entry = _CONTAINMENT_CACHE.get(key)
        if entry is None:
            return _CACHE_MISS
        ts, value = entry
        if now - ts > ttl:
            _CONTAINMENT_CACHE.pop(key, None)
            return _CACHE_MISS
        _CONTAINMENT_CACHE.move_to_end(key)
        return value


def _cache_put(key, value: Optional[float]) -> None:
    """Кладёт значение (включая None) в кэш с текущим временем."""
    ttl = _get_containment_cache_ttl_s()
    if ttl == 0:
        return
    cap = _get_containment_cache_max()
    now = time.time()
    with _CONTAINMENT_CACHE_LOCK:
        _CONTAINMENT_CACHE[key] = (now, value)
        _CONTAINMENT_CACHE.move_to_end(key)
        while len(_CONTAINMENT_CACHE) > cap:
            _CONTAINMENT_CACHE.popitem(last=False)


# --- Основной хелпер -------------------------------------------------------


def _distinct_sample(plugin, conn, table: str, column: str, limit: int) -> set:
    """Один DISTINCT-сэмпл через плагин → множество строковых значений.

    Бросает наружу при любой проблеме (отсутствие методов, success=False,
    исключение execute_select) — ловит вызывающий fail-open try.
    """
    if not hasattr(plugin, "build_distinct_values_query"):
        raise RuntimeError("plugin lacks build_distinct_values_query")
    if not hasattr(plugin, "execute_select"):
        raise RuntimeError("plugin lacks execute_select")

    sql = plugin.build_distinct_values_query(table, column, limit)
    result = plugin.execute_select(conn, sql, row_limit=limit)
    if not result.get("success", False):
        raise RuntimeError("execute_select returned success=False")
    # NULL уже отфильтрован в build_distinct_values_query (WHERE IS NOT NULL),
    # но на всякий случай отсекаем None из данных.
    return {str(row[0]) for row in result.get("data", []) if row and row[0] is not None}


def estimate_join_containment(
    dsn: str,
    table_a: str,
    col_a: str,
    table_b: str,
    col_b: str,
    *,
    sample_size: Optional[int] = None,
    get_plugin=None,
) -> Optional[float]:
    """Оценивает containment(child→parent) = |a∩b| / |a| на DISTINCT-сэмпле.

    Семантика результата:
      * пустая a_vals (знаменатель 0) → None (неопределимо, НЕ 0.0);
      * пустая b_vals → 0.0 (пересечение пусто, знаменатель ненулевой);
      * любая ошибка/таймаут/недоступность плагина → None (fail-open).

    sample_size=None → env TEXT_TO_SQL_CONTAINMENT_SAMPLE (default 1000).
    get_plugin=None → ленивый `from db_plugins import get_plugin` (DI-хук
    для тестов).
    """
    key = (dsn, table_a, col_a, table_b, col_b)

    cached = _cache_check(key)
    if cached is not _CACHE_MISS:
        return cached

    value = _compute_join_containment(
        dsn,
        table_a,
        col_a,
        table_b,
        col_b,
        sample_size=sample_size,
        get_plugin=get_plugin,
    )
    _cache_put(key, value)
    return value


def _compute_join_containment(
    dsn,
    table_a,
    col_a,
    table_b,
    col_b,
    *,
    sample_size,
    get_plugin,
) -> Optional[float]:
    """Считает containment без кэша. Полностью fail-open → None при ошибке."""
    try:
        limit = sample_size if sample_size is not None else _get_containment_sample()

        if get_plugin is None:
            from db_plugins import get_plugin as get_plugin_default

            get_plugin = get_plugin_default

        plugin = get_plugin(dsn)
        conn = plugin.connect(dsn)
        try:
            a_vals = _distinct_sample(plugin, conn, table_a, col_a, limit)
            b_vals = _distinct_sample(plugin, conn, table_b, col_b, limit)
        finally:
            plugin.close(conn)

        if not a_vals:
            # Знаменатель 0 — containment неопределим.
            return None
        if not b_vals:
            return 0.0

        intersection = len(a_vals & b_vals)
        return intersection / len(a_vals)
    except Exception as e:
        logger.warning(
            "estimate_join_containment failed for dsn=%s %s.%s -> %s.%s: %s "
            "(fail-open -> None)",
            mask_dsn_value(dsn),
            table_a,
            col_a,
            table_b,
            col_b,
            type(e).__name__,
        )
        return None
