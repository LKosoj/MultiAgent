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

warnings.filterwarnings(
    "ignore",
    message=r"builtin type (SwigPyPacked|SwigPyObject|swigvarlink) has no __module__ attribute",
    category=DeprecationWarning,
)

import chromadb
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


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
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

# Устанавливаем оффлайн режим для HuggingFace Hub (использовать только локальный кэш)
# Это предотвратит попытки скачивания модели при каждом запуске
if os.environ.get('HF_HUB_OFFLINE') != '0':  # Можно переопределить через env переменную
    os.environ['HF_HUB_OFFLINE'] = '1'

# Подавляем предупреждения от transformers
warnings.filterwarnings('ignore', category=UserWarning, module='transformers')


class DatabaseHandler:
    """Обработчик баз данных для системы памяти"""
    
    def __init__(self, 
                 db_path: str = "memory/smolagents_memory.db", 
                 chroma_path: str = "memory/chromadb",
                 embedding_model: str = "intfloat/multilingual-e5-base"):
        """Инициализация обработчика БД
        
        Args:
            db_path: Путь к файлу базы данных SQLite
            chroma_path: Путь к директории ChromaDB
            embedding_model: Модель для создания эмбеддингов
        """
        self.db_path = db_path
        self.chroma_path = chroma_path
        self._lock = threading.Lock()
        
        # Инициализация SQLite
        self._init_db()
        
        # Инициализация ChromaDB
        self._init_chroma(embedding_model)
    
    def _get_connection(self):
        """Создает новое соединение с базой данных для текущего потока"""
        return sqlite3.connect(self.db_path, check_same_thread=False)

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
            cursor.execute('''
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
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_session_agent 
                ON agent_memory(session_id, agent_name)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_memory_temporal 
                ON agent_memory(valid_from, valid_to)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_memory_active 
                ON agent_memory(session_id) WHERE valid_to IS NULL
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_memory_run 
                ON agent_memory(session_id, agent_name, run_id)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_memory_instance_step 
                ON agent_memory(session_id, agent_name, instance_step)
            ''')
            # --- Strategic Memory Table ---
            cursor.execute('''
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
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_strategic_session_type
                ON strategic_memory(session_id, type)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_strategic_temporal 
                ON strategic_memory(valid_from, valid_to)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_strategic_active 
                ON strategic_memory(session_id, type) WHERE valid_to IS NULL
            ''')
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
                self.embedding_model = SentenceTransformer(embedding_model, local_files_only=True)
                print(f"✅ Модель загружена из локального кэша")
            except Exception as e:
                # Если модели нет локально, загружаем из интернета
                print(f"⚠️ Модель не найдена локально, загружаем из HuggingFace...")
                self.embedding_model = SentenceTransformer(embedding_model)
            # Сохраняем читаемое имя модели для UI
            self.embedding_model_name = embedding_model
            
            # Создаем клиент ChromaDB с отключенной телеметрией
            from chromadb.config import Settings
            settings = Settings(anonymized_telemetry=False)
            self.chroma_client = chromadb.PersistentClient(
                path=self.chroma_path,
                settings=settings
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
            chroma_metric = os.getenv("TEXT_TO_SQL_CHROMA_METRIC", "cosine").strip().lower() or "cosine"
            self.strategic_collection = self.chroma_client.get_or_create_collection(
                name="strategic_memory",
                metadata={
                    "description": "High-level goals and context",
                    "hnsw:space": chroma_metric,
                },
            )

            self.tactical_collection = self.chroma_client.get_or_create_collection(
                name="tactical_memory",
                metadata={
                    "description": "Detailed step-by-step agent experience",
                    "hnsw:space": chroma_metric,
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
                if isinstance(_meta, dict):
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
            
            _patch_chromadb_swig_type_modules()
            print("ChromaDB инициализирована успешно")
            
        except Exception as e:
            print(f"Ошибка при инициализации ChromaDB: {e}")
            # Fallback: устанавливаем None чтобы система работала только с SQLite
            self.embedding_model = None
            self.embedding_model_name = ""
            self.chroma_client = None
            self.strategic_collection = None
            self.tactical_collection = None

    @property
    def lock(self):
        """Возвращает блокировку для потокобезопасности"""
        return self._lock
