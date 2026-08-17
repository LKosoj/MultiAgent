"""
Низкоуровневая работа с базами данных (SQLite + ChromaDB)
=========================================================

DatabaseHandler отвечает за:
- Инициализацию и подключение к SQLite
- Инициализацию и подключение к ChromaDB
- Создание схемы БД с темпоральными полями
- Управление соединениями
"""

import atexit
import gc
import sqlite3
import threading
import warnings
import os
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum

import chromadb
from sentence_transformers import SentenceTransformer

warnings.filterwarnings(
    "ignore",
    message=r"builtin type (SwigPyPacked|SwigPyObject|swigvarlink) has no __module__ attribute",
    category=DeprecationWarning,
)

logger = logging.getLogger(__name__)
MEMORY_DB_SCHEMA_VERSION = 1


class VectorReadiness(str, Enum):
    """Состояние дополнительного векторного индекса.

    SQLite остаётся источником истины для памяти. Это поле нужно только для
    того, чтобы вызывающий код не принимал недоступный или устаревший Chroma
    за готовый индекс.
    """

    DISABLED = "disabled"
    READY = "ready"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    REBUILDING = "rebuilding"


def _sqlite_busy_timeout_ms() -> int:
    raw = os.getenv("MEMORY_SQLITE_BUSY_TIMEOUT_MS", "5000")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid MEMORY_SQLITE_BUSY_TIMEOUT_MS=%r; using 5000", raw)
        return 5000
    return max(0, value)


