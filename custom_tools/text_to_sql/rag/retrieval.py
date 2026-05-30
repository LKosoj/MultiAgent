"""
Поиск/ретривал SQL-сниппетов: ChromaDB, doc/*.md, ранжирование, подготовка кэша.

EPIC 8.10: класс перешёл с mixin-режима на сервис (композиция). Получает
зависимости (repo_root, embeddings) через ``__init__``.
``RetrievalMixin`` оставлен как алиас.
"""
import atexit
import math
import os
import re
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memory.manager import (
    memory_manager,
    EmbeddingUnavailableError,
    EmbeddingFailedError,
)

from ..schema_memory import _resolve_chroma_metric, _distance_to_similarity
from ._similarity import cosine_similarity
from .embedding_utils import EmbeddingUtils

logger = logging.getLogger(__name__)


# W5-T5: общий ThreadPoolExecutor для Chroma-запросов.
#
# Раньше в `_search_chroma` создавался новый ThreadPoolExecutor на каждый
# запрос. При лагающем Chroma `cancel_futures=True` НЕ прерывает уже-
# запущенный query (Python не умеет прерывать чужой sync-код), worker
# остаётся жить → линейный рост потоков под нагрузкой → OOM.
#
# Решение: один shared pool с фиксированным `max_workers`. Если все
# слоты заняты — fail-fast (RuntimeError), а не неограниченное накопление.
#
# ВАЖНО: cancel() НЕ прерывает chroma операцию (sync C-extension), и
# зависший worker удерживает слот в пуле. Это сознательный trade-off:
# либо ограниченный пул с возможным «застреванием» под катастрофическим
# Chroma-сбоем (видимое RuntimeError на overload), либо безграничный
# рост потоков. Выбран первый вариант — он деградирует громко.

_CHROMA_POOL: Optional[ThreadPoolExecutor] = None
_CHROMA_POOL_LOCK = threading.Lock()
_CHROMA_POOL_MAX_WORKERS: Optional[int] = None


def _chroma_pool_max_workers() -> int:
    """Размер shared ChromaDB-пула (env RAG_CHROMA_POOL_MAX_WORKERS, default 8)."""
    raw = os.getenv("RAG_CHROMA_POOL_MAX_WORKERS")
    if raw is None or raw == "":
        return 8
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"RAG_CHROMA_POOL_MAX_WORKERS must be a positive integer, got {raw!r}"
        )
    if value < 1:
        raise ValueError(
            f"RAG_CHROMA_POOL_MAX_WORKERS must be >= 1, got {value}"
        )
    return value


def _get_chroma_pool() -> ThreadPoolExecutor:
    """Возвращает (лениво создавая) shared executor для Chroma-запросов."""
    global _CHROMA_POOL, _CHROMA_POOL_MAX_WORKERS
    if _CHROMA_POOL is not None:
        return _CHROMA_POOL
    with _CHROMA_POOL_LOCK:
        if _CHROMA_POOL is None:
            _CHROMA_POOL_MAX_WORKERS = _chroma_pool_max_workers()
            _CHROMA_POOL = ThreadPoolExecutor(
                max_workers=_CHROMA_POOL_MAX_WORKERS,
                thread_name_prefix="rag-chroma",
            )
        return _CHROMA_POOL


def _shutdown_chroma_pool() -> None:
    """atexit-хук: корректно гасим shared pool (без ожидания зависших задач)."""
    global _CHROMA_POOL
    pool = _CHROMA_POOL
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            # atexit-контекст: даже логирование может быть недоступно
            # (handler'ы уже закрыты). Тихо выходим — это финализация.
            pass
        _CHROMA_POOL = None


atexit.register(_shutdown_chroma_pool)


def _max_n_results() -> int:
    """Верхняя граница n_results для ChromaDB-запроса (env RAG_MAX_N_RESULTS, default 100)."""
    raw = os.getenv("RAG_MAX_N_RESULTS")
    if raw is None or raw == "":
        return 100
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"RAG_MAX_N_RESULTS must be a positive integer, got {raw!r}"
        )
    if value < 1:
        raise ValueError(
            f"RAG_MAX_N_RESULTS must be >= 1, got {value}"
        )
    return value


