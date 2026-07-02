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
from typing import Dict, List
from smolagents import tool

from .database import _embedding_dimension
from .manager import memory_manager


_SUPPORTED_CHROMA_METRICS = {"cosine", "l2", "ip"}


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
    if isinstance(data, dict):
        ck = data.get("cache_kind")

        if ck == "agent_step":
            agent_response = data.get("agent_response")
            if isinstance(agent_response, str) and agent_response.strip():
                return agent_response.strip()
            slim = {
                k: v
                for k, v in data.items()
                if k not in ("timestamp", "policy_scope", "agent_context")
            }
            return memory_manager._extract_text_content(slim)

        if ck == "agent_summary":
            summary_text = data.get("summary_text")
            if isinstance(summary_text, str) and summary_text.strip():
                return summary_text.strip()
            return memory_manager._extract_text_content(data)

        if ck == "schema_table":
            table_fqn = data.get("table_fqn") or ""
            desc = data.get("description") or ""
            cols = []
            try:
                table_info = data.get("table_info") or {}
                if isinstance(table_info, dict):
                    for col in table_info.get("columns") or []:
                        if isinstance(col, dict):
                            name = col.get("name")
                            col_desc = col.get("description")
                            if name:
                                if isinstance(col_desc, str) and col_desc.strip():
                                    cols.append(f"{name}: {col_desc.strip()}")
                                else:
                                    cols.append(str(name))
            except Exception:
                cols = []
            cols_text = "; ".join(cols[:40])
            base = f"Таблица {table_fqn}. {desc}".strip()
            if cols_text:
                base = f"{base}\nКолонки: {cols_text}"
            return base[:8000]

    return memory_manager._extract_text_content(data)


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

    chroma_metric = _resolve_rebuild_chroma_metric(handler)

    print("🗑️ Удаление существующих коллекций ChromaDB...")
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
    tactical_count, tactical_errors = _rebuild_tactical_memory(handler)
    
    # Восстанавливаем стратегическую память из strategic_memory
    print("🎯 Восстановление стратегической памяти...")
    strategic_count, strategic_errors = _rebuild_strategic_memory(handler)
    
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


def _rebuild_tactical_memory(handler) -> tuple[int, int]:
    """Восстанавливает тактическую память из таблицы agent_memory
    
    Args:
        handler: DatabaseHandler
        
    Returns:
        tuple: (количество восстановленных записей, количество ошибок)
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
        synced_count = 0
        errors_count = 0
        
        for session_id, agent_name, step, instance_step, run_id, data_json in records:
            try:
                # Создаем составной ID как в оригинальном коде
                tactical_id = f"{session_id}-{agent_name}-{step}"
                
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
                embedding = memory_manager._create_embedding(text_content, purpose="passage")
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

                handler.tactical_collection.add(
                    embeddings=[embedding],
                    documents=[text_content],
                    metadatas=[metadata],
                    ids=[tactical_id]
                )
                
                synced_count += 1
                if synced_count % 20 == 0:
                    print(f"   Синхронизировано: {synced_count} записей")
                    
            except Exception as e:
                errors_count += 1
                if errors_count <= 3:  # Показываем только первые 3 ошибки
                    print(f"   ⚠️ Ошибка синхронизации {tactical_id}: {e}")
        
        print(f"   ✅ Тактическая память: {synced_count} записей, ошибок: {errors_count}")
        return synced_count, errors_count
        
    finally:
        conn.close()


def _rebuild_strategic_memory(handler) -> tuple[int, int]:
    """Восстанавливает стратегическую память из таблицы strategic_memory
    
    Args:
        handler: DatabaseHandler
        
    Returns:
        tuple: (количество восстановленных записей, количество ошибок)
    """
    conn = handler._get_connection()
    try:
        cursor = conn.cursor()
        # ChromaDB хранит ТОЛЬКО активные записи (valid_to IS NULL)
        cursor.execute("SELECT memory_id, session_id, type, content FROM strategic_memory WHERE valid_to IS NULL ORDER BY memory_id")
        
        records = cursor.fetchall()
        synced_count = 0
        errors_count = 0
        
        for memory_id, session_id, memory_type, content in records:
            try:
                if not content or len(content.strip()) < 10:
                    continue  # Пропускаем слишком короткий контент
                
                # Создаем эмбеддинг
                embedding = memory_manager._create_embedding(content, purpose="passage")
                if not embedding:
                    continue
                
                # Добавляем в ChromaDB
                handler.strategic_collection.add(
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[{
                        "session_id": session_id,
                        "type": memory_type,
                        "memory_id": memory_id
                    }],
                    ids=[str(memory_id)]
                )
                
                synced_count += 1
                
            except Exception as e:
                errors_count += 1
                if errors_count <= 3:
                    print(f"   ⚠️ Ошибка синхронизации стратегической памяти {memory_id}: {e}")
        
        print(f"   ✅ Стратегическая память: {synced_count} записей, ошибок: {errors_count}")
        return synced_count, errors_count
        
    finally:
        conn.close()


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
