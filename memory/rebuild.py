"""
Утилиты пересборки векторной базы данных ChromaDB
=================================================

Этот модуль содержит функции для восстановления ChromaDB из SQLite данных.
Используется для:
- Восстановления после повреждения ChromaDB
- Обновления после изменения модели эмбеддингов  
- Принудительной синхронизации данных
"""

import json
import os
import threading
from contextlib import nullcontext
from typing import Dict, List
from smolagents import tool

from .chroma_text import extract_tactical_chroma_text
from .database import _embedding_dimension
from .manager import memory_manager


_SUPPORTED_CHROMA_METRICS = {"cosine", "l2", "ip"}
_REBUILD_LOCK = threading.RLock()


def _metric_from_collection(collection) -> str | None:
    """Return Chroma hnsw metric from an existing collection, if exposed."""
    metadata = getattr(collection, "metadata", None)
    if isinstance(metadata, dict):
        raw = metadata.get("hnsw:space") or metadata.get("metric")
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()

    configuration = getattr(collection, "configuration", None)
    if isinstance(configuration, dict):
        hnsw = configuration.get("hnsw")
        if isinstance(hnsw, dict):
            raw = hnsw.get("space")
            if isinstance(raw, str) and raw.strip():
                return raw.strip().lower()

    return None


def _resolve_rebuild_chroma_metric(handler) -> str:
    """Resolve metric for recreated collections.

    Existing collection metadata wins over env: rebuild should preserve the
    actual metric already on disk when Chroma exposes it. Env remains the
    fallback for fresh collections.
    """
    for collection in (
        getattr(handler, "tactical_collection", None),
        getattr(handler, "strategic_collection", None),
    ):
        metric = _metric_from_collection(collection)
        if metric:
            if metric not in _SUPPORTED_CHROMA_METRICS:
                raise ValueError(
                    f"Unsupported Chroma distance metric '{metric}' while rebuilding"
                )
            return metric

    metric = os.getenv("TEXT_TO_SQL_CHROMA_METRIC", "cosine").strip().lower() or "cosine"
    if metric not in _SUPPORTED_CHROMA_METRICS:
        raise ValueError(
            f"Unsupported TEXT_TO_SQL_CHROMA_METRIC '{metric}' while rebuilding"
        )
    return metric


def _collection_metadata(handler, description: str, chroma_metric: str) -> Dict:
    metadata = {
        "description": description,
        "hnsw:space": chroma_metric,
    }
    embedding_model_name = getattr(handler, "embedding_model_name", "") or ""
    if embedding_model_name:
        metadata["embedding_model_name"] = embedding_model_name

    embedding_dimension = getattr(handler, "embedding_dimension", None)
    if embedding_dimension is None:
        embedding_dimension = _embedding_dimension(getattr(handler, "embedding_model", None))
    if embedding_dimension is not None:
        metadata["embedding_dimension"] = int(embedding_dimension)

    return metadata


def _extract_tactical_chroma_text(data: Dict) -> str:
    """Mirror save_memory's Chroma document text selection for tactical rows."""
    return extract_tactical_chroma_text(
        data,
        fallback_extractor=memory_manager._extract_text_content,
    )


def _handler_lock_cm(handler):
    lock = getattr(handler, "lock", None)
    if lock is None:
        return nullcontext()
    return lock


def rebuild_chromadb_from_sqlite(db_handler=None) -> str:
    """Пересоздает коллекции ChromaDB из данных в SQLite.
    
    Args:
        db_handler: DatabaseHandler (если None, используется из memory_manager)
    
    Returns:
        str: Статус операции и статистика восстановления
    """
    handler = db_handler or memory_manager.db_handler
    
    if not handler.chroma_client or not handler.embedding_model:
        return "❌ ChromaDB или модель эмбеддингов не инициализированы."

    with _REBUILD_LOCK, _handler_lock_cm(handler):
        return _rebuild_chromadb_from_sqlite_locked(handler)