def _chroma_query_timeout_sec() -> int:
    """Таймаут ChromaDB-запроса в секундах (env RAG_CHROMA_QUERY_TIMEOUT_SEC, default 30).

    Fail-fast: невалидное значение → ValueError с понятным сообщением,
    вместо голого ValueError из int().
    """
    raw = os.getenv("RAG_CHROMA_QUERY_TIMEOUT_SEC")
    if raw is None or raw == "":
        return 30
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"RAG_CHROMA_QUERY_TIMEOUT_SEC must be a positive integer, got {raw!r}"
        )
    if value < 1:
        raise ValueError(
            f"RAG_CHROMA_QUERY_TIMEOUT_SEC must be >= 1, got {value}"
        )
    return value


def _rescore_short_circuit_threshold() -> Optional[float]:
    """Порог short-circuit при ререскоринге (env RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD).

    По умолчанию выключен (None) — пересчитываем все результаты, не доверяя
    приклеенным заранее скорам. Если задан — пропускаем те, у кого
    similarity_score > threshold.
    """
    raw = os.getenv("RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD")
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD must be a float, got {raw!r}"
        )


def _rag_examples_min_score() -> float:
    """Минимальный порог similarity для SQL-примеров при ретривале (#16).

    Источник истины — similarity_thresholds.yaml (поле rag_examples_min_score).
    Env RAG_EXAMPLES_MIN_SCORE имеет приоритет над yaml (совместимость с тестами
    без yaml: выставить RAG_EXAMPLES_MIN_SCORE=0.0 чтобы не фильтровать ничего).

    При недоступном yaml и не заданном env — используем 0.0 (пропускать всё,
    поведение как было до добавления cutoff), чтобы тестовые среды без yaml
    не ломались.

    Деградация различает «нет конфига» и «конфиг битый»:
      * FileNotFoundError (yaml отсутствует) и KeyError (поле/профиль не
        найдены) → 0.0 с логом (test-env tolerance, отсечка снята);
      * ValueError из resolve_threshold (значение вне [0,1], нечисловое,
        невалидный формат profiles) НАМЕРЕННО НЕ перехватывается и
        пробрасывается: повреждённый source of truth должен падать громко,
        а не молча отдавать 0.0 (AGENTS.md — нет silent fallback).
    """
    raw = os.getenv("RAG_EXAMPLES_MIN_SCORE")
    if raw is not None and raw != "":
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"RAG_EXAMPLES_MIN_SCORE must be a float, got {raw!r}"
            )
    # env не задан — читаем из yaml через resolve_threshold с env_override=None
    # (env уже проверен выше). При FileNotFoundError yaml — возвращаем 0.0,
    # чтобы тестовые среды без yaml не падали.
    try:
        from ..similarity_thresholds_config import resolve_threshold
        return resolve_threshold("rag_examples_min_score")
    except FileNotFoundError:
        logger.debug(
            "similarity_thresholds.yaml не найден, rag_examples_min_score=0.0 (no cutoff)"
        )
        return 0.0
    except KeyError:
        # KeyError достижим при неполном профиле ИЛИ при неверном имени профиля
        # в env (TEXT_TO_SQL_SIMILARITY_PROFILE → get_profile). Не роняем весь
        # ретривал, но логируем ЯВНО (warning, не молча) и снимаем отсечку.
        # ВНИМАНИЕ: битое ЗНАЧЕНИЕ порога даёт ValueError — он сюда НЕ попадает
        # и намеренно пробрасывается (см. docstring).
        logger.warning(
            "similarity_thresholds.yaml: rag_examples_min_score не найден "
            "(поле или профиль) — отсечка снята (rag_examples_min_score=0.0)"
        )
        return 0.0