def _embedding_dimension(model) -> int | None:
    getter = getattr(model, "get_embedding_dimension", None)
    if not callable(getter):
        getter = getattr(model, "get_sentence_embedding_dimension", None)
    if callable(getter):
        try:
            dim = getter()
        except Exception:
            return None
        try:
            return int(dim) if dim is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _deduplicate_active_agent_memory(cursor: sqlite3.Cursor) -> None:
    """Soft-delete legacy active duplicates before adding the active-row index."""
    duplicate_groups = cursor.execute(
        """
        SELECT GROUP_CONCAT(rowid)
        FROM agent_memory
        WHERE valid_to IS NULL
        GROUP BY session_id, agent_name, step
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if not duplicate_groups:
        return

    base_time = datetime.now(timezone.utc)
    deduped_rows = 0
    for group_index, (rowids_csv,) in enumerate(duplicate_groups):
        rowids = [int(rowid) for rowid in str(rowids_csv).split(",") if rowid]
        if len(rowids) < 2:
            continue
        placeholders = ",".join("?" for _ in rowids)
        ordered_rowids = [
            row[0]
            for row in cursor.execute(
                f"""
                SELECT rowid
                FROM agent_memory
                WHERE rowid IN ({placeholders})
                ORDER BY COALESCE(updated_at, created_at, valid_from, timestamp) DESC, rowid DESC
                """,
                rowids,
            ).fetchall()
        ]
        for offset, rowid in enumerate(ordered_rowids[1:], start=1):
            closed_at = (
                base_time + timedelta(microseconds=group_index * 1000 + offset)
            ).isoformat()
            cursor.execute(
                """
                UPDATE agent_memory
                SET valid_to = ?, updated_at = ?
                WHERE rowid = ? AND valid_to IS NULL
                """,
                (closed_at, closed_at, rowid),
            )
            deduped_rows += cursor.rowcount

    if deduped_rows:
        logger.warning(
            "Soft-deleted %s legacy duplicate active agent_memory rows before "
            "creating idx_agent_memory_active_step_unique",
            deduped_rows,
        )


def _patch_chromadb_swig_type_modules() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        candidates = list(object.__subclasses__())
        candidates.extend(obj for obj in gc.get_objects() if isinstance(obj, type))
    for cls in candidates:
        if cls.__name__ in {"SwigPyPacked", "SwigPyObject", "swigvarlink"}:
            try:
                cls.__module__ = "swig_runtime_data4"
            except (AttributeError, TypeError):
                pass


_patch_chromadb_swig_type_modules()
atexit.register(_patch_chromadb_swig_type_modules)

# Устанавливаем переменную окружения для отключения параллелизма токенизаторов
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# Устанавливаем оффлайн режим для HuggingFace Hub (использовать только локальный кэш)
# Это предотвратит попытки скачивания модели при каждом запуске
if os.environ.get("HF_HUB_OFFLINE") != "0":  # Можно переопределить через env переменную
    os.environ["HF_HUB_OFFLINE"] = "1"

# Подавляем предупреждения от transformers
warnings.filterwarnings("ignore", category=UserWarning, module="transformers")


class DatabaseHandler:
    """Обработчик баз данных для системы памяти"""

    def __init__(
        self,
        db_path: str = "memory/smolagents_memory.db",
        chroma_path: str = "memory/chromadb",
        embedding_model: str = "intfloat/multilingual-e5-base",
    ):
        """Инициализация обработчика БД

        Args:
            db_path: Путь к файлу базы данных SQLite
            chroma_path: Путь к директории ChromaDB
            embedding_model: Модель для создания эмбеддингов
        """
        self.db_path = db_path
        self.chroma_path = chroma_path
        self._lock = threading.Lock()
        self._vector_readiness_lock = threading.Lock()
        self.vector_readiness = VectorReadiness.UNAVAILABLE
        self.vector_readiness_reason = "not_initialized"

        # Инициализация SQLite
        self._init_db()

        # Chroma — дополнительный индекс. Явное отключение не должно мешать
        # SQLite-памяти, уже инициализированной выше.
        if os.getenv("MEMORY_CHROMA_DISABLED", "0") == "1":
            self.embedding_model = None
            self.embedding_model_name = ""
            self.embedding_dimension = None
            self.embedding_metadata_mismatch = False
            self.chroma_client = None
            self.strategic_collection = None
            self.tactical_collection = None
            self.set_vector_readiness(VectorReadiness.DISABLED, "disabled_by_config")
        else:
            self._init_chroma(embedding_model)

    def set_vector_readiness(
        self,
        readiness: VectorReadiness | str,
        reason: str | None = None,
    ) -> None:
        """Публикует состояние Chroma отдельно от доступности SQLite."""
        state = VectorReadiness(readiness)
        with self._vector_readiness_lock:
            self.vector_readiness = state
            self.vector_readiness_reason = reason

    def _configure_connection(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        busy_timeout_ms = _sqlite_busy_timeout_ms()
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path not in {":memory:", ""}:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError as exc:
                logger.warning(
                    "SQLite WAL mode is unavailable for %s: %s", self.db_path, exc
                )
        return conn

    def _get_connection(self):
        """Создает новое соединение с единой SQLite policy для текущего потока."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=_sqlite_busy_timeout_ms() / 1000,
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        return self._configure_connection(conn)

    def get_connection(self):
        """Публичный alias для `_get_connection` (см. T3.20).

        Старый `_get_connection` остаётся ради обратной совместимости с
        остальной кодовой базой, новые потребители (схемная память, кэш
        linking-результата) должны звать публичный API.
        """
        return self._get_connection()

    def _init_db(self):
        """Инициализация структуры базы данных с темпоральными полями"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS agent_memory (
                    session_id TEXT,
                    agent_name TEXT,
                    step INTEGER,
                    instance_step INTEGER,
                    run_id TEXT,
                    data TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_to DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, agent_name, step, valid_to)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_session_agent 
                ON agent_memory(session_id, agent_name)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_temporal 
                ON agent_memory(valid_from, valid_to)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_active 
                ON agent_memory(session_id) WHERE valid_to IS NULL
            """)
            _deduplicate_active_agent_memory(cursor)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_memory_active_step_unique
                ON agent_memory(session_id, agent_name, step)
                WHERE valid_to IS NULL
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_run 
                ON agent_memory(session_id, agent_name, run_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_instance_step 
                ON agent_memory(session_id, agent_name, instance_step)
            """)
            # --- Strategic Memory Table ---
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategic_memory (
                    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
                    valid_to DATETIME NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategic_session_type
                ON strategic_memory(session_id, type)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategic_temporal 
                ON strategic_memory(valid_from, valid_to)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_strategic_active 
                ON strategic_memory(session_id, type) WHERE valid_to IS NULL
            """)
            current_version = cursor.execute("PRAGMA user_version").fetchone()[0]
            if current_version > MEMORY_DB_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Memory DB schema version {current_version} is newer than "
                    f"supported {MEMORY_DB_SCHEMA_VERSION}"
                )
            if current_version < MEMORY_DB_SCHEMA_VERSION:
                cursor.execute(f"PRAGMA user_version = {MEMORY_DB_SCHEMA_VERSION}")
            conn.commit()
        finally:
            conn.close()

    def _init_chroma(self, embedding_model: str):
        """Инициализация ChromaDB и модели эмбеддингов"""
        try:
            # Создаем модель эмбеддингов
            print(f"Загружаем модель эмбеддингов: {embedding_model}")
            # Сначала пробуем загрузить из локального кэша
            try:
                self.embedding_model = SentenceTransformer(
                    embedding_model, local_files_only=True
                )
                print("✅ Модель загружена из локального кэша")
            except Exception:
                # Если модели нет локально, загружаем из интернета
                print("⚠️ Модель не найдена локально, загружаем из HuggingFace...")
                self.embedding_model = SentenceTransformer(embedding_model)
            # Сохраняем читаемое имя модели для UI
            self.embedding_model_name = embedding_model
            self.embedding_dimension = _embedding_dimension(self.embedding_model)
            self.embedding_metadata_mismatch = False

            # Создаем клиент ChromaDB с отключенной телеметрией
            from chromadb.config import Settings

            settings = Settings(anonymized_telemetry=False)
            self.chroma_client = chromadb.PersistentClient(
                path=self.chroma_path, settings=settings
            )

            # Создаем коллекции для разных типов памяти.
            # W5-T1: явная метрика расстояния через hnsw:space (default
            # cosine). Все пороги text-to-sql (min_score=0.2, формулы
            # _distance_to_similarity) рассчитаны под cosine. Дефолт Chroma —
            # l2; без явной метрики формулы не работают.
            #
            # ВАЖНО: get_or_create_collection НЕ меняет метрику уже
            # существующей коллекции. Если на диске лежит коллекция,
            # созданная ранее с l2, передаваемый metadata игнорируется,
            # коллекция продолжит работать с l2. Для миграции существующих
            # развёрток нужна ре-индексация (rebuild_chromadb_from_sqlite
            # удаляет коллекции перед созданием). Фактическая метрика
            # определяется через `_resolve_chroma_metric` по
            # `collection.metadata["hnsw:space"]`.
            #
            # Опционально можно переопределить через env
            # TEXT_TO_SQL_CHROMA_METRIC (cosine|l2|ip), но cosine — единственный
            # вариант, под который написана downstream-логика.
            chroma_metric = (
                os.getenv("TEXT_TO_SQL_CHROMA_METRIC", "cosine").strip().lower()
                or "cosine"
            )
            collection_metadata = {
                "hnsw:space": chroma_metric,
                "embedding_model_name": self.embedding_model_name,
            }
            if self.embedding_dimension is not None:
                collection_metadata["embedding_dimension"] = self.embedding_dimension
            self.strategic_collection = self.chroma_client.get_or_create_collection(
                name="strategic_memory",
                metadata={
                    "description": "High-level goals and context",
                    **collection_metadata,
                },
            )

            self.tactical_collection = self.chroma_client.get_or_create_collection(
                name="tactical_memory",
                metadata={
                    "description": "Detailed step-by-step agent experience",
                    **collection_metadata,
                },
            )

            # W5-T1: предупреждаем, если коллекция уже существовала с другой
            # метрикой — это означает, что новый metadata был проигнорирован,
            # downstream-формулы могут давать некорректный ranking.
            for _coll_name, _coll in (
                ("strategic_memory", self.strategic_collection),
                ("tactical_memory", self.tactical_collection),
            ):
                _actual = None
                _meta = getattr(_coll, "metadata", None)
                if not isinstance(_meta, dict):
                    self.embedding_metadata_mismatch = True
                    logger.error(
                        "Chroma collection '%s' exposes invalid metadata; "
                        "rebuild ChromaDB from SQLite before semantic memory operations.",
                        _coll_name,
                    )
                    continue
                _actual = _meta.get("hnsw:space")
                if _actual and _actual != chroma_metric:
                    logger.warning(
                        "Chroma collection '%s' uses hnsw:space='%s', "
                        "but TEXT_TO_SQL_CHROMA_METRIC='%s'. "
                        "Existing collection metric is preserved; re-create the collection "
                        "to switch (see memory.rebuild.rebuild_chromadb_from_sqlite).",
                        _coll_name,
                        _actual,
                        chroma_metric,
                    )
                stored_model = _meta.get("embedding_model_name")
                stored_dim = _meta.get("embedding_dimension")
                model_mismatch = (
                    isinstance(stored_model, str)
                    and stored_model
                    and stored_model != self.embedding_model_name
                )
                dim_mismatch = False
                if stored_dim is not None and self.embedding_dimension is not None:
                    try:
                        dim_mismatch = int(stored_dim) != int(self.embedding_dimension)
                    except (TypeError, ValueError):
                        dim_mismatch = True
                if model_mismatch or dim_mismatch:
                    self.embedding_metadata_mismatch = True
                    logger.error(
                        "Chroma collection '%s' embedding metadata mismatch: "
                        "stored model=%r dim=%r, current model=%r dim=%r. "
                        "Semantic search may fail; rebuild ChromaDB from SQLite.",
                        _coll_name,
                        stored_model,
                        stored_dim,
                        self.embedding_model_name,
                        self.embedding_dimension,
                    )

            _patch_chromadb_swig_type_modules()
            if self.embedding_metadata_mismatch:
                self.set_vector_readiness(
                    VectorReadiness.STALE,
                    "collection_metadata_mismatch",
                )
            else:
                self.set_vector_readiness(VectorReadiness.READY)
            print("ChromaDB инициализирована успешно")

        except Exception as e:
            print(f"Ошибка при инициализации ChromaDB: {e}")
            # Fallback: устанавливаем None чтобы система работала только с SQLite
            self.embedding_model = None
            self.embedding_model_name = ""
            self.embedding_dimension = None
            self.embedding_metadata_mismatch = False
            self.chroma_client = None
            self.strategic_collection = None
            self.tactical_collection = None
            self.set_vector_readiness(
                VectorReadiness.UNAVAILABLE,
                "initialization_failed",
            )

    @property
    def lock(self):
        """Возвращает блокировку для потокобезопасности"""
        return self._lock