def _rebuild_chromadb_from_sqlite_locked(handler) -> str:
    chroma_metric = _resolve_rebuild_chroma_metric(handler)
    print("🔎 Подготовка данных для пересборки ChromaDB...")
    tactical_records, tactical_errors = _prepare_tactical_memory(handler)
    strategic_records, strategic_errors = _prepare_strategic_memory(handler)

    collections_touched = False
    try:
        print("🗑️ Удаление существующих коллекций ChromaDB...")
        collections_touched = True
        try:
            # Удаляем существующие коллекции
            try:
                handler.chroma_client.delete_collection("strategic_memory")
            except Exception:
                pass  # Коллекция может не существовать

            try:
                handler.chroma_client.delete_collection("tactical_memory")
            except Exception:
                pass  # Коллекция может не существовать
        except Exception as e:
            print(f"⚠️ Предупреждение при удалении коллекций: {e}")

        print("🔧 Создание новых коллекций...")
        # Пересоздаем коллекции.
        # W5-T1: явная метрика hnsw:space (default cosine), согласована с
        # database.py:_init_chroma. Поскольку коллекции выше удалены через
        # delete_collection, эта запись фактически применяется (в отличие от
        # get_or_create_collection поверх существующей коллекции).
        handler.strategic_collection = handler.chroma_client.get_or_create_collection(
            name="strategic_memory",
            metadata=_collection_metadata(
                handler,
                "High-level goals and context",
                chroma_metric,
            ),
        )
        handler.tactical_collection = handler.chroma_client.get_or_create_collection(
            name="tactical_memory",
            metadata=_collection_metadata(
                handler,
                "Detailed step-by-step agent experience",
                chroma_metric,
            ),
        )

        # Восстанавливаем тактическую память из agent_memory
        print("📊 Восстановление тактической памяти...")
        tactical_count = _add_tactical_memory(handler, tactical_records)

        # Восстанавливаем стратегическую память из strategic_memory
        print("🎯 Восстановление стратегической памяти...")
        strategic_count = _add_strategic_memory(handler, strategic_records)
    except Exception:
        if collections_touched and hasattr(handler, "embedding_metadata_mismatch"):
            handler.embedding_metadata_mismatch = True
        raise

    if hasattr(handler, "embedding_metadata_mismatch"):
        handler.embedding_metadata_mismatch = False
    
    # Выводим итоговую статистику
    result = f"✅ ChromaDB успешно пересоздана из SQLite:\n"
    result += f"📋 Тактическая память: {tactical_count} записей"
    if tactical_errors > 0:
        result += f" (ошибок: {tactical_errors})"
    result += f"\n🎯 Стратегическая память: {strategic_count} записей"
    if strategic_errors > 0:
        result += f" (ошибок: {strategic_errors})"
    result += f"\n🔍 Семантический поиск готов к использованию"
    
    print(result)
    return result


def _prepare_tactical_memory(handler) -> tuple[List[Dict], int]:
    """Читает SQLite и готовит embeddings до удаления старых Chroma коллекций.
    
    Args:
        handler: DatabaseHandler
        
    Returns:
        tuple: (prepared records, количество ошибок)
    """
    conn = handler._get_connection()
    try:
        cursor = conn.cursor()
        # ChromaDB хранит ТОЛЬКО активные записи (valid_to IS NULL)
        cursor.execute(
            "SELECT session_id, agent_name, step, instance_step, run_id, data "
            "FROM agent_memory WHERE valid_to IS NULL ORDER BY session_id, agent_name, step"
        )
        
        records = cursor.fetchall()
        prepared: List[Dict] = []
        errors_count = 0
        
        for session_id, agent_name, step, instance_step, run_id, data_json in records:
            tactical_id = f"{session_id}-{agent_name}-{step}"
            try:
                # Парсим JSON данные
                try:
                    data_dict = json.loads(data_json)
                except json.JSONDecodeError:
                    errors_count += 1
                    continue
                
                # Извлекаем текстовое содержимое
                text_content = _extract_tactical_chroma_text(data_dict)
                if not text_content or len(text_content.strip()) < 10:
                    continue  # Пропускаем слишком короткий контент
                
                # Создаем эмбеддинг
                embedding = memory_manager._create_embedding(
                    text_content,
                    purpose="passage",
                    allow_metadata_mismatch=True,
                )
                if not embedding:
                    continue
                
                # Добавляем в ChromaDB
                metadata = {
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "step": step,
                    "tactical_id": tactical_id,
                }
                key_fields = [
                    "cache_kind", "cache_key", "cache_source", "schema_version",
                    "filename", "table_fqn", "auto_loaded", "source",
                    "artifact_type", "file_hash", "topic", "category", "tags",
                    "is_global", "saved_by", "saved_at", "memory_source",
                ]
                for field in key_fields:
                    if field in data_dict and data_dict[field] is not None:
                        metadata[field] = str(data_dict[field])
                if run_id:
                    metadata["run_id"] = str(run_id)
                if instance_step is not None:
                    metadata["instance_step"] = int(instance_step)

                prepared.append(
                    {
                        "embedding": embedding,
                        "document": text_content,
                        "metadata": metadata,
                        "id": tactical_id,
                    }
                )
                    
            except Exception as e:
                errors_count += 1
                if errors_count <= 3:  # Показываем только первые 3 ошибки
                    print(f"   ⚠️ Ошибка подготовки тактической памяти {tactical_id}: {e}")
        
        return prepared, errors_count
        
    finally:
        conn.close()