def _filter_min_score_unranked(
    items: List[Dict[str, Any]], min_score: float
) -> List[Dict[str, Any]]:
    """min_score-отсечка для items БЕЗ реранкинга (fallback-ветки).

    Элементы из search_examples_by_query приходят без similarity_score. Прямой
    фильтр `float(it.get("similarity_score", 0.0)) >= min_score` отбрасывал бы
    их ВСЕ при min_score>0 — молчаливая деградация (выдача 0 примеров без следа
    в логах, AGENTS.md). Здесь отсекаем только элементы, у которых скор ЕСТЬ;
    нескоринговые (фильтровать нечем) сохраняем и логируем их пропуск.
    """
    if min_score <= 0.0:
        return items
    kept: List[Dict[str, Any]] = []
    unscored = 0
    for it in items:
        raw_score = it.get("similarity_score")
        # Скоринга нет, если ключ отсутствует ИЛИ значение None/нечисловое
        # (артефакты search_examples_by_query). Во всех этих случаях фильтровать
        # нечем: сохраняем элемент и считаем unscored (а не роняем float(None)
        # TypeError'ом и не дропаем молча).
        if raw_score is None:
            unscored += 1
            kept.append(it)
            continue
        try:
            score_val = float(raw_score)
        except (TypeError, ValueError):
            unscored += 1
            kept.append(it)
            continue
        if score_val >= min_score:
            kept.append(it)
    if unscored:
        logger.debug(
            "min_score=%.3f не применён к %d элементам без similarity_score "
            "(fallback без реранкинга — фильтровать нечем)",
            min_score, unscored,
        )
    return kept


