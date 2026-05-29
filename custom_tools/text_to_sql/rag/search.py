"""
RAGSearcher: координатор RAG-поиска SQL примеров с кэшированием в тактической памяти.

EPIC 8.10: RAGSearcher собран через КОМПОЗИЦИЮ сервисов
(IndexingService, RetrievalService, EmbeddingUtils), а не через
multi-inheritance из mixin'ов (как было в Phase 7). Это убирает архитектурный
долг "класс с 3 mixin-родителями и неявной диспетчеризацией" и закрывает
EPIC 8.10.

Process-global реестр индексации (``_index_registry``) и пер-сессионные
локи (``_per_session_locks``) теперь живут на ``SharedIndexState`` как
ClassVar — общие на весь процесс, как и было до композиции (см. 4.5).
"""
import os
import logging
import warnings
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from memory.tools import get_memory
from memory.manager import EmbeddingUnavailableError, EmbeddingFailedError

from ..deprecations import TextToSQLDeprecationWarning
from ._state import SharedIndexState
from .embedding_utils import EmbeddingUtils
from .indexing import IndexingService
from .retrieval import RetrievalService

logger = logging.getLogger(__name__)


class RAGSearcher:
    """Поисковик SQL примеров с кэшированием в тактической памяти.

    EPIC 8.10: композиция вместо multi-inheritance. RAGSearcher
    делегирует операции трём сервисам:
      * ``self._embeddings`` — хэши, session_id, эмбеддинги.
      * ``self._indexing``   — индексация sqlrag/*.md.
      * ``self._retrieval``  — Chroma/doc-поиск, ранжирование, кэш.

    Делегаторы (``_method`` → ``self._<service>._method``) сохранены, чтобы:
      1) тесты могли вызывать ``searcher._search_chroma(...)`` напрямую;
      2) ``monkeypatch.setattr(searcher, "_remove_old_file_records", ...)``
         перехватывал перекрёстные вызовы — поэтому сервисы при cross-call
         идут через ``self._host._method``, а не ``self._method``.
    """

    # EPIC 8.10: shared state хранится как ClassVar на самом RAGSearcher
    # (через инстанс SharedIndexState, поля которого тоже ClassVar) —
    # это сохраняет process-global семантику реестра/локов: несколько
    # экземпляров RAGSearcher шарят один и тот же registry/locks.
    _shared_state: ClassVar[SharedIndexState] = SharedIndexState()

    def __init__(self):
        # Поднимаемся к корню проекта.
        # __file__ = .../custom_tools/text_to_sql/rag/search.py
        # parents[0]=rag, parents[1]=text_to_sql, parents[2]=custom_tools, parents[3]=repo root
        self.repo_root = Path(__file__).resolve().parents[3]

        # Композиция: собираем сервисы. Сервисы получают host (self),
        # чтобы при cross-call (например, _ensure_sqlrag_files_indexed
        # вызывает _remove_old_file_records) запрос шёл через RAGSearcher —
        # тогда monkeypatch на инстанс RAGSearcher работает корректно.
        self._embeddings = EmbeddingUtils()
        self._indexing = IndexingService(
            state=self._shared_state,
            repo_root=self.repo_root,
            embeddings=self._embeddings,
        )
        self._indexing._host = self
        self._retrieval = RetrievalService(
            repo_root=self.repo_root,
            embeddings=self._embeddings,
        )
        self._retrieval._host = self

    # ---------------- Делегаторы: EmbeddingUtils ----------------
    def _get_session_id(self) -> str:
        return self._embeddings._get_session_id()

    def _embedding_to_key(self, emb: List[float], k: int) -> str:
        return self._embeddings._embedding_to_key(emb, k)

    def _hash_key(self, obj: Any) -> str:
        return self._embeddings._hash_key(obj)

    def _create_embedding_safe(self, text: str, purpose: str = "passage") -> Optional[List[float]]:
        return self._embeddings._create_embedding_safe(text, purpose=purpose)

    # ---------------- Сессионный лок (process-global) ----------------
    def _get_session_lock(self, session_id: str):
        """Возвращает class-level RLock для конкретного session_id.

        4.5: лок process-global, шарится между экземплярами RAGSearcher
        через ``SharedIndexState``.
        """
        return self._shared_state.get_session_lock(session_id)

    # ---------------- Делегаторы: IndexingService ----------------
    def _ensure_sqlrag_files_indexed(self) -> None:
        return self._indexing._ensure_sqlrag_files_indexed()

    def _is_file_indexed(self, session_id: str, file_hash: str) -> bool:
        return self._indexing._is_file_indexed(session_id, file_hash)

    def _index_file_in_memory(self, session_id: str, filename: str, sql_snippets: List[str], file_hash: str) -> None:
        return self._indexing._index_file_in_memory(session_id, filename, sql_snippets, file_hash)

    def _remove_old_file_records(self, session_id: str, filename: str) -> None:
        return self._indexing._remove_old_file_records(session_id, filename)

    def _cleanup_orphaned_records(self, session_id: str) -> None:
        return self._indexing._cleanup_orphaned_records(session_id)

    def _load_sqlrag_files(self, session_id: str) -> List[str]:
        return self._indexing._load_sqlrag_files(session_id)

    # ---------------- Делегаторы: RetrievalService ----------------
    def _prepare_cache_info(
        self,
        query_embedding: List[float],
        top_k: int,
        query_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._retrieval._prepare_cache_info(
            query_embedding, top_k, query_text=query_text
        )

    def _load_from_cache(self, cache_info: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        return self._retrieval._load_from_cache(cache_info)

    def _save_to_cache(self, cache_info: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        return self._retrieval._save_to_cache(cache_info, results)

    def _search_chroma(self, query_embedding: List[float], top_k: int) -> List[Dict[str, Any]]:
        return self._retrieval._search_chroma(query_embedding, top_k)

    def _search_doc_files(self, top_k: int) -> List[Dict[str, Any]]:
        return self._retrieval._search_doc_files(top_k)

    def _extract_sql_candidates_from_data(self, data_obj: Any) -> List[str]:
        return self._retrieval._extract_sql_candidates_from_data(data_obj)

    def _rerank_results_by_text(self, query_text: str, items: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
        return self._retrieval._rerank_results_by_text(query_text, items, top_k)

    def _rescore_missing_with_embedding(
        self,
        query_embedding: List[float],
        items: List[Dict[str, Any]],
        query_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._retrieval._rescore_missing_with_embedding(query_embedding, items, query_text=query_text)

    # ---------------- Публичные методы ----------------
    def search_examples(
        self,
        query_embedding: List[float],
        top_k: int = 3,
        query_text: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Поиск релевантных SQL-примеров (устаревший вход через эмбеддинг, сохранён для совместимости).

        4.3: добавлен необязательный `query_text` для симметрии prefix'ов
        E5/BGE/Instructor моделей: при его наличии рескоринг создаёт
        эмбеддинг запроса с purpose="query", а кандидатов — с purpose="passage".
        Без `query_text` эмиттится DeprecationWarning — legacy путь не
        гарантирует prefix-симметрию.

        Предпочтительный API: search_examples_by_query(query_text, ...).
        """
        logger.info("Searching for SQL examples with RAG (by embedding)")

        # W8-T9: пустой/None query_embedding — fail-fast. Без этого `[]`
        # выглядел бы как штатный cache miss и маскировал баг у caller'а
        # (например, embedding-модель вернула пустой вектор и тот не был
        # проверен перед поиском).
        if query_embedding is None or (
            isinstance(query_embedding, list) and len(query_embedding) == 0
        ):
            raise ValueError("query_embedding is empty; cannot search")

        if query_text is None:
            warnings.warn(
                "search_examples(query_embedding=...) без query_text устарел: "
                "E5/BGE prefix symmetry не гарантируется (query_embedding мог "
                "быть создан с другим purpose). Используйте "
                "search_examples_by_query(query_text=...) или передайте "
                "query_text для корректного рескоринга.",
                TextToSQLDeprecationWarning,
                stacklevel=2,
            )

        results: List[Dict[str, Any]] = []

        # Подготовка кэша (W5-T2: передаём query_text, если есть, для
        # text-based cache key — он стабильнее embedding-based).
        cache_info = self._prepare_cache_info(query_embedding, top_k, query_text=query_text)

        # Попытка загрузить из кэша
        cached_results = self._load_from_cache(cache_info)
        if cached_results:
            return cached_results

        # Загрузка и индексация sqlrag/*.md файлов
        sqlrag_snippets = self._load_sqlrag_files(cache_info["session_id"])

        # Поиск через ChromaDB (тактическая память)
        chroma_results = self._search_chroma(query_embedding, top_k)
        results.extend(chroma_results)

        allow_file_fallbacks = os.getenv("VECTOR_DB_SEARCH_ALLOW_FILE_FALLBACKS", "0") == "1"
        # Только результаты из ChromaDB считаем кэшируемыми; file-fallback
        # источники (sqlrag/*.md, doc/*.md) не сохраняем в тактическую память,
        # чтобы при появлении настоящих векторных данных кэш не возвращал
        # просроченные снимки файловых fallback'ов. Дополнительно: если
        # cache_info помечен как нек кэшируемый (нет model_id), тоже пропускаем.
        cache_eligible = bool(chroma_results) and cache_info.get("cacheable", True)

        # Opt-in fallback: использование snippets из sqlrag/*.md.
        # 4.1: similarity_score не приклеиваем — реранкер ниже расставит реальные скоры.
        if allow_file_fallbacks and not results and sqlrag_snippets:
            for snippet in sqlrag_snippets:
                results.append({"sql_example": snippet})
                if len(results) >= top_k:
                    break

        # Opt-in fallback: парсинг локальных документов в doc/*.md
        if allow_file_fallbacks and not results:
            doc_results = self._search_doc_files(top_k)
            results.extend(doc_results)

        # Сохранение в кэш
        if results:
            # Пересчёт и ранжирование по эмбеддингу запроса.
            # 4.3: если задан query_text — пробрасываем для prefix-симметрии E5/BGE.
            # W2-T3: если модель эмбеддингов недоступна — возвращаем исходные
            # results БЕЗ кеширования. Кешировать score=0 (как было раньше)
            # = silent fallback: при появлении нормальной модели на следующих
            # запросах ChromaDB-данные перебивались бы «пустым» кэшем.
            try:
                results = self._rescore_missing_with_embedding(
                    query_embedding, results, query_text=query_text
                )
                results.sort(key=lambda x: float(x.get("similarity_score", 0.0)), reverse=True)
                if cache_eligible:
                    self._save_to_cache(cache_info, results[:top_k])
            except EmbeddingUnavailableError as e:
                logger.warning(
                    "Rescore skipped (embedding model unavailable); returning unranked results without caching: %s",
                    e,
                )
            except EmbeddingFailedError as e:
                logger.warning(
                    "Rescore failed (embedding computation error); returning unranked results without caching: %s",
                    e,
                )

        return results[:top_k]

    def search_examples_by_query(self, query_text: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Поиск релевантных SQL-примеров по текстовому запросу через get_memory.
        Предпочтительный путь без прямой работы с эмбеддингами и без кэширования результатов.
        """
        logger.info("Searching for SQL examples with RAG (by text via get_memory)")

        results: List[Dict[str, Any]] = []
        session_id = self._get_session_id()

        # Семантический поиск в тактической памяти через get_memory.
        # Late lookup через фасад rag, чтобы поддерживать monkeypatch(rag, "get_memory", ...).
        try:
            # Убеждаемся, что sqlrag/*.md файлы проиндексированы в память
            # (одноразово за запуск + инкрементально). ValueError из
            # _parse_enable_frontmatter (битый YAML/enable) — fail-fast наружу
            # (см. indexing.py: "ValueError frontmatter пробрасываем выше").
            self._ensure_sqlrag_files_indexed()

            from custom_tools.text_to_sql import rag as _facade
            # T8 / #17: индексированные sqlrag-примеры пишутся под
            # cache_kind='sqlrag_example' (см. indexing.py:449), тогда как
            # 'vector_db_search' — ОТДЕЛЬНЫЙ namespace кэша поисковой выдачи
            # (retrieval.py:262), а не примеры. Раньше здесь запрашивался
            # 'vector_db_search' → проиндексированные примеры не находились
            # вовсе. Запрашиваем правильный namespace примеров.
            memory_hits = _facade.get_memory(
                session_id=session_id,
                agent_name="Schema-RAG-Agent",
                cache_kind="sqlrag_example",
                query=query_text,
                include_historical=False,
            )
            if isinstance(memory_hits, list):
                for hit in memory_hits:
                    data = hit.get("data", {}) if isinstance(hit, dict) else {}
                    for sql_text in self._extract_sql_candidates_from_data(data):
                        results.append({
                            "sql_example": sql_text,
                        })
                # Динамическое ранжирование по эмбеддингам
                results = self._rerank_results_by_text(query_text, results, top_k)
        except (RuntimeError, TimeoutError, OSError) as e:
            # HIGH #5: ValueError убран из списка — контракт indexing.py
            # требует пробрасывать его наверх (fail-fast на битом frontmatter).
            # TypeError/AttributeError/AssertionError — developer errors,
            # их тоже прокидываем наверх (не глушим).
            logger.warning(f"get_memory search failed: {e}")

        # Opt-in fallback: парсинг локальных документов в doc/*.md (если результатов недостаточно)
        if os.getenv("VECTOR_DB_SEARCH_ALLOW_FILE_FALLBACKS", "0") == "1" and len(results) < top_k:
            doc_raw = self._search_doc_files(top_k - len(results))
            # Перерасчёт скоринга для доков
            if doc_raw:
                results.extend(self._rerank_results_by_text(query_text, doc_raw, top_k - len(results)))

        return results[:top_k]