def _add_tactical_memory(handler, records: List[Dict]) -> int:
    synced_count = 0
    for record in records:
        handler.tactical_collection.add(
            embeddings=[record["embedding"]],
            documents=[record["document"]],
            metadatas=[record["metadata"]],
            ids=[record["id"]],
        )
        synced_count += 1
        if synced_count % 20 == 0:
            print(f"   Синхронизировано: {synced_count} записей")
    print(f"   ✅ Тактическая память: {synced_count} записей")
    return synced_count


def _rebuild_tactical_memory(handler) -> tuple[int, int]:
    records, errors_count = _prepare_tactical_memory(handler)
    return _add_tactical_memory(handler, records), errors_count


def _prepare_strategic_memory(handler) -> tuple[List[Dict], int]:
    """Читает strategic_memory и готовит embeddings до удаления Chroma.
    
    Args:
        handler: DatabaseHandler
        
    Returns:
        tuple: (prepared records, количество ошибок)
    """
    conn = handler._get_connection()
    try:
        cursor = conn.cursor()
        # ChromaDB хранит ТОЛЬКО активные записи (valid_to IS NULL)
        cursor.execute("SELECT memory_id, session_id, type, content FROM strategic_memory WHERE valid_to IS NULL ORDER BY memory_id")
        
        records = cursor.fetchall()
        prepared: List[Dict] = []
        errors_count = 0
        
        for memory_id, session_id, memory_type, content in records:
            try:
                if not content or len(content.strip()) < 10:
                    continue  # Пропускаем слишком короткий контент
                
                # Создаем эмбеддинг
                embedding = memory_manager._create_embedding(
                    content,
                    purpose="passage",
                    allow_metadata_mismatch=True,
                )
                if not embedding:
                    continue
                
                prepared.append(
                    {
                        "embedding": embedding,
                        "document": content,
                        "metadata": {
                            "session_id": session_id,
                            "type": memory_type,
                            "memory_id": memory_id,
                        },
                        "id": str(memory_id),
                    }
                )
                
            except Exception as e:
                errors_count += 1
                if errors_count <= 3:
                    print(f"   ⚠️ Ошибка подготовки стратегической памяти {memory_id}: {e}")
        
        return prepared, errors_count
        
    finally:
        conn.close()


def _add_strategic_memory(handler, records: List[Dict]) -> int:
    synced_count = 0
    for record in records:
        handler.strategic_collection.add(
            embeddings=[record["embedding"]],
            documents=[record["document"]],
            metadatas=[record["metadata"]],
            ids=[record["id"]],
        )
        synced_count += 1
    print(f"   ✅ Стратегическая память: {synced_count} записей")
    return synced_count


def _rebuild_strategic_memory(handler) -> tuple[int, int]:
    records, errors_count = _prepare_strategic_memory(handler)
    return _add_strategic_memory(handler, records), errors_count


@tool
def rebuild_chromadb_tool() -> str:
    """Инструмент для пересборки ChromaDB из SQLite данных.
    
    Административный инструмент для восстановления векторной базы данных.
    Полностью очищает ChromaDB и восстанавливает все данные из SQLite.
    
    Returns:
        str: Статус операции и статистика восстановления
    """
    try:
        print("🔄 Начинаем пересоздание ChromaDB из SQLite...")
        result = rebuild_chromadb_from_sqlite()
        return result
        
    except Exception as e:
        error_msg = f"❌ Ошибка пересоздания ChromaDB: {str(e)}"
        print(error_msg)
        return error_msg


# Обновляем метод в memory_manager для использования новой логики
def patch_memory_manager():
    """Обновляет методы memory_manager для использования новой логики пересборки"""
    def new_rebuild_method(self):
        """Новый метод пересборки для MemoryManager"""
        return rebuild_chromadb_from_sqlite(self.db_handler)
    
    # Заменяем метод
    memory_manager.rebuild_chromadb_from_sqlite = new_rebuild_method.__get__(memory_manager, memory_manager.__class__)


# Применяем патч при импорте
patch_memory_manager()