class RetrievalService:
    """Сервис: ретривал из ChromaDB, doc/*.md, ранжирование и кэш-инфо.

    EPIC 8.10: получает зависимости (repo_root, embeddings) через ``__init__``;
    больше не mixin для RAGSearcher.
    """

    def __init__(self, *, repo_root: Path, embeddings: EmbeddingUtils):
        self._repo_root = repo_root
        self._embeddings = embeddings
        # _host выставляется RAGSearcher после конструктора (см. search.py).
        # Используется, чтобы monkeypatch на host.repo_root корректно
        # отражался в сервисе (тесты подменяют searcher.repo_root).
        self._host: Optional[Any] = None

    @property
    def repo_root(self) -> Path:
        """repo_root всегда читается из host (если есть), иначе из self.

        Это позволяет тестам подменять ``searcher.repo_root`` и получать
        ожидаемое поведение в сервисе без прямого monkeypatch сервиса.
        """
        host = self._host
        if host is not None and hasattr(host, "repo_root"):
            return host.repo_root
        return self._repo_root

    @repo_root.setter
    def repo_root(self, value: Path) -> None:
        self._repo_root = value

    # --- Прокси для зависимостей --------------------------------------------
    def _embedding_to_key(self, emb: List[float], k: int) -> str:
        return self._embeddings._embedding_to_key(emb, k)

    def _text_to_key(self, text: str, k: int) -> str:
        return self._embeddings._text_to_key(text, k)

    def _create_embedding_safe(self, text: str, purpose: str = "passage"):
        return self._embeddings._create_embedding_safe(text, purpose=purpose)

    # --- Основные операции --------------------------------------------------
    def _prepare_cache_info(
        self,
        query_embedding: List[float],
        top_k: int,
        query_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Подготавливает информацию для кэширования.

        Возвращает словарь, в т.ч. с флагом ``cacheable``: True/False.
        Если model_id не удалось определить (модель эмбеддингов ещё не
        инициализирована и env RAG_EMBEDDING_MODEL_ID не задан) — мы
        НЕ кэшируем, чтобы избежать коллизий «unknown→реальная модель»,
        дающих разные ключи для одного и того же эмбеддинга.

        W5-T2: если задан `query_text`, cache key строится из него (точный
        текст запроса), а не из эмбеддинга. Это стабильнее: убирает
        зависимость от prefix-схемы embedding-модели и от плавающих
        компонентов вектора. Без `query_text` падаем обратно на
        embedding-based key (legacy путь). И тот, и другой используют
        precise hash (без квантования — см. embedding_utils.py:W5-T2).
        """
        from ..utils import get_schema_version

        # W5-T4: используем _get_session_id() (через embeddings) — он
        # делает fail-fast при пустом DSN, чтобы избежать cross-tenant
        # leak (раньше: "" → "default"/"db" → общее namespace).
        session_id_cache = self._embeddings._get_session_id()
        cache_kind = "vector_db_search"

        # Embedding model id входит в cache_key, чтобы смена модели
        # эмбеддингов не приводила к коллизии кэша.
        from custom_tools.text_to_sql import rag as _facade
        import hashlib
        _mm = _facade.memory_manager
        model_id = getattr(getattr(_mm, "db_handler", None), "embedding_model_name", None)
        if not model_id:
            model_id = os.getenv("RAG_EMBEDDING_MODEL_ID")

        cacheable = bool(model_id)
        # Стабильное значение для cache_key, даже если кэшировать нельзя
        # (cache_key всё равно возвращаем, чтобы тип не плыл; caller проверяет cacheable).
        model_id_for_key = model_id or "unknown"

        # W5-T2: если есть текст — используем его (точный hash), иначе
        # legacy путь через embedding с precise (битовым) hash.
        if query_text is not None:
            base_key = self._text_to_key(query_text, top_k)
            key_source = "text"
        else:
            base_key = self._embedding_to_key(query_embedding, top_k)
            key_source = "embedding"
        cache_key = hashlib.sha256(
            f"{model_id_for_key}|{key_source}|{base_key}".encode("utf-8")
        ).hexdigest()
        schema_version = get_schema_version(None)

        return {
            "session_id": session_id_cache,
            "cache_kind": cache_kind,
            "cache_key": cache_key,
            "schema_version": schema_version,
            "cacheable": cacheable,
        }

    def _load_from_cache(self, cache_info: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Загружает кэшированный результат vector_db_search по cache_key + schema_version.

        Fail-fast: исключения memory layer пробрасываются наверх (см. indexing.py:121).
        Late lookup через фасад rag для поддержки monkeypatch на rag.get_memory.

        Если ``cache_info["cacheable"] is False`` (model_id не определён) —
        кэш игнорируется (cache miss), чтобы не сравнивать ключи между
        «unknown»-моделью и реальной.
        """
        if not cache_info.get("cacheable", True):
            return None

        from custom_tools.text_to_sql import rag as _facade

        cached = _facade.get_memory(
            session_id=cache_info["session_id"],
            agent_name="Schema-RAG-Agent",
            cache_kind=cache_info["cache_kind"],
            include_historical=False,
        )

        for item in cached if isinstance(cached, list) else []:
            data = item.get("data", {}) if isinstance(item, dict) else {}
            if not isinstance(data, dict):
                continue
            if (data.get("cache_key") == cache_info["cache_key"]
                    and data.get("schema_version") == cache_info["schema_version"]):
                result = data.get("result")
                if result is not None:
                    return result
        return None

    def _save_to_cache(self, cache_info: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        """Сохраняет результат vector_db_search в тактическую память.

        Fail-fast: исключения memory layer пробрасываются наверх.
        Late lookup через фасад rag для поддержки monkeypatch на rag.save_memory.

        Не сохраняет результат, если ``cache_info["cacheable"] is False``
        (см. _prepare_cache_info).
        """
        if not cache_info.get("cacheable", True):
            return

        from custom_tools.text_to_sql import rag as _facade

        _facade.save_memory(
            session_id=cache_info["session_id"],
            agent_name="Schema-RAG-Agent",
            data={
                "cache_source": "vector_db_search",
                "cache_kind": cache_info["cache_kind"],
                "cache_key": cache_info["cache_key"],
                "schema_version": cache_info["schema_version"],
                "result": results,
            },
        )

    def _search_chroma(self, query_embedding: List[float], top_k: int) -> List[Dict[str, Any]]:
        """Поиск через ChromaDB. Возвращает top_k результатов, отсортированных по similarity desc.

        W5-T5: использует shared ThreadPoolExecutor (`_get_chroma_pool`), а не
        per-request executor. Это устраняет линейный рост потоков под
        лагающей Chroma.

        ВАЖНО: ``future.cancel()`` НЕ прерывает уже-запущенный Chroma-
        запрос — это sync вызов в C-extension, Python не имеет способа
        его прервать. Зависший worker удерживает слот в пуле. Если все
        слоты забиты (например, Chroma висит на каждом запросе), новый
        ``executor.submit`` блокирующе ждёт освобождения слота. Чтобы
        не висеть бесконечно, проверяем заполненность пула вручную и
        бросаем RuntimeError ("Chroma query pool overloaded") до submit.

        Поведение при timeout: тоже fail-fast (RuntimeError), без silent
        continue. См. AGENTS.md «не используй fallback».
        """
        results: List[Dict[str, Any]] = []

        # W8-T9: пустой/None query_embedding — это developer-bug, а не "ничего не нашлось".
        # Раньше при пустом векторе мы тихо возвращали [] (silent fallback), что
        # выглядело как нормальный cache miss. Теперь fail-fast.
        if query_embedding is None or (
            isinstance(query_embedding, list) and len(query_embedding) == 0
        ):
            raise ValueError("query_embedding is empty; cannot search")

        # Late lookup через фасад rag для поддержки monkeypatch на rag.memory_manager.
        from custom_tools.text_to_sql import rag as _facade
        _mm = _facade.memory_manager

        # Ожидаемые ветви "нет коллекции / нет модели / некорректный вход" —
        # это не ошибка, это штатный пустой результат (логируем как debug).
        if not (getattr(_mm, "db_handler", None) and
                getattr(_mm.db_handler, "tactical_collection", None) and
                _mm.db_handler.embedding_model and
                isinstance(query_embedding, list)):
            logger.debug("ChromaDB search skipped: tactical collection or embedding model unavailable")
            return results

        handler = _mm.db_handler
        collection = handler.tactical_collection

        # 4.2: метрику резолвим один раз вне цикла.
        metric = _resolve_chroma_metric(collection)

        # 4.9: верхняя граница n_results.
        requested = max(1, top_k * 3)
        cap = _max_n_results()
        n_results = min(requested, cap)
        if requested > cap:
            logger.info(
                "ChromaDB n_results truncated from %d to %d (RAG_MAX_N_RESULTS)",
                requested, cap,
            )

        # W5-T5: используем shared ThreadPoolExecutor вместо per-request.
        # Timeout-guard выполнения query: при таймауте — fail-fast наружу,
        # без подмены результата пустым списком (см. AGENTS.md "не используй fallback").
        #
        # Overload-guard: если все слоты пула заняты (зависшие worker'ы под
        # лагающей Chroma), бросаем RuntimeError немедленно. Без этого
        # `executor.submit` мог бы блокироваться внутри.
        timeout_sec = _chroma_query_timeout_sec()
        # _get_chroma_pool() гарантирует инициализацию _CHROMA_POOL_MAX_WORKERS
        # под локом до возврата executor — читаем глобал только после этого вызова.
        executor = _get_chroma_pool()
        # Сколько задач уже выполняется/висит в очереди — приблизительная
        # оценка по приватному `_work_queue`. Это лучший доступный сигнал;
        # внутри stdlib иного публичного API нет. Если оценить нельзя
        # (изменения в Python) — пропускаем guard и полагаемся на timeout.
        try:
            queued = executor._work_queue.qsize()  # type: ignore[attr-defined]
            # _CHROMA_POOL_MAX_WORKERS гарантированно не None после _get_chroma_pool().
            max_workers = _CHROMA_POOL_MAX_WORKERS or _chroma_pool_max_workers()
            if queued >= max_workers:
                # Все воркеры заняты и есть очередь — fail-fast.
                raise RuntimeError(
                    f"Chroma query pool overloaded: {queued} queued tasks, "
                    f"max_workers={max_workers}. Likely Chroma is hanging "
                    "(workers blocked in sync C-call, cannot be cancelled)."
                )
        except AttributeError:
            # _work_queue не доступен — пропускаем overload-guard,
            # полагаемся на timeout ниже.
            pass

        _future = executor.submit(
            lambda: collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
            )
        )
        try:
            raw = _future.result(timeout=timeout_sec)
        except FuturesTimeout:
            # cancel() НЕ прервёт уже-запущенный chroma запрос (sync C-call),
            # но снимет future из очереди если ещё не стартовал. Worker'а
            # ждать НЕ ДОЛЖНЫ — иначе timeout-guard теряет смысл; pool
            # переживёт «застрявший» worker (он отъест 1 слот из max_workers,
            # это видно через overload-guard выше).
            _future.cancel()
            logger.error(
                "ChromaDB query exceeded %ds (RAG_CHROMA_QUERY_TIMEOUT_SEC); "
                "worker remains stuck in pool (cannot cancel sync C-call)",
                timeout_sec,
            )
            raise RuntimeError(
                f"ChromaDB query exceeded {timeout_sec}s"
            )

        try:
            docs = raw.get("documents", [[]])[0] if raw else []
            distances = raw.get("distances", [[]])[0] if raw else []

            # 4.10: собираем ВСЁ, потом сортируем и берём top_k.
            for doc, dist in zip(docs, distances):
                # 4.2: если distance отсутствует — пропускаем запись (fail-fast),
                # вместо подстановки магического 0.5. Реранкер опирается на
                # реальные distance'ы, отсутствие dist — некорректный вход.
                if dist is None:
                    logger.debug("ChromaDB result skipped: distance is None")
                    continue
                for match in re.finditer(r"(?is)select\s+[\s\S]+?;", doc):
                    sql_snippet = match.group(0)
                    # 4.2: distance → similarity по фактической метрике коллекции.
                    score = _distance_to_similarity(float(dist), metric)
                    results.append({
                        "sql_example": sql_snippet.strip(),
                        "similarity_score": round(score, 3),
                    })

        except (KeyError, TypeError, AttributeError, IndexError, ValueError):
            # 4.10: ошибки парсинга raw (dict/list/число) — логируем с трассой.
            # Прочие исключения (RuntimeError и т.п.) пробрасываем наружу,
            # чтобы не маскировать баги (см. AGENTS.md: silent fallback запрещён).
            logger.exception("ChromaDB search post-processing failed")
            return results

        # 4.10: сортируем по similarity desc и возвращаем top_k.
        results.sort(key=lambda x: (-float(x.get("similarity_score", 0.0)), str(x.get("sql_example", ""))[:200]))
        # #16: отсечка по rag_examples_min_score (source of truth: similarity_thresholds.yaml).
        min_score = _rag_examples_min_score()
        if min_score > 0.0:
            before = len(results)
            results = [r for r in results if float(r.get("similarity_score", 0.0)) >= min_score]
            dropped = before - len(results)
            if dropped:
                logger.debug(
                    "_search_chroma: отброшено %d элементов с similarity_score < %.3f",
                    dropped, min_score,
                )
        return results[:top_k]

    def _search_doc_files(self, top_k: int) -> List[Dict[str, Any]]:
        """Поиск в локальных документах doc/*.md.

        4.11: similarity_score НЕ приклеивается — его задаёт реранкер вызывающей
              стороной. Если embedding-модель недоступна и
              RAG_DOC_FALLBACK_RERANK_REQUIRED=1 (default), возвращаем [].
        """
        results: List[Dict[str, Any]] = []

        if os.getenv("RAG_DOCS_ENABLE", "1") == "0":
            return results

        # 4.11: требуем доступности реранкера, чтобы не отдавать «голые» сниппеты.
        if os.getenv("RAG_DOC_FALLBACK_RERANK_REQUIRED", "1") == "1":
            from custom_tools.text_to_sql import rag as _facade
            _mm = _facade.memory_manager
            if not getattr(_mm, "_create_embedding", None):
                logger.debug(
                    "Doc fallback skipped: embedding model unavailable and "
                    "RAG_DOC_FALLBACK_RERANK_REQUIRED=1"
                )
                return results

        try:
            doc_dir = self.repo_root / "doc"
            if not doc_dir.exists():
                return results

            md_files = [p for p in doc_dir.iterdir() if p.suffix.lower() in {".md", ".markdown"}]

            for md in md_files:
                text = md.read_text(encoding="utf-8", errors="ignore")
                # Извлекаем блоки ```sql ... ```
                for block in re.findall(r"```sql\s+([\s\S]*?)```", text, flags=re.IGNORECASE):
                    snippet = block.strip()
                    if not snippet.endswith(";"):
                        snippet += ";"
                    # 4.11: без similarity_score — приклеит реранкер.
                    results.append({"sql_example": snippet})

        except (OSError, IOError, UnicodeDecodeError, KeyError, TypeError):
            # File IO ошибки чтения doc/*.md + парсинговые KeyError/TypeError.
            # Прочие исключения пробрасываем наверх (AGENTS.md: silent fallback запрещён).
            logger.exception("Doc files search failed (IO/parse error)")
            return results

        # 4.10: возвращаем top_k (без преждевременного выхода из цикла).
        return results[:top_k]

    def _extract_sql_candidates_from_data(self, data_obj: Any) -> List[str]:
        """Извлекает SQL-кандидаты из структуры данных записи памяти."""
        candidates: List[str] = []
        try:
            if not data_obj:
                return candidates
            # Частые поля. 'sql_example' — поле, под которым индексатор
            # (indexing.py) сохраняет проиндексированные sqlrag-примеры;
            # без него search_examples_by_query (cache_kind='sqlrag_example')
            # извлекал бы SQL только случайно через wildcard-regex ниже и
            # терял бы примеры без завершающей ';'.
            possible_fields = [
                "sql_query", "sql", "generated_sql", "formatted_sql_query",
                "sql_example",
            ]
            for f in possible_fields:
                val = data_obj.get(f) if isinstance(data_obj, dict) else None
                if isinstance(val, str) and re.search(r"(?is)\bselect\s+", val):
                    q = val.strip()
                    if not q.endswith(";"):
                        q += ";"
                    candidates.append(q)
            # Вложенные результаты
            if isinstance(data_obj, dict) and isinstance(data_obj.get("result"), list):
                for item in data_obj["result"]:
                    if isinstance(item, dict):
                        s = item.get("sql_example")
                        if isinstance(s, str) and re.search(r"(?is)\bselect\s+", s):
                            q = s.strip()
                            if not q.endswith(";"):
                                q += ";"
                            candidates.append(q)
            # Поиск по произвольным строковым полям
            if isinstance(data_obj, dict):
                for k, v in data_obj.items():
                    if isinstance(v, str):
                        for m in re.finditer(r"(?is)select\s+[\s\S]+?;", v):
                            candidates.append(m.group(0).strip())
        except (AttributeError, TypeError, KeyError, IndexError):
            # Защита hot path от некорректной структуры data_obj
            # (например, элемент списка result не dict).
            # Прочие исключения пробрасываем наверх (AGENTS.md: silent fallback запрещён).
            return candidates
        # Уникализируем порядок сохранения
        seen = set()
        unique: List[str] = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique

    def _rerank_results_by_text(self, query_text: str, items: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        """Реранкинг по cosine(query_emb, passage_emb).

        W2-T3: ошибки эмбеддинга различаются по типу.
          * EmbeddingUnavailableError на query — нет смысла реранжировать
            (модель не настроена); возвращаем исходный порядок items[:top_k]
            с явным warning. Это НЕ silent fallback — сигнал явно
            логируется и не маскируется под «успех».
          * EmbeddingFailedError на query — transient; логируем и тоже
            возвращаем исходный порядок (раз даже query не построился —
            нечего сравнивать).
          * Любая из ошибок на конкретный пассаж — этот пассаж получает
            score=0.0 (попадает в хвост), остальные ранжируются нормально.
        """
        if not items:
            return []
        try:
            q_emb = (self._host or self)._create_embedding_safe(query_text, purpose="query")
        except EmbeddingUnavailableError as e:
            logger.warning(
                "Rerank skipped: embedding model unavailable for query: %s", e
            )
            # min_score-фильтр при fallback без реранкинга: НЕ выбрасываем
            # молча нескоринговые элементы (см. _filter_min_score_unranked).
            filtered = _filter_min_score_unranked(items, _rag_examples_min_score())
            return filtered[:top_k]
        except EmbeddingFailedError as e:
            logger.warning(
                "Rerank skipped: embedding computation failed for query: %s", e
            )
            # min_score-фильтр при fallback без реранкинга: НЕ выбрасываем
            # молча нескоринговые элементы (см. _filter_min_score_unranked).
            filtered = _filter_min_score_unranked(items, _rag_examples_min_score())
            return filtered[:top_k]
        if not q_emb:
            logger.warning(
                "Rerank skipped: query embedding is empty/None — returning original order"
            )
            # min_score-фильтр при fallback без реранкинга: НЕ выбрасываем
            # молча нескоринговые элементы (см. _filter_min_score_unranked).
            filtered = _filter_min_score_unranked(items, _rag_examples_min_score())
            return filtered[:top_k]
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for it in items:
            sql_text = it.get("sql_example") or ""
            try:
                c_emb = (self._host or self)._create_embedding_safe(sql_text, purpose="passage")
            except (EmbeddingUnavailableError, EmbeddingFailedError) as e:
                # Пер-пассажная ошибка: логируем и считаем score=0.0
                # (пассаж попадает в хвост). Если ошибка повторится для
                # каждого пассажа подряд — суммарно вся выдача попадёт в
                # хвост, что заметно при инспекции скоров.
                logger.warning(
                    "Rerank passage embedding failed (score=0.0): %s", e
                )
                c_emb = None
            if c_emb:
                score = cosine_similarity(q_emb, c_emb)
            else:
                score = 0.0
            if not math.isfinite(score):
                score = 0.0
            new_item = dict(it)
            new_item["similarity_score"] = round(float(score), 3)
            scored.append((score, new_item))
        scored.sort(key=lambda x: x[0], reverse=True)
        # #16: отсечка по rag_examples_min_score после реранкинга.
        min_score = _rag_examples_min_score()
        if min_score > 0.0:
            before = len(scored)
            scored = [(s, it) for s, it in scored if s >= min_score]
            dropped = before - len(scored)
            if dropped:
                logger.debug(
                    "_rerank_results_by_text: отброшено %d элементов с similarity_score < %.3f",
                    dropped, min_score,
                )
        return [x[1] for x in scored[:top_k]]

    def _rescore_missing_with_embedding(
        self,
        query_embedding: List[float],
        items: List[Dict[str, Any]],
        query_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Пересчитывает similarity_score для всех результатов.

        Параметры:
          * query_embedding — legacy путь. ИНВАРИАНТ: должен быть создан с
            purpose="query" (иначе скор будет искажён из-за prefix'ов
            E5/BGE/Instructor моделей).
          * query_text — предпочтительный путь. Если задан, эмбеддинг
            запроса создаётся внутри с purpose="query" из текста.

        Использует purpose-aware API memory_manager._create_embedding для
        passage'ей, чтобы корректно работали prefix'ы моделей.

        4.4: short-circuit пропуск управляется env
            RAG_RESCORE_SHORT_CIRCUIT_THRESHOLD (default None=выкл).

        W2-T3: при недоступности embedding-модели пробрасываем
        EmbeddingUnavailableError — caller (search_examples) ловит и
        возвращает исходные results БЕЗ сохранения в кэш, чтобы не
        зафиксировать score=0.0 как «настоящие» данные.
        """
        if not items:
            return []
        # Late lookup через фасад rag для поддержки monkeypatch на rag.memory_manager.
        from custom_tools.text_to_sql import rag as _facade
        _mm = _facade.memory_manager
        if not (getattr(_mm, "db_handler", None)
                and getattr(_mm.db_handler, "embedding_model", None)):
            # W2-T3: было silent ``return items`` — теперь fail-fast,
            # чтобы caller мог решить «не кешировать score=0».
            raise EmbeddingUnavailableError(
                "Cannot rescore: embedding model is not configured"
            )

        # 4.3: если задан текст — пересоздаём эмбеддинг с purpose="query".
        q_emb: Optional[List[float]] = query_embedding
        if query_text is not None:
            # EmbeddingUnavailableError/EmbeddingFailedError пробрасываем наверх:
            # без query-эмбеддинга нечего сравнивать с пассажами.
            q_emb = (self._host or self)._create_embedding_safe(query_text, purpose="query")
            if not q_emb:
                raise EmbeddingUnavailableError(
                    "Cannot rescore: embedding model returned empty vector for query_text"
                )

        short_circuit_threshold = _rescore_short_circuit_threshold()

        for it in items:
            if (
                short_circuit_threshold is not None
                and "similarity_score" in it
                and isinstance(it["similarity_score"], (int, float))
                and it["similarity_score"] > short_circuit_threshold
            ):
                continue
            sql_text = it.get("sql_example", "")
            if not sql_text:
                it["similarity_score"] = 0.0
                continue
            # Используем purpose="passage" для SQL-сниппета (документ-кандидат).
            # Если query прошёл, но конкретный пассаж упал — это пер-пассажная
            # деградация: логируем и ставим score=0.0 (пассаж в хвост).
            try:
                c_emb = (self._host or self)._create_embedding_safe(sql_text, purpose="passage")
            except (EmbeddingUnavailableError, EmbeddingFailedError) as exc:
                logger.warning(
                    "Rescore passage embedding failed (score=0.0): %s", exc
                )
                c_emb = None
            if not c_emb:
                it["similarity_score"] = 0.0
                continue
            score = cosine_similarity(q_emb, c_emb)
            # Симметрия с _rerank_results_by_text: NaN/inf
            # в similarity_score даёт недетерминированный sort — фиксируем 0.0.
            if not math.isfinite(score):
                score = 0.0
            it["similarity_score"] = round(float(score), 3)
        return items


# EPIC 8.10: алиас для обратной совместимости.
RetrievalMixin = RetrievalService
